"""BPM and musical key from a few short excerpts, in plain numpy.

**Tempo.**  An onset-strength envelope (positive spectral flux of the log spectrum, 100 frames a
second) is turned into a periodicity spectrum (its Fourier magnitude).  Each candidate tempo is
scored by the energy at its beat frequency and the next three multiples (bars of kicks, hats on
the eighths and sixteenths all agree with the true beat), summed over the excerpts.  A broad prior
centred on 125 BPM (one octave wide) breaks half/double ties; a heard style with a well-known
tempo band (Drum n Bass and Jungle at 160-185, Dubstep and Grime at 135-150, Hip Hop at 80-115)
picks the octave inside that band.  The winner is refined on a 0.01 BPM grid using the first eight
multiples, which sharpens the estimate well below the excerpt's natural resolution.

**Key.**  A faithful port of Essentia's ``KeyExtractor`` (spectral peaks, spectral whitening, a
12-bin harmonic pitch-class profile, then correlation with key profiles), checked against
Essentia's own C++ build on real tracks.  The ``edma`` profile (Faraldo et al. 2016, derived from
electronic dance music) gives the answer; Essentia's default ``bgate`` profile is computed too, and
when the two disagree the page says the key is uncertain.  Camelot notation is added for mixing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

SAMPLE_RATE = 16_000
_ONSET_FPS = 100
_ONSET_HOP = SAMPLE_RATE // _ONSET_FPS  # 160
_ONSET_FFT = 1024

PITCH_NAMES = ("C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B")
#: Camelot wheel: pitch class -> number, for minor (A) and major (B) keys.
_CAMELOT_MINOR = {8: 1, 3: 2, 10: 3, 5: 4, 0: 5, 7: 6, 2: 7, 9: 8, 4: 9, 11: 10, 6: 11, 1: 12}
_CAMELOT_MAJOR = {11: 1, 6: 2, 1: 3, 8: 4, 3: 5, 10: 6, 5: 7, 0: 8, 7: 9, 2: 10, 9: 11, 4: 12}
#: Essentia ``Key`` profile "edma" (major, minor), tonic first.
_EDMA_MAJOR = np.array([1.00, 0.29, 0.50, 0.40, 0.60, 0.56, 0.32, 0.80, 0.31, 0.45, 0.42, 0.39])
_EDMA_MINOR = np.array([1.00, 0.31, 0.44, 0.58, 0.33, 0.49, 0.29, 0.78, 0.43, 0.29, 0.53, 0.32])

#: Styles whose tempo band is distinctive enough to choose the octave (inclusive BPM range).
_STYLE_TEMPO = {
    "Electronic---Drum n Bass": (158.0, 186.0),
    "Electronic---Jungle": (155.0, 182.0),
    "Electronic---Dubstep": (132.0, 152.0),
    "Electronic---Grime": (130.0, 150.0),
    "Electronic---Halftime": (155.0, 182.0),
    "Electronic---Juke": (150.0, 168.0),
    "Electronic---Happy Hardcore": (160.0, 190.0),
    "Electronic---Gabber": (150.0, 220.0),
    "Electronic---Hardstyle": (140.0, 160.0),
    "Hip Hop---Boom Bap": (78.0, 112.0),
    "Hip Hop---Trap": (65.0, 90.0),
}


@dataclass(frozen=True)
class Tempo:
    bpm: float
    confidence: float


@dataclass(frozen=True)
class Key:
    tonic: str
    mode: str  # "major" | "minor"
    camelot: str
    confidence: float

    @property
    def name(self) -> str:
        return f"{self.tonic} {self.mode}"


def onset_envelope(signal: np.ndarray) -> np.ndarray:
    signal = np.asarray(signal, dtype=np.float64).reshape(-1)
    if signal.size < _ONSET_FFT * 2:
        return np.zeros(0)
    frames = np.lib.stride_tricks.sliding_window_view(signal, _ONSET_FFT)[::_ONSET_HOP]
    spectrum = np.abs(np.fft.rfft(frames * np.hanning(_ONSET_FFT), axis=1))
    log_spec = np.log1p(1000.0 * spectrum)
    flux = np.maximum(np.diff(log_spec, axis=0), 0.0).sum(axis=1)
    # Remove the slow loudness trend so the periodicity spectrum sees only the beat pattern.
    width = _ONSET_FPS  # one second
    kernel = np.ones(width) / width
    trend = np.convolve(flux, kernel, mode="same")
    envelope = np.maximum(flux - trend, 0.0)
    return envelope - envelope.mean()


_PAD = 1 << 17  # zero-padded FFT: 100 / 131072 Hz = 0.046 BPM between bins


def _periodicity(envelope: np.ndarray) -> np.ndarray:
    """|DFT| of the windowed envelope on a fine zero-padded grid, normalised by its length."""

    window = np.hanning(envelope.size)
    return np.abs(np.fft.rfft(envelope * window, n=_PAD)) / max(1.0, window.sum())


def _magnitude_at(spectrum: np.ndarray, freqs_hz: np.ndarray) -> np.ndarray:
    """The periodicity spectrum at arbitrary frequencies (Hz), by linear interpolation: the same
    values as evaluating the DFT at each frequency directly, to well under a percent."""

    bins = freqs_hz * _PAD / _ONSET_FPS
    return np.interp(bins, np.arange(spectrum.size), spectrum)


def _comb(envelopes: Sequence[np.ndarray], bpms: np.ndarray, harmonics: int) -> np.ndarray:
    weights = [1.0, 1.0, 0.5, 0.5, 0.25, 0.25, 0.25, 0.25][:harmonics]
    score = np.zeros(bpms.size)
    for envelope in envelopes:
        if envelope.size < 4 * _ONSET_FPS:
            continue
        energy = float(np.sqrt(np.mean(envelope**2))) or 1.0
        spectrum = _periodicity(envelope)
        for k, weight in enumerate(weights, start=1):
            freqs = bpms * k / 60.0
            ok = freqs < _ONSET_FPS / 2
            part = np.zeros(bpms.size)
            part[ok] = _magnitude_at(spectrum, freqs[ok])
            score += weight * part / energy
    return score


def estimate_tempo(segments: Sequence[np.ndarray], style_hint: str | None = None) -> Tempo | None:
    envelopes = [onset_envelope(s) for s in segments]
    envelopes = [e for e in envelopes if e.size >= 4 * _ONSET_FPS and np.any(e)]
    if not envelopes:
        return None
    grid = np.arange(60.0, 200.0, 0.25)
    score = _comb(envelopes, grid, harmonics=4)
    if not np.any(score > 0):
        return None
    prior = np.exp(-0.5 * (np.log2(grid / 125.0) / 1.0) ** 2)
    band = _STYLE_TEMPO.get(style_hint or "")
    if band is not None:
        inside = (grid >= band[0]) & (grid <= band[1])
        prior = np.where(inside, prior * 3.0, prior)
    weighted = score * prior
    best = int(np.argmax(weighted))
    coarse = float(grid[best])
    if band is None and coarse > 160.0:
        # Off-beat hats make a 90-100 BPM groove look twice as fast; outside a fast style's band,
        # the half tempo wins when its own evidence is at least half as strong.
        half = int(np.argmin(np.abs(grid - coarse / 2.0)))
        if score[half] >= 0.5 * score[best]:
            coarse = float(grid[half])
    if band is not None and not band[0] <= coarse <= band[1]:
        for factor in (2.0, 0.5, 1.5, 2.0 / 3.0):
            if band[0] <= coarse * factor <= band[1]:
                coarse *= factor
                break
    fine_grid = np.arange(coarse * 0.97, coarse * 1.03, 0.01)
    fine = _comb(envelopes, fine_grid, harmonics=8)
    bpm = float(fine_grid[int(np.argmax(fine))])
    # Confidence: how clearly the winner beats the best tempo that is not it or its octave
    # (1 = nothing else comes close, 0 = a different tempo scores as high).
    related = np.zeros(grid.size, dtype=bool)
    for factor in (0.5, 1.0, 2.0):
        related |= np.abs(grid / (bpm * factor) - 1.0) < 0.04
    rival = float(weighted[~related].max()) if np.any(~related) else 0.0
    confidence = float(np.clip(1.0 - rival / float(weighted.max()), 0.0, 1.0))
    if abs(bpm - round(bpm)) < 0.15:
        bpm = float(round(bpm))
    return Tempo(round(bpm, 1), round(confidence, 2))


# ------------------------------------------------------------------------------------------
# Key: a port of Essentia's KeyExtractor (FrameCutter -> Windowing(hann, normalised) -> Spectrum
# -> SpectralPeaks -> SpectralWhitening -> HPCP(12, harmonics=4, cosine, 1 semitone, 25..3500 Hz)
# -> mean over frames -> peak-normalise, gate below 0.2 -> Key(profile, no polyphony)).
# ------------------------------------------------------------------------------------------
KEY_FRAME = 4096
KEY_MIN_HZ = 25.0
KEY_MAX_HZ = 3500.0
_KEY_NAMES_FROM_A = ("A", "Bb", "B", "C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab")
_SILENCE_DB = -100.0


def _lin2db(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=np.float64)
    out = np.full(values.shape, _SILENCE_DB)
    ok = values >= 1e-10
    out[ok] = 10.0 * np.log10(values[ok])
    return out


def _harmonic_table(harmonics: int = 4) -> list[tuple[float, float]]:
    table: list[list[float]] = []
    for i in range(harmonics + 1):
        semitone = 12.0 * float(np.log2(i + 1.0))
        octweight = max(1.0, (semitone / 12.0) * 0.5)
        while semitone >= 12.0 - 1e-5:
            semitone -= 12.0
        for entry in table:
            if abs(entry[0] - semitone) < 1e-5:
                entry[1] += 1.0 / octweight
                break
        else:
            table.append([semitone, 1.0 / octweight])
    return [(a, b) for a, b in table]


_HARMONICS = _harmonic_table()


def _spectral_peaks(spectrum: np.ndarray, max_peaks: int = 60) -> tuple[np.ndarray, np.ndarray]:
    """Essentia ``SpectralPeaks``: interpolated local maxima in 25..3500 Hz, loudest 60."""

    size = spectrum.size
    scale = (SAMPLE_RATE / 2.0) / (size - 1)
    lo = max(1, int(np.ceil(KEY_MIN_HZ / scale)))
    a = spectrum
    idx = np.arange(lo, size - 1)
    is_peak = (a[idx] > a[idx - 1]) & (a[idx] > a[idx + 1]) & (a[idx] > 1e-4)
    idx = idx[is_peak]
    left, mid, right = a[idx - 1], a[idx], a[idx + 1]
    denominator = left - 2 * mid + right
    safe = np.where(denominator != 0, denominator, 1.0)
    delta = np.where(denominator != 0, 0.5 * (left - right) / safe, 0.0)
    position = (idx + delta) * scale
    value = mid - 0.25 * (left - right) * delta
    keep = position <= KEY_MAX_HZ
    position, value = position[keep], value[keep]
    order = np.lexsort((position, -value))[:max_peaks]
    return position[order], value[order]


def _whiten(spectrum: np.ndarray, freqs: np.ndarray, mags: np.ndarray) -> np.ndarray:
    """Essentia ``SpectralWhitening`` (maxFrequency 3500, so the envelope reaches 4200 Hz)."""

    if mags.size == 0:
        return mags
    max_freq = KEY_MAX_HZ * 1.2
    spectral_range = SAMPLE_RATE / 2.0
    size = spectrum.size
    mags_db = 2.0 * _lin2db(mags)
    energy = spectrum * spectrum
    xs: list[float] = []
    ys: list[float] = []
    freq = 0.0
    while freq <= max_freq and freq <= spectral_range:
        bf = freq - max(50.0, freq * 0.34)
        ef = freq + max(50.0, freq * 0.58)
        b = int(bf / spectral_range * (size - 1.0) + 0.5)
        e = int(ef / spectral_range * (size - 1.0) + 0.5)
        b = min(size - 1, max(b, 0))
        e = min(size, max(e, b + 1))
        c = b / 2.0 + e / 2.0
        half = e - c
        i = np.arange(b, e)
        weight = (1.0 - np.abs(i - c) / half) ** 4 * energy[b:e]
        n = float(weight.sum())
        xs.append(freq)
        ys.append(float((energy[b:e] * weight).sum() / n) if n != 0 else 0.0)
        freq += 100.0
    ys[-1] = ys[-2]
    env_db = 2.0 * _lin2db(np.sqrt(np.array(ys)))
    env = np.interp(freqs, xs, env_db)
    # Essentia also computes a "40 dB under the loudest peak" floor, but the next branch always
    # overwrites it, so it has no effect; it is left out here.
    value = (
        np.where(mags_db > env, 0.0, np.where(mags_db > env - 30.0, mags_db - env, -200.0))
        - 20.0 * freqs / 4000.0
    )
    value = np.where(freqs > max_freq - 100.0, mags_db, value)
    return np.power(10.0, value / 2.0 / 10.0)


_SHIFTS = np.array([2.0 ** (-semitone / 12.0) for semitone, _ in _HARMONICS])
_STRENGTHS = np.array([strength for _, strength in _HARMONICS])


def _hpcp(freqs: np.ndarray, mags: np.ndarray) -> np.ndarray:
    """Essentia ``HPCP``: 12 bins from A (440 Hz), 4 harmonics, cosine weight over 1 semitone."""

    keep = (freqs >= KEY_MIN_HZ) & (freqs <= KEY_MAX_HZ)
    freqs, mags = freqs[keep], mags[keep]
    hpcp = np.zeros(12)
    if freqs.size == 0:
        return hpcp
    fundamentals = np.outer(freqs, _SHIFTS)  # [peaks, harmonics]
    position = np.log2(fundamentals / 440.0) * 12.0
    contribution = (mags[:, None] ** 2) * (_STRENGTHS[None, :] ** 2)
    left = np.ceil(position - 0.5)
    right = np.floor(position + 0.5)
    for offset in (0, 1):
        bins = left + offset
        valid = bins <= right
        weight = np.cos(np.pi * np.abs(position - bins))
        np.add.at(hpcp, (bins[valid].astype(int)) % 12, (weight * contribution)[valid])
    return hpcp


def hpcp_frames(signal: np.ndarray) -> list[np.ndarray]:
    """Per-frame HPCPs for one excerpt (frames of 4096 every 4096, centred like FrameCutter)."""

    signal = np.asarray(signal, dtype=np.float64).reshape(-1)
    if signal.size < KEY_FRAME:
        return []
    half = KEY_FRAME // 2
    count = -(-signal.size // KEY_FRAME) + 1
    padded = np.zeros(half + count * KEY_FRAME + KEY_FRAME)
    padded[half : half + signal.size] = signal
    window = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(KEY_FRAME) / (KEY_FRAME - 1))
    window = window * 2.0 / window.sum()  # Essentia's "normalized" window
    spectra = np.abs(
        np.fft.rfft(
            np.lib.stride_tricks.sliding_window_view(padded, KEY_FRAME)[::KEY_FRAME][:count]
            * window,
            axis=1,
        )
    )
    frames: list[np.ndarray] = []
    for spectrum in spectra:
        freqs, mags = _spectral_peaks(spectrum)
        frames.append(_hpcp(freqs, _whiten(spectrum, freqs, mags)))
    return frames


#: Essentia ``Key`` profiles (major, minor) with the tonic first: "edma" (Faraldo et al. 2016,
#: tuned on electronic dance music) and "bgate" (Essentia's default).
_PROFILES = {
    "edma": (_EDMA_MAJOR, _EDMA_MINOR),
    "bgate": (
        np.array([1.00, 0.00, 0.42, 0.00, 0.53, 0.37, 0.00, 0.77, 0.00, 0.38, 0.21, 0.30]),
        np.array([1.00, 0.00, 0.36, 0.39, 0.00, 0.38, 0.00, 0.74, 0.27, 0.00, 0.42, 0.23]),
    ),
}
_PITCH_FROM_NAME = {name: i for i, name in enumerate(PITCH_NAMES)}


def key_from_pcp(pcp: np.ndarray, profile: str = "edma") -> tuple[int, str, float, float]:
    """Essentia ``Key`` on an averaged 12-bin PCP: (tonic index from A, mode, strength, margin)."""

    pcp = np.asarray(pcp, dtype=np.float64)
    if pcp.max() > 0:
        pcp = pcp / pcp.max()
    pcp = np.where(pcp < 0.2, 0.0, pcp)
    centred = pcp - pcp.mean()
    std = float(np.sqrt((centred**2).sum()))
    best: dict[str, tuple[float, float, int]] = {}
    for mode, prof in zip(("major", "minor"), _PROFILES[profile], strict=True):
        pc = prof - prof.mean()
        pstd = float(np.sqrt((pc**2).sum()))
        first, second, index = -1.0, -1.0, -1
        for shift in range(12):
            r = float((centred * np.roll(pc, shift)).sum() / (std * pstd)) if std and pstd else 0.0
            if r > first:
                second, first, index = first, r, shift
        best[mode] = (first, second, index)
    mode = "major" if best["major"][0] > best["minor"][0] else "minor"
    strength, second, index = best[mode]
    margin = (strength - second) / strength if strength > 0 else 0.0
    return index, mode, strength, margin


def estimate_key(segments: Sequence[np.ndarray], profile: str = "edma") -> Key | None:
    return estimate_keys(segments, (profile,))[0]


def estimate_keys(
    segments: Sequence[np.ndarray], profiles: Sequence[str] = ("edma", "bgate")
) -> list[Key | None]:
    """The key under each profile, from one shared pass over the audio."""

    frames = [frame for segment in segments for frame in hpcp_frames(segment)]
    if not frames:
        return [None for _ in profiles]
    average = np.mean(frames, axis=0)
    if average.max() <= 0:
        return [None for _ in profiles]
    return [_key_for(average, profile) for profile in profiles]


def _key_for(average: np.ndarray, profile: str) -> Key:
    index, mode, strength, _ = key_from_pcp(average, profile)
    tonic = _KEY_NAMES_FROM_A[index]
    pitch = _PITCH_FROM_NAME[tonic]
    number = (_CAMELOT_MAJOR if mode == "major" else _CAMELOT_MINOR)[pitch]
    camelot = f"{number}{'B' if mode == 'major' else 'A'}"
    return Key(tonic, mode, camelot, round(float(np.clip(strength, 0.0, 1.0)), 2))
