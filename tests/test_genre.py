"""Song genre recognition: preprocessing, vocabulary, look-ups, fusion, BPM/key, source, service.

Everything here is offline: yt-dlp and ffmpeg are a fake runner, the model is a fake inference
function, Discogs and Last.fm answer through ``httpx.MockTransport``, and the model download is a
fake fetch.  No Shazam, no AudD, no network.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import numpy as np
import pytest

from id_detector.genre import frontend, fuse, lookup, source, tempo_key, vocab
from id_detector.genre import model as style_model
from id_detector.genre.lookup import Lookups, SourceEvidence
from id_detector.genre.service import GenreService, artwork_allowed
from id_detector.process import ProcessError, ProcessResult

FIXTURES = Path(__file__).parent / "fixtures" / "genre"
SR = 16_000
LABELS = vocab.labels()


def label_index(name: str) -> int:
    return LABELS.index(name if "---" in name else f"Electronic---{name}")


# ------------------------------------------------------------------------------------------
# Preprocessing: the exact Essentia MusiCNN mel frontend
# ------------------------------------------------------------------------------------------
def _reference_signal() -> np.ndarray:
    t = np.arange(32000, dtype=np.float64) / SR
    x = (
        0.3 * np.sin(2 * np.pi * 220 * t)
        + 0.2 * np.sin(2 * np.pi * 1234.5 * t)
        + 0.1 * np.sin(2 * np.pi * 5000 * t)
        + 0.05 * np.sin(2 * np.pi * 70 * t)
    )
    return x.astype(np.float32)


def test_mel_frontend_matches_essentias_own_cpp_output():
    """Reference values come from Essentia's C++ (essentia.js WASM) on the same signal."""

    reference = json.loads((FIXTURES / "essentia_musicnn_reference.json").read_text("utf-8"))
    mel = frontend.melspectrogram(_reference_signal())
    assert mel.shape == (reference["frames"], 96)
    assert np.abs(mel[0] - np.array(reference["frame_0"])).max() < 1e-3
    assert np.abs(mel[60] - np.array(reference["frame_60"])).max() < 1e-3
    assert np.abs(mel.mean(axis=0) - np.array(reference["mean"])).max() < 1e-3


def test_zero_input_gives_zero_bands_like_essentia():
    assert np.all(frontend.melspectrogram(np.zeros(4096, dtype=np.float32)) == 0)


def test_frame_count_follows_essentias_frame_cutter():
    # First frame centred on sample 0; the last is the first whose centre reaches the end.
    assert frontend.frame_count(0) == 0
    assert frontend.frame_count(1) == 2
    assert frontend.frame_count(256) == 2
    assert frontend.frame_count(257) == 3
    assert frontend.frame_count(32000) == 126


def test_mel_filterbank_is_slaney_unit_triangle_area():
    bank = frontend.mel_filterbank()
    assert bank.shape == (257, 96)
    assert np.all(bank >= 0)
    # Every band has some weight, and band centres rise monotonically.
    centres = (bank * np.arange(257)[:, None]).sum(0) / bank.sum(0)
    assert np.all(np.diff(centres) > 0)


def test_patches_are_128_frames_every_62_with_the_tail_discarded():
    mel = np.arange(300 * 96, dtype=np.float32).reshape(300, 96)
    patches = frontend.patches(mel)
    assert patches.shape == (3, 128, 96)  # starts 0, 62, 124 (186 + 128 > 300)
    assert np.array_equal(patches[1], mel[62:190])
    assert frontend.patches(mel[:100]).shape == (0, 128, 96)


def test_predict_averages_every_patch_of_every_excerpt():
    seen = []

    def infer(batch: np.ndarray) -> np.ndarray:
        seen.append(batch.shape)
        out = np.zeros((batch.shape[0], 400), dtype=np.float32)
        out[:, 7] = np.linspace(0, 1, batch.shape[0])
        return out

    signals = [np.random.default_rng(1).standard_normal(SR * 3).astype(np.float32)] * 2
    probabilities = style_model.predict(signals, infer)
    assert seen and seen[0][1:] == (128, 96)
    assert probabilities.shape == (400,)
    assert probabilities[7] == pytest.approx(0.5, abs=1e-6)
    with pytest.raises(style_model.ModelError):
        style_model.predict([np.zeros(1000, dtype=np.float32)], infer)


# ------------------------------------------------------------------------------------------
# Model download
# ------------------------------------------------------------------------------------------
def _tiny_model(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    payloads = {"tiny.onnx": b"onnx-bytes" * 10, "tiny.json": b'{"classes": []}'}
    files = tuple(
        style_model.ModelFile(name, hashlib.sha256(body).hexdigest(), len(body))
        for name, body in payloads.items()
    )
    monkeypatch.setattr(style_model, "FILES", files)
    monkeypatch.setattr(style_model, "DOWNLOAD_BYTES", sum(len(b) for b in payloads.values()))
    return payloads


def test_model_download_verifies_checksums_and_reports_progress(tmp_path, monkeypatch):
    payloads = _tiny_model(monkeypatch)
    fetched: list[str] = []
    progress: list[tuple[int, int]] = []

    def fetch(url: str, destination: Path, on_bytes) -> None:
        name = url.rsplit("/", 1)[-1]
        fetched.append(name)
        destination.write_bytes(payloads[name])
        on_bytes(len(payloads[name]))

    style_model.ensure_model(tmp_path, lambda d, t: progress.append((d, t)), fetch=fetch)
    assert style_model.is_ready(tmp_path)
    assert fetched == ["tiny.onnx", "tiny.json"]
    assert progress[-1][0] == progress[-1][1] == style_model.DOWNLOAD_BYTES
    # Ready: no second download.
    style_model.ensure_model(tmp_path, fetch=fetch)
    assert len(fetched) == 2
    assert not list(tmp_path.glob("*.part"))


def test_a_corrupt_model_download_is_refused_and_not_kept(tmp_path, monkeypatch):
    _tiny_model(monkeypatch)

    def fetch(url: str, destination: Path, on_bytes) -> None:
        destination.write_bytes(b"x" * 100)

    with pytest.raises(style_model.ModelError):
        style_model.ensure_model(tmp_path, fetch=fetch)
    assert not style_model.is_ready(tmp_path)
    assert not list(tmp_path.iterdir())


def test_the_real_model_files_are_pinned_and_git_ignored():
    assert style_model.ONNX.url.startswith("https://essentia.upf.edu/models/")
    assert len(style_model.ONNX.sha256) == 64
    gitignore = (Path(__file__).resolve().parents[1] / ".gitignore").read_text("utf-8")
    assert "\nmodels/\n" in gitignore


# ------------------------------------------------------------------------------------------
# Vocabulary and tag mapping
# ------------------------------------------------------------------------------------------
def test_vocabulary_has_the_400_discogs_styles_in_model_order():
    assert len(LABELS) == 400
    assert LABELS[0] == "Blues---Boogie Woogie"
    assert "Electronic---Speed Garage" in LABELS
    assert vocab.parent("Electronic---Bassline") == "Electronic"
    assert vocab.display("Electronic---Drum n Bass") == "Drum n Bass"
    assert vocab.display("Hip Hop---Grime") == "Grime (Hip Hop)"


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("ukg", ["UK Garage"]),
        ("UK Garage", ["UK Garage"]),
        ("2step", ["UK Garage"]),
        ("Speed Garage", ["Speed Garage"]),
        ("speedgarage", ["Speed Garage"]),
        ("niche", ["Bassline"]),
        ("drum & bass", ["Drum n Bass"]),
        ("Drum and bass", ["Drum n Bass"]),
        ("dnb", ["Drum n Bass"]),
        ("breaks", ["Breakbeat", "Breaks"]),
        ("Deep House", ["Deep House"]),
        ("tech-house", ["Tech House"]),
        ("dubstep", ["Dubstep"]),
    ],
)
def test_tags_map_onto_the_discogs_vocabulary(tag, expected):
    assert [vocab.style(x) for x in vocab.map_tag(tag)] == expected


@pytest.mark.parametrize("tag", ["seen live", "british", "00s", "electronic", "dance", "favorites"])
def test_junk_and_umbrella_tags_are_ignored(tag):
    assert vocab.map_tag(tag) == ()


def test_umbrella_tags_name_only_the_parent():
    assert vocab.parent_tag("Electronic") == "Electronic"
    assert vocab.parent_tag("speed garage") is None


# ------------------------------------------------------------------------------------------
# Look-ups (parsers and clients, with fake HTTP)
# ------------------------------------------------------------------------------------------
DISCOGS_PAYLOAD = {
    "results": [
        {
            "title": "Double 99 - RIP Groove",
            "style": ["Speed Garage", "UK Garage"],
            "genre": ["Electronic"],
            "format": ["Vinyl", '12"'],
        },
        {
            "title": "Double 99 - Ripgroove (Remixes)",
            "style": ["Speed Garage"],
            "genre": ["Electronic"],
            "format": ["Vinyl"],
        },
        {
            "title": "Various - Garage Anthems",
            "style": ["House", "UK Garage", "Speed Garage", "Bassline"],
            "genre": ["Electronic"],
            "format": ["CD", "Compilation"],
        },
        {
            "title": "Somebody Else - Groove",
            "style": ["Techno"],
            "genre": ["Electronic"],
            "format": ["Vinyl"],
        },
    ]
}


def test_discogs_styles_are_weighted_by_rank_and_compilations_count_less():
    evidence = lookup.parse_discogs(DISCOGS_PAYLOAD, "Double 99", "RIP Groove")
    assert evidence.status == "ok"
    shares = {vocab.style(k): v for k, v in evidence.shares.items()}
    assert max(shares, key=shares.get) == "Speed Garage"
    assert "Techno" not in shares  # a different artist's record is skipped
    assert sum(evidence.shares.values()) == pytest.approx(1.0)
    assert evidence.detail == "3 matching releases"
    assert evidence.quality > 0.5


def test_discogs_without_results_is_no_evidence():
    evidence = lookup.parse_discogs({"results": []}, "A", "B")
    assert evidence.status == "none" and not evidence.shares


def test_lastfm_tags_drop_junk_and_artist_tags_count_less():
    payload = {
        "toptags": {
            "tag": [
                {"name": "speed garage", "count": 100},
                {"name": "UK Garage", "count": 60},
                {"name": "seen live", "count": 90},
                {"name": "electronic", "count": 50},
                {"name": "house", "count": 5},
            ]
        }
    }
    track = lookup.parse_lastfm(payload, artist_level=False)
    artist = lookup.parse_lastfm(payload, artist_level=True)
    assert set(vocab.style(k) for k in track.shares) == {"Speed Garage", "UK Garage"}
    assert track.quality == 1.0 and artist.quality == 0.5
    assert track.parents["Electronic"] > 0.9
    assert (
        lookup.parse_lastfm({"error": 6, "message": "Track not found"}, artist_level=False).status
        == "none"
    )


def _mock_transport(calls: list[httpx.Request], *, track_tags: list | None = None, discogs=None):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "api.discogs.com":
            assert request.headers["Authorization"] == "Discogs token=disc-token"
            return httpx.Response(200, json=discogs if discogs is not None else DISCOGS_PAYLOAD)
        method = request.url.params.get("method")
        if method == "track.gettoptags":
            return httpx.Response(200, json={"toptags": {"tag": track_tags or []}})
        if method == "artist.gettoptags":
            tags = [{"name": "House", "count": 100}, {"name": "UK Garage", "count": 70},
                    {"name": "garage", "count": 40}, {"name": "2step", "count": 20}]  # fmt: skip
            return httpx.Response(200, json={"toptags": {"tag": tags}})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_lookups_fall_back_to_artist_tags_when_track_tags_are_empty(monkeypatch):
    monkeypatch.setenv("DISCOGS_API_TOKEN", "disc-token")
    monkeypatch.setenv("LASTFM_API_KEY", "fm-key")
    calls: list[httpx.Request] = []
    client = Lookups(transport=_mock_transport(calls))
    discogs = asyncio.run(client.discogs("Double 99", "RIP Groove"))
    lastfm = asyncio.run(client.lastfm("Todd Edwards", "Saved My Life"))
    assert discogs.status == "ok"
    assert lastfm.status == "ok" and lastfm.detail == "artist tags" and lastfm.quality == 0.5
    methods = [c.url.params.get("method") for c in calls if c.url.host != "api.discogs.com"]
    assert methods == ["track.gettoptags", "artist.gettoptags"]
    assert all(
        c.url.params.get("api_key") == "fm-key" for c in calls if "audioscrobbler" in str(c.url)
    )


def test_the_discogs_token_alias_and_missing_keys(monkeypatch):
    monkeypatch.delenv("DISCOGS_API_TOKEN", raising=False)
    monkeypatch.delenv("LASTFM_API_KEY", raising=False)
    monkeypatch.setenv("DISCOGS_TOKEN", "disc-token")
    assert Lookups.enabled() == {"discogs": True, "lastfm": False}
    off = asyncio.run(Lookups(transport=_mock_transport([])).lastfm("A", "B"))
    assert off.status == "off" and "Last.fm" in off.detail
    monkeypatch.delenv("DISCOGS_TOKEN")
    assert Lookups.enabled() == {"discogs": False, "lastfm": False}


def test_a_failing_lookup_is_reported_not_raised(monkeypatch):
    monkeypatch.setenv("DISCOGS_API_TOKEN", "disc-token")
    transport = httpx.MockTransport(lambda request: httpx.Response(429))
    evidence = asyncio.run(Lookups(transport=transport).discogs("A", "B"))
    assert evidence.status == "error" and "rate limited" in evidence.detail


# ------------------------------------------------------------------------------------------
# Fusion
# ------------------------------------------------------------------------------------------
def _probabilities(**styles: float) -> np.ndarray:
    p = np.full(400, 0.01)
    for name, value in styles.items():
        p[label_index(name.replace("_", " "))] = value
    return p


def _evidence(source: str, quality: float = 1.0, **styles: float) -> SourceEvidence:
    shares = {f"Electronic---{k.replace('_', ' ')}": v for k, v in styles.items()}
    return SourceEvidence(source, shares=shares, quality=quality, status="ok")


def test_heard_only_keeps_the_model_ranking():
    p = _probabilities(Bassline=0.5, Speed_Garage=0.3, UK_Garage=0.2)
    result = fuse.fuse(p)
    assert [g["name"] for g in result.ranking] == ["Bassline", "Speed Garage", "UK Garage"]
    assert result.agreement == "heard-only"
    assert result.parent == "Electronic"
    assert result.model_ranking[0]["name"] == "Bassline"
    assert result.ranking[0]["sources"] == ["heard"]


def test_agreeing_sources_raise_the_answer():
    p = _probabilities(Speed_Garage=0.5, UK_Garage=0.3, Bassline=0.25)
    alone = fuse.fuse(p).ranking[0]["share"]
    both = fuse.fuse(
        p,
        _evidence("discogs", Speed_Garage=0.7, UK_Garage=0.3),
        _evidence("lastfm", Speed_Garage=1.0),
    )
    assert both.ranking[0]["name"] == "Speed Garage"
    assert both.ranking[0]["share"] > alone
    assert both.agreement == "agree"
    assert set(both.ranking[0]["sources"]) == {"heard", "discogs", "lastfm"}


def test_disagreement_shows_both_sides():
    p = _probabilities(Hardcore=0.5, Drum_n_Bass=0.2)
    result = fuse.fuse(p, _evidence("discogs", Drum_n_Bass=1.0), _evidence("lastfm", Jungle=1.0))
    names = [g["name"] for g in result.ranking]
    assert "Drum n Bass" in names and "Hardcore" in names
    assert result.agreement == "disagree"
    assert "Hardcore" in result.summary and "Drum n Bass" in result.summary
    # The model's own ranking is untouched by the look-ups.
    assert result.model_ranking[0]["name"] == "Hardcore"


def test_a_broad_lookup_style_counts_half():
    p = _probabilities(Speed_Garage=0.4, House=0.38)
    result = fuse.fuse(p, _evidence("discogs", House=0.5, Speed_Garage=0.5))
    assert result.ranking[0]["name"] == "Speed Garage"


def test_lookups_without_evidence_do_not_change_the_answer():
    p = _probabilities(Dubstep=0.5, Grime=0.4)
    none = SourceEvidence("discogs", status="none")
    off = SourceEvidence("lastfm", status="off")
    assert fuse.fuse(p, none, off).ranking == fuse.fuse(p).ranking


# ------------------------------------------------------------------------------------------
# BPM and key
# ------------------------------------------------------------------------------------------
def _drums(bpm: float, seconds: float = 15.0, offset: float = 0.0) -> np.ndarray:
    n = int(seconds * SR)
    x = np.zeros(n)
    t = np.arange(int(0.12 * SR)) / SR
    kick = np.sin(2 * np.pi * (50 + 80 * np.exp(-t * 30)) * t) * np.exp(-t * 20)
    rng = np.random.default_rng(3)
    hat = rng.standard_normal(int(0.03 * SR)) * np.exp(-np.arange(int(0.03 * SR)) / SR * 150) * 0.3
    beat = 60.0 / bpm
    k = 0
    while (start := int((offset + k * beat) * SR)) < n:
        end = min(n, start + kick.size)
        x[start:end] += kick[: end - start]
        h = int((offset + (k + 0.5) * beat) * SR)
        if h < n:
            end = min(n, h + hat.size)
            x[h:end] += hat[: end - h]
        k += 1
    return x


def _chords(tonic: int, minor: bool, seconds: float = 15.0) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    x = np.zeros_like(t)
    quarter = len(t) // 4
    for j, root in enumerate((0, 5, 7, 0)):
        third = 4 if root == 7 or not minor else 3
        for interval in (0, third, 7):
            f = 440 * 2 ** ((48 + tonic + root + interval - 69) / 12)
            for harmonic, amp in ((1, 1.0), (2, 0.5), (3, 0.25)):
                x[j * quarter : (j + 1) * quarter] += amp * np.sin(
                    2 * np.pi * f * harmonic * t[j * quarter : (j + 1) * quarter]
                )
        bass = 440 * 2 ** ((36 + tonic + root - 69) / 12)
        x[j * quarter : (j + 1) * quarter] += 0.8 * np.sin(
            2 * np.pi * bass * t[j * quarter : (j + 1) * quarter]
        )
    return x / np.abs(x).max()


@pytest.mark.parametrize("bpm", [120.0, 124.0, 128.0, 132.5, 140.0, 150.0])
def test_tempo_of_a_click_track(bpm):
    tempo = tempo_key.estimate_tempo([_drums(bpm, offset=o) for o in (0.0, 0.11, 0.37)])
    assert tempo is not None and tempo.bpm == pytest.approx(bpm, abs=0.3)


def test_a_fast_style_picks_the_fast_octave_and_otherwise_half_wins():
    segments = [_drums(174.0, offset=o) for o in (0.0, 0.2)]
    assert tempo_key.estimate_tempo(segments, "Electronic---Drum n Bass").bpm == pytest.approx(
        174, abs=0.3
    )
    assert tempo_key.estimate_tempo(segments).bpm == pytest.approx(87, abs=0.3)


def test_tempo_of_silence_is_none():
    assert tempo_key.estimate_tempo([np.zeros(SR * 10)]) is None


@pytest.mark.parametrize(
    ("tonic", "minor"), [(0, False), (9, True), (7, False), (5, True), (2, True)]
)
def test_key_of_a_chord_progression(tonic, minor):
    key = tempo_key.estimate_key([_chords(tonic, minor) + 0.3 * _drums(126.0)])
    assert key is not None
    assert key.tonic == tempo_key.PITCH_NAMES[tonic]
    assert key.mode == ("minor" if minor else "major")


def test_camelot_notation():
    pcp_a_minor = np.roll(tempo_key._EDMA_MINOR, 0)  # tonic first = A (index 0 from A)
    index, mode, strength, _ = tempo_key.key_from_pcp(pcp_a_minor)
    assert (tempo_key._KEY_NAMES_FROM_A[index], mode) == ("A", "minor")
    assert strength == pytest.approx(1.0)
    assert tempo_key._CAMELOT_MINOR[9] == 8  # A minor = 8A
    assert tempo_key._CAMELOT_MAJOR[0] == 8  # C major = 8B
    assert tempo_key._CAMELOT_MINOR[8] == 1  # Ab minor = 1A


# ------------------------------------------------------------------------------------------
# Finding the song
# ------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "kind", "platform"),
    [
        ("Double 99 - RIP Groove", "search", "other"),
        ("https://soundcloud.com/artist/track", "link", "soundcloud"),
        ("soundcloud.com/artist/track", "link", "soundcloud"),
        ("https://www.youtube.com/watch?v=abcdefghijk", "link", "youtube"),
        ("https://youtu.be/abcdefghijk", "link", "youtube"),
        (
            "https://open.spotify.com/intl-de/track/4uLU6hMCjMI75M1A2tKUQC?si=x",
            "spotify",
            "spotify",
        ),
        ("spotify:track:4uLU6hMCjMI75M1A2tKUQC", "spotify", "spotify"),
    ],
)
def test_input_is_classified(text, kind, platform):
    query = source.classify(text)
    assert (query.kind, query.platform) == (kind, platform)
    if kind == "spotify":
        assert query.text == "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "https://open.spotify.com/album/abc",
        "https://example.com/x",
        "https://u:p@soundcloud.com/a",
    ],
)
def test_unusable_input_is_refused_in_plain_words(text):
    with pytest.raises(source.SourceError):
        source.classify(text)


def test_artist_and_title_are_split_and_cleaned():
    assert source.split_artist_title("Double 99 - RIP Groove (Official Video)") == (
        "Double 99",
        "RIP Groove",
    )
    assert source.split_artist_title("Rip Groove (Original Mix)", "Double 99 - Topic") == (
        "Double 99",
        "Rip Groove",
    )
    assert source.split_artist_title("Track (Someone Remix)", "x") == ("x", "Track (Someone Remix)")


def test_search_ranking_prefers_a_real_song_over_previews_and_mixes():
    q = "Double 99 - RIP Groove"
    song = source.Candidate("u1", "Double 99 - RIP Groove", "Skint", "youtube", 217)
    preview = source.Candidate("u2", "Double 99 - RIP Groove", "Double 99", "soundcloud", 30)
    mix = source.Candidate("u3", "Garage mix ft RIP Groove", "dj", "youtube", 3600)
    scores = [source.score_candidate(q, c, None) for c in (song, preview, mix)]
    assert scores[0] > scores[1] and scores[0] > scores[2]


def test_excerpts_come_from_the_body_of_the_song():
    starts = source.segment_starts(240.0)
    assert len(starts) == 3 and starts[0] == pytest.approx(72.0) and starts[-1] <= 240 - 17
    assert source.segment_starts(10.0) == [0.0]
    assert len(source.segment_starts(45.0)) == 1


SPOTIFY_PAGE = (
    "<html><head><title>Sandstorm - song and lyrics by Darude | Spotify</title>"
    '<meta property="og:title" content="Sandstorm"/>'
    '<meta property="og:description" content="Darude · Before the Storm · Song · 2001"/>'
    '<meta property="og:image" content="https://i.scdn.co/image/abc"/>'
    '<meta name="music:duration" content="225"/></head></html>'
)


def test_a_spotify_page_gives_artist_title_and_length():
    track = source.parse_spotify_page(SPOTIFY_PAGE)
    assert track is not None
    assert (track.artist, track.title, track.duration) == ("Darude", "Sandstorm", 225.0)
    assert source.parse_spotify_page("<html></html>") is None


# ------------------------------------------------------------------------------------------
# Fake yt-dlp / ffmpeg, fake model, the whole service
# ------------------------------------------------------------------------------------------
def _song(start: float, seconds: float) -> np.ndarray:
    return (0.5 * _chords(9, True, seconds) + 0.5 * _drums(128.0, seconds)).astype(np.float32)


class FakeRunner:
    """Answers yt-dlp searches and ``-J`` resolves from tables, and "decodes" synthetic audio."""

    def __init__(self, *, drm: tuple[str, ...] = (), entries: dict | None = None) -> None:
        self.calls: list[list[str]] = []
        self.drm = drm
        self.entries = entries or {
            "scsearch": [
                {"title": "Double 99 - ripgroove", "url": "https://soundcloud.com/x/ripgroove",
                 "uploader": "fan", "duration": 196.6, "ie_key": "Soundcloud"},
                {"title": "RIP Groove (Radio Edit)", "url": "https://soundcloud.com/double99/rip",
                 "uploader": "Double 99", "duration": 30.0},
            ],
            "ytsearch": [
                {"title": "Double 99 - RIP Groove (Original)", "url": "https://www.youtube.com/watch?v=uR3Vw8J8vUo",
                 "channel": "Skint Records", "duration": 217, "id": "uR3Vw8J8vUo"},
            ],
        }  # fmt: skip

    async def __call__(self, args, timeout):
        args = [str(a) for a in args]
        self.calls.append(args)
        if args[0] == "ffmpeg":
            out = Path(args[-1])
            start = float(args[args.index("-ss") + 1])
            seconds = float(args[args.index("-t") + 1])
            _song(start, seconds).tofile(out)
            return ProcessResult(tuple(args), 0, "", "")
        target = args[-1]
        for prefix, entries in self.entries.items():
            if target.startswith(prefix):
                return ProcessResult(tuple(args), 0, json.dumps({"entries": entries}), "")
        if target in self.drm:
            raise ProcessError(
                ProcessResult(tuple(args), 1, "", "ERROR: This video is DRM protected")
            )
        youtube = "youtube" in target
        info = {
            "title": "Double 99 - RIP Groove (Original)" if youtube else "Double 99 - ripgroove",
            "uploader": "Skint Records" if youtube else "fan",
            "duration": 217.0,
            "webpage_url": target,
            "extractor_key": "Youtube" if youtube else "Soundcloud",
            "id": "uR3Vw8J8vUo" if youtube else "123",
            "url": "https://media.example/stream.m4a",
            "http_headers": {"User-Agent": "test"},
        }
        return ProcessResult(tuple(args), 0, json.dumps(info), "")

    def count(self, program: str) -> int:
        return sum(1 for c in self.calls if program in " ".join(c[:3]))


def _fake_infer(model_dir: Path):
    def infer(batch: np.ndarray) -> np.ndarray:
        out = np.full((batch.shape[0], 400), 0.01, dtype=np.float32)
        out[:, label_index("Speed Garage")] = 0.6
        out[:, label_index("UK Garage")] = 0.35
        out[:, label_index("Bassline")] = 0.25
        return out

    return infer


@pytest.fixture
def no_keys(monkeypatch):
    for name in ("DISCOGS_API_TOKEN", "DISCOGS_TOKEN", "LASTFM_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def _service(tmp_path: Path, monkeypatch, runner: FakeRunner, **kwargs) -> GenreService:
    payloads = _tiny_model(monkeypatch)

    def fetch(url, destination, on_bytes):
        destination.write_bytes(payloads[url.rsplit("/", 1)[-1]])
        on_bytes(destination.stat().st_size)

    async def fetch_bytes(url):
        return b"\x89PNG fake", "image/png"

    async def fetch_text(url):
        return SPOTIFY_PAGE

    return GenreService(
        tmp_path / "work",
        model_dir=tmp_path / "models",
        runner=runner,
        fetch_text=fetch_text,
        fetch_bytes=fetch_bytes,
        infer_factory=_fake_infer,
        model_fetch=fetch,
        background=False,
        **kwargs,
    )


def test_a_search_check_end_to_end_listening_only(tmp_path, monkeypatch, no_keys):
    runner = FakeRunner()
    service = _service(tmp_path, monkeypatch, runner)
    check = service.start("Double 99 - RIP Groove")
    data = check.public()
    assert data["status"] == "done", data["error"]
    result = data["result"]
    assert result["genres"][0]["name"] == "Speed Garage"
    assert result["parent"] == "Electronic"
    assert result["agreement"] == "heard-only"
    assert result["lookups_enabled"] == {"discogs": False, "lastfm": False}
    assert result["sources"]["discogs"]["status"] == "off"
    assert result["bpm"]["value"] == pytest.approx(128, abs=0.5)
    assert result["key"]["name"] == "A minor" and result["key"]["camelot"] == "8A"
    assert result["track"]["platform"] == "youtube"  # best-ranked candidate (the 30 s preview lost)
    assert result["track"]["art"]
    assert [s["key"] for s in data["steps"]] == ["model", "find", "listen", "lookup", "done"]
    assert data["steps"][3]["state"] == "skipped"  # look-ups off
    assert len(result["segments"]) == 3
    assert runner.count("ffmpeg") == 3
    assert service.model_ready()
    # Asking again is instant: nothing is searched, resolved or decoded.
    before = len(runner.calls)
    again = service.start("double 99 -  RIP groove").public()
    assert again["status"] == "done" and again["result"]["cached"] is True
    assert len(runner.calls) == before
    assert service.recent()[0]["genre"] == "Speed Garage"
    art = service.artwork_path(result["track"]["art"])
    assert art is not None and art.read_bytes().startswith(b"\x89PNG")


def test_a_copy_protected_match_falls_through_to_the_next(tmp_path, monkeypatch, no_keys):
    runner = FakeRunner(drm=("https://www.youtube.com/watch?v=uR3Vw8J8vUo",))
    service = _service(tmp_path, monkeypatch, runner)
    result = service.start("Double 99 - RIP Groove").public()["result"]
    assert result["track"]["platform"] == "soundcloud"


def test_a_pasted_link_is_analysed_directly_and_lookups_fuse(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCOGS_API_TOKEN", "disc-token")
    monkeypatch.setenv("LASTFM_API_KEY", "fm-key")
    calls: list[httpx.Request] = []
    tags = [{"name": "speed garage", "count": 100}, {"name": "ukg", "count": 50}]
    runner = FakeRunner()
    service = _service(
        tmp_path,
        monkeypatch,
        runner,
        lookups=Lookups(transport=_mock_transport(calls, track_tags=tags)),
    )
    data = service.start("https://www.youtube.com/watch?v=uR3Vw8J8vUo").public()
    result = data["result"]
    assert not any("search" in " ".join(c) for c in runner.calls)
    assert result["lookup_names"] == {"artist": "Double 99", "title": "RIP Groove"}
    assert result["agreement"] == "agree"
    assert result["genres"][0]["name"] == "Speed Garage"
    assert set(result["genres"][0]["sources"]) == {"heard", "discogs", "lastfm"}
    assert result["sources"]["lastfm"]["detail"] == "track tags"
    assert result["weights"]["discogs"] > 0 and result["weights"]["lastfm"] == 0.5


def test_a_spotify_link_searches_for_a_playable_copy(tmp_path, monkeypatch, no_keys):
    runner = FakeRunner()
    service = _service(tmp_path, monkeypatch, runner)
    result = service.start("https://open.spotify.com/track/6Sy9BUbgFse0n0LPA5lwy5").public()[
        "result"
    ]
    searches = [c[-1] for c in runner.calls if c[-1].startswith(("scsearch", "ytsearch"))]
    assert any("Darude - Sandstorm" in s for s in searches)
    assert result["lookup_names"] == {"artist": "Darude", "title": "Sandstorm"}


def test_picking_another_match_re_analyses_that_one(tmp_path, monkeypatch, no_keys):
    runner = FakeRunner()
    service = _service(tmp_path, monkeypatch, runner)
    first = service.start("Double 99 - RIP Groove").public()["result"]
    assert len(first["candidates"]) == 3
    picked = service.start(
        "Double 99 - RIP Groove", pick="https://soundcloud.com/x/ripgroove"
    ).public()
    assert picked["result"]["track"]["platform"] == "soundcloud"
    assert picked["result"]["lookup_names"]["title"] == "RIP Groove"
    with pytest.raises(source.SourceError):
        service.start("Double 99 - RIP Groove", pick="not a link")


def test_nothing_found_fails_in_plain_words(tmp_path, monkeypatch, no_keys):
    runner = FakeRunner(entries={"scsearch": [], "ytsearch": []})
    service = _service(tmp_path, monkeypatch, runner)
    data = service.start("zzzz qqqq").public()
    assert data["status"] == "failed"
    assert "Nothing matched" in data["error"]
    assert any(s["state"] == "failed" for s in data["steps"])


def test_artwork_is_only_fetched_from_known_image_hosts():
    assert artwork_allowed("https://i1.sndcdn.com/artworks-abc-t500x500.jpg")
    assert artwork_allowed("https://i.ytimg.com/vi/abc/mqdefault.jpg")
    assert not artwork_allowed("http://i.ytimg.com/vi/abc/mqdefault.jpg")
    assert not artwork_allowed("https://evil.example/sndcdn.com.jpg")
