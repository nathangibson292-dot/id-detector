"""Hermetic coverage for local playlists; all identities and metadata are synthetic."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from id_detector.playlists import handlers, row_actions_html, store, view


@pytest.fixture(autouse=True)
def isolated_store(monkeypatch):
    monkeypatch.delenv("IDEA_PLAYLISTS_PATH", raising=False)


@pytest.fixture
def track():
    return {
        "candidate_id": "candidate-a",
        "artist": "Fake artist",
        "title": "Fake track",
        "badge": "likely",
        "version_status": "unverified",
        "acquire": None,
        "source": {
            "source_key": "aa",
            "media_key": "bb",
            "episode_id": "episode-a",
            "start_ms": 1200,
            "mix_title": "Fake mix",
        },
    }


def test_default_and_roundtrip(tmp_path):
    path = tmp_path / "local/playlists.json"
    data = store.load_store(path)
    assert data == {
        "version": 1,
        "playlists": [
            {"id": "likes", "name": "Likes", "builtin": True, "created_ms": 0, "tracks": []}
        ],
    }
    assert store.ensure_likes(data) == store.ensure_likes(copy.deepcopy(data))
    store.save_store(path, data)
    assert store.load_store(path) == data
    for corrupt in ("broken", "null", '{"version":1,"playlists":[{}]}'):
        path.write_text(corrupt, encoding="utf-8")
        assert store.load_store(path) == data


def test_paths(tmp_path, monkeypatch):
    assert store.default_store_path(tmp_path / "work") == tmp_path / "data/local/playlists.json"
    override = tmp_path / "alternate/lists.json"
    monkeypatch.setenv("IDEA_PLAYLISTS_PATH", str(override))
    assert store.default_store_path(tmp_path / "work") == override
    assert override.parent.is_dir()
    monkeypatch.setenv("IDEA_PLAYLISTS_PATH", "relative.json")
    with pytest.raises(ValueError):
        store.default_store_path(tmp_path)


def test_playlist_lifecycle():
    data, key = store.create_playlist({}, "  Fake list  ")
    assert store.get_playlist(data, key)["name"] == "Fake list"
    _, duplicate = store.create_playlist(data, "Fake list")
    assert duplicate != key
    store.rename_playlist(data, key, "Renamed")
    assert store.get_playlist(data, key)["name"] == "Renamed"
    store.delete_playlist(data, key)
    assert store.get_playlist(data, key) is None
    for operation in (
        lambda: store.create_playlist(data, "  "),
        lambda: store.rename_playlist(data, duplicate, " "),
        lambda: store.rename_playlist(data, "likes", "Other"),
        lambda: store.delete_playlist(data, "likes"),
    ):
        with pytest.raises(ValueError):
            operation()
    assert store.list_playlists(data)[0]["id"] == "likes"


def test_tracks_and_state(track):
    data, key = store.create_playlist({}, "Synthetic")
    store.add_track(data, key, track)
    original = copy.deepcopy(data)
    store.add_track(data, key, track)
    assert data == original
    second = copy.deepcopy(track)
    second["source"]["episode_id"] = "episode-b"
    second["title"] = "Updated title"
    store.add_track(data, key, second)
    saved = store.get_playlist(data, key)["tracks"][0]
    assert len(saved["sources"]) == 2
    assert saved["title"] == "Updated title"
    assert store.toggle_like(data, track)[1] is True
    state = store.state(data)
    assert set(state) == {"playlists", "liked", "membership"}
    assert state["liked"] == ["candidate-a"]
    assert state["membership"] == {"candidate-a": ["likes", key]}
    assert store.toggle_like(data, track)[1] is False
    store.remove_track(data, key, "candidate-a")
    assert store.state(data)["membership"] == {}


@pytest.mark.parametrize(
    "keys",
    [
        ("..", "bb", "episode-a"),
        ("aa", "../bb", "episode-a"),
        ("aa", "bb", "../episode"),
        ("", "bb", "episode"),
    ],
)
def test_snapshot_rejects_traversal(tmp_path, keys):
    with pytest.raises(ValueError):
        handlers.snapshot_track(tmp_path, *keys)


def test_snapshot_reads_flat_entries(tmp_path, track):
    media = tmp_path / "aa/bb"
    (media / "present").mkdir(parents=True)
    entry = {**track, "kind": "track", "episode_id": "episode-a", "start_ms": 1200}
    (media / "present/tracklist.json").write_text(json.dumps({"entries": [entry]}))
    assert handlers.snapshot_track(tmp_path, "aa", "bb", "episode-a")["title"] == "Fake track"
    assert handlers.snapshot_track(tmp_path, "aa", "bb", "missing") is None


def test_views_escape_and_empty(track):
    data, key = store.create_playlist({}, '<Fake & "list">')
    assert b"&lt;Fake &amp; &quot;list&quot;&gt;" in view.render_index(data)
    assert b"&lt;Fake &amp; &quot;list&quot;&gt;" in view.render_playlist(data, key)
    assert view.render_playlist(data, "missing") is None
    assert isinstance(view.render_index({}), bytes)
    assert isinstance(view.render_playlist({}, "likes"), bytes)
    store.add_track(data, key, track)
    page = view.render_playlist(data, key)
    assert b"badge-likely" in page
    assert b"/aa/bb/present/index.html#episode-a" in page
    assert b'data-action="delete"' not in view.render_playlist(data, "likes")
    assert "&quot;" in row_actions_html({"candidate_id": 'fake"', "episode_id": "episode"})
    assert 'data-candidate-id="episode"' in row_actions_html({"episode_id": "episode"})
    assert "javascript:" not in view.acquire_links_html(
        {"direct": [{"kind": "stream", "url": "javascript:alert(1)"}]}
    )


def test_handlers_flow(tmp_path, monkeypatch, track):
    work = tmp_path / "work"
    monkeypatch.setattr(handlers, "snapshot_track", lambda *args: copy.deepcopy(track))

    def post(action, **form):
        status, body, content_type = handlers.handle_post(
            "/playlists/" + action, form, work_root=work
        )
        assert content_type == "application/json"
        return status, json.loads(body)

    status, created = post("create", name="Synthetic")
    assert status == 200
    fields = {
        "source_key": "aa",
        "media_key": "bb",
        "episode_id": "episode-a",
        "candidate_id": "candidate-a",
    }
    assert post("add", **fields, playlist_id=created["id"])[0] == 200
    assert post("like", **fields)[1]["liked"] is True
    status, body, _ = handlers.handle_get("/playlists/state", {}, work_root=work)
    state = json.loads(body)
    assert status == 200 and set(state) == {"playlists", "liked", "membership"}
    assert set(state["membership"]["candidate-a"]) == {"likes", created["id"]}
    assert post("remove", playlist_id=created["id"], candidate_id="candidate-a")[0] == 200
    assert post("add", **fields, playlist_id="__new__", name="Second")[0] == 200
    assert post("rename", playlist_id="likes", name="Other")[0] == 400
    assert post("delete", playlist_id="missing")[0] == 404
    assert post("like")[0] == 400
    assert post("unknown")[0] == 405
    assert handlers.handle_get("/playlists/xxx", {}, work_root=work)[0] == 404
    assert handlers.handle_get("/playlists/p/missing", {}, work_root=work)[0] == 404
    assert handlers.handle_get("/playlists", {}, work_root=work)[0] == 200
    assert post("like", **{**fields, "candidate_id": "stale"})[0] == 400
    monkeypatch.setattr(handlers, "snapshot_track", lambda *args: None)
    assert post("like", **fields)[0] == 404


def test_concurrent_handler_mutations(tmp_path):
    work = tmp_path / "work"

    def create(number):
        return handlers.handle_post("/playlists/create", {"name": f"Fake {number}"}, work_root=work)

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(result[0] == 200 for result in pool.map(create, range(12)))
    data = store.load_store(store.default_store_path(work))
    assert len(data["playlists"]) == 13


def test_result_controls_only_on_visible_tracks(track):
    from id_detector.present.page import _track_row_html

    entry = {
        **track,
        "episode_id": "episode-a",
        "start_ms": 1200,
        "primary_role": "dominant",
        "hint_supported": False,
    }
    visible = _track_row_html(entry, "file")
    assert 'class="pl-actions"' in visible
    assert 'id="episode-a"' in visible
    assert 'class="pl-actions"' not in _track_row_html(entry, "file", hidden="short")


def test_server_shim_routes_and_guards_playlists(tmp_path, monkeypatch):
    """End-to-end through the real stdlib server: routing, the CSRF guard, and handler wiring."""
    import httpx

    from id_detector.present.server import serve_in_background

    monkeypatch.setenv("IDEA_PLAYLISTS_PATH", str(tmp_path / "playlists.json"))
    running = serve_in_background(tmp_path, port=0)
    try:
        base = running.base_url
        # GET index + state are served even with the analyse app inactive.
        assert httpx.get(base + "/playlists", timeout=10).status_code == 200
        state = httpx.get(base + "/playlists/state", timeout=10)
        assert state.status_code == 200
        assert set(state.json()) == {"playlists", "liked", "membership"}

        token = httpx.get(base + "/csrf", timeout=10).json()["token"]
        # A mutation without the CSRF token is refused (loopback Host passes; the token gate fails).
        refused = httpx.post(base + "/playlists/create", data={"name": "No token"}, timeout=10)
        assert refused.status_code == 403
        # With the token it succeeds and the new playlist appears in state.
        created = httpx.post(
            base + "/playlists/create",
            data={"name": "My set"},
            headers={"X-CSRF-Token": token},
            timeout=10,
        )
        assert created.status_code == 200 and created.json()["ok"] is True
        names = [
            p["name"] for p in httpx.get(base + "/playlists/state", timeout=10).json()["playlists"]
        ]
        assert "My set" in names

        # Unknown action -> 405; unknown GET under /playlists -> 404.
        bogus = httpx.post(
            base + "/playlists/bogus", data={}, headers={"X-CSRF-Token": token}, timeout=10
        )
        assert bogus.status_code == 405
        assert httpx.get(base + "/playlists/nope", timeout=10).status_code == 404
    finally:
        running.shutdown()
