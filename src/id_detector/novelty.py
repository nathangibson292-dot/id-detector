"""Spectral-novelty change points computed locally from the canonical PCM.

The plan asks for "*spectral-novelty change points: compute locally from the canonical PCM (e.g.
log-mel flux with a 3 s median baseline; peaks above a configurable z-score)*".  This module is
exactly that and nothing more: it never leaves the machine, needs no provider, and returns integer
millisecond positions so the result can go straight into an artefact.

The detector is deliberately simple and deterministic:

1. 128 ms Hann-windowed frames at a 100 ms hop over the 16 kHz mono canonical PCM;
2. a 40-band triangular mel filterbank, ``log1p`` compressed;
3. half-wave-rectified spectral flux between consecutive frames;
4. a 3 s centred **median** baseline and a median-absolute-deviation scale, giving a robust
   z-score per frame;
5. peaks above ``z_threshold`` that are the local maximum of their own neighbourhood and at least
   ``min_separation_ms`` apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from id_detector.decode import BYTES_PER_SAMPLE, SAMPLE_RATE
from id_detector.io import native_path

FRAME_MS = 128
HOP_MS = 100
MEL_BANDS = 40
MEL_LOW_HZ = 40
MEL_HIGH_HZ = 7_800
BASELINE_MS = 3_000
DEFAULT_Z_THRESHOLD_E4 = 30_000
DEFAULT_MIN_SEPARATION_MS = 4_000
DEFAULT_MAX_EVENTS = 512
_READ_BLOCK_SAMPLES = 1 << 20
#: Frames transformed per FFT batch. A two-hour set is ~72,000 frames; batching keeps the peak
#: allocation at a few tens of megabytes instead of gigabytes.
_FRAME_BATCH = 2_048


@dataclass(frozen=True)
class NoveltyEvent:
    at_ms: int
    z_e4: int


def _hz_to_mel(value: np.ndarray | float) -> np.ndarray | float:
    return 2595.0 * np.log10(1.0 + np.asarray(value, dtype=np.float64) / 700.0)


def _mel_to_hz(value: np.ndarray) -> np.ndarray:
    return 700.0 * (10.0 ** (value / 2595.0) - 1.0)


def _mel_filterbank(n_fft: int, sample_rate: int) -> np.ndarray:
    bins = n_fft // 2 + 1
    edges = _mel_to_hz(np.linspace(_hz_to_mel(MEL_LOW_HZ), _hz_to_mel(MEL_HIGH_HZ), MEL_BANDS + 2))
    positions = np.floor((n_fft + 1) * edges / sample_rate).astype(np.int64)
    positions = np.clip(positions, 0, bins - 1)
    filters = np.zeros((MEL_BANDS, bins), dtype=np.float64)
    for band in range(MEL_BANDS):
        left, centre, right = positions[band], positions[band + 1], positions[band + 2]
        if centre <= left:
            centre = min(left + 1, bins - 1)
        if right <= centre:
            right = min(centre + 1, bins - 1)
        if centre > left:
            filters[band, left:centre] = np.linspace(0.0, 1.0, centre - left, endpoint=False)
        if right > centre:
            filters[band, centre:right] = np.linspace(1.0, 0.0, right - centre, endpoint=False)
    return filters


def log_mel_frames(samples: np.ndarray, *, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Return the ``(frames, MEL_BANDS)`` log-mel matrix of one mono float signal."""

    frame_length = max(2, sample_rate * FRAME_MS // 1_000)
    hop_length = max(1, sample_rate * HOP_MS // 1_000)
    if samples.size < frame_length:
        return np.zeros((0, MEL_BANDS), dtype=np.float64)
    count = 1 + (samples.size - frame_length) // hop_length
    strided = np.lib.stride_tricks.as_strided(
        samples,
        shape=(count, frame_length),
        strides=(samples.strides[0] * hop_length, samples.strides[0]),
        writeable=False,
    )
    window = np.hanning(frame_length)
    filters = _mel_filterbank(frame_length, sample_rate).T
    result = np.empty((count, MEL_BANDS), dtype=np.float64)
    for start in range(0, count, _FRAME_BATCH):
        end = min(count, start + _FRAME_BATCH)
        spectrum = np.abs(np.fft.rfft(strided[start:end] * window, axis=1))
        result[start:end] = np.log1p(spectrum @ filters)
    return result


def spectral_flux(log_mel: np.ndarray) -> np.ndarray:
    """Half-wave-rectified log-mel flux; entry ``i`` describes the step into frame ``i + 1``."""

    if log_mel.shape[0] < 2:
        return np.zeros(0, dtype=np.float64)
    difference = np.diff(log_mel, axis=0)
    return np.maximum(difference, 0.0).sum(axis=1)


def flux_change_points(
    flux: np.ndarray,
    *,
    z_threshold_e4: int = DEFAULT_Z_THRESHOLD_E4,
    min_separation_ms: int = DEFAULT_MIN_SEPARATION_MS,
    max_events: int = DEFAULT_MAX_EVENTS,
    hop_ms: int = HOP_MS,
) -> tuple[NoveltyEvent, ...]:
    """Return robust z-scored peaks of one flux curve as integer millisecond positions."""

    if flux.size == 0:
        return ()
    baseline_frames = max(3, BASELINE_MS // max(1, hop_ms))
    half = baseline_frames // 2
    padded = np.pad(flux, (half, half), mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(padded, baseline_frames)[: flux.size]
    median = np.median(windows, axis=1)
    deviation = np.median(np.abs(windows - median[:, None]), axis=1)
    scale = np.maximum(1.4826 * deviation, 1e-6)
    z_scores = (flux - median) / scale

    threshold = z_threshold_e4 / 10_000.0
    separation_frames = max(1, min_separation_ms // max(1, hop_ms))
    candidates = [
        index
        for index in range(flux.size)
        if z_scores[index] >= threshold
        and flux[index]
        >= np.max(
            flux[max(0, index - separation_frames) : index + separation_frames + 1],
            initial=0.0,
        )
    ]
    events: list[NoveltyEvent] = []
    for index in candidates:
        # entry i is the step *into* frame i + 1, whose start is (i + 1) * hop_ms.
        at_ms = (index + 1) * hop_ms
        if events and at_ms - events[-1].at_ms < min_separation_ms:
            if z_scores[index] <= events[-1].z_e4 / 10_000.0:
                continue
            events.pop()
        events.append(NoveltyEvent(at_ms=at_ms, z_e4=int(round(z_scores[index] * 10_000))))
    if len(events) > max_events:
        events = sorted(
            sorted(events, key=lambda item: (-item.z_e4, item.at_ms))[:max_events],
            key=lambda item: item.at_ms,
        )
    return tuple(events)


def _read_pcm(pcm_path: Path) -> np.ndarray:
    with open(native_path(pcm_path), "rb") as handle:
        blocks: list[np.ndarray] = []
        while True:
            payload = handle.read(_READ_BLOCK_SAMPLES * BYTES_PER_SAMPLE)
            if not payload:
                break
            usable = len(payload) - (len(payload) % BYTES_PER_SAMPLE)
            blocks.append(np.frombuffer(payload[:usable], dtype="<i2"))
    if not blocks:
        return np.zeros(0, dtype=np.float64)
    return np.concatenate(blocks).astype(np.float64) / 32768.0


def novelty_change_points(
    pcm_path: Path,
    *,
    z_threshold_e4: int = DEFAULT_Z_THRESHOLD_E4,
    min_separation_ms: int = DEFAULT_MIN_SEPARATION_MS,
    max_events: int = DEFAULT_MAX_EVENTS,
    duration_ms: int | None = None,
) -> tuple[NoveltyEvent, ...]:
    """Compute change points directly from the canonical 16 kHz mono s16le PCM file."""

    samples = _read_pcm(pcm_path)
    events = flux_change_points(
        spectral_flux(log_mel_frames(samples)),
        z_threshold_e4=z_threshold_e4,
        min_separation_ms=min_separation_ms,
        max_events=max_events,
    )
    if duration_ms is None:
        return events
    return tuple(item for item in events if item.at_ms <= duration_ms)
