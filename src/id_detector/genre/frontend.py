"""Essentia's MusiCNN-style mel-spectrogram, reproduced in numpy for the Discogs-EffNet model.

The Discogs-EffNet model was trained on the exact input that Essentia's
``TensorflowPredictEffnetDiscogs`` builds, so this module copies that pipeline step by step
(Essentia ``master``: ``tensorflowpredicteffnetdiscogs.{h,cpp}``, ``tensorflowinputmusicnn.cpp``,
``framecutter.cpp``, ``windowing.cpp``, ``melbands.cpp``, ``triangularbands.cpp``,
``unaryoperator.cpp``, ``vectorrealtotensor.cpp``):

* audio: mono, 16 kHz, float in [-1, 1] (``MonoLoader(sampleRate=16000)``);
* ``FrameCutter(frameSize=512, hopSize=256)``, default ``startFromZero=False``: the first frame
  starts 256 samples *before* the signal (zero-padded, centred on sample 0) and the last frame is
  the first whose centre reaches the end of the signal;
* ``Windowing(type="hann", normalized=False)``: the symmetric Hann ``0.5 - 0.5 cos(2 pi i/(N-1))``
  (zero-phase rotation does not change a magnitude spectrum);
* ``Spectrum(size=512)``: the magnitude of the 257-bin real FFT;
* ``MelBands(96 bands, 0..8000 Hz, warpingFormula="slaneyMel", weighting="linear",
  normalize="unit_tri", type="power")``: triangles whose edges are equally spaced on the Slaney mel
  scale, applied to the *squared* magnitude, each divided by its theoretical area (half its width
  in Hz);
* ``UnaryOperator(shift=1, scale=10000)`` then ``log10`` (clipped at 1e-30): ``log10(1 + 10000 x)``;
* ``VectorRealToTensor``: patches of 128 frames every 62 frames, an incomplete last patch
  discarded (``lastPatchMode="discard"``), shaped ``[patches, 128, 96]`` (time by mel band).

Proof that this matches Essentia bit-for-bit within float32 noise is in
``docs/reviews/build-genre.md`` (compared against Essentia's own C++ compiled to WebAssembly, and
against Essentia's published regression values).
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np

SAMPLE_RATE = 16_000
FRAME_SIZE = 512
HOP_SIZE = 256
N_BANDS = 96
PATCH_SIZE = 128
PATCH_HOP = 62


def _hz_to_mel_slaney(hz: np.ndarray) -> np.ndarray:
    hz = np.asarray(hz, dtype=np.float64)
    lin_slope = 3.0 / 200.0
    log_step = np.log(6.4) / 27.0
    return np.where(
        hz < 1000.0,
        hz * lin_slope,
        1000.0 * lin_slope + np.log(np.maximum(hz, 1e-12) / 1000.0) / log_step,
    )


def _mel_to_hz_slaney(mel: np.ndarray) -> np.ndarray:
    mel = np.asarray(mel, dtype=np.float64)
    lin_slope = 3.0 / 200.0
    min_log_mel = 1000.0 * lin_slope
    log_step = np.log(6.4) / 27.0
    return np.where(
        mel < min_log_mel, mel / lin_slope, 1000.0 * np.exp((mel - min_log_mel) * log_step)
    )


@lru_cache(maxsize=1)
def mel_filterbank() -> np.ndarray:
    """The ``[257, 96]`` matrix Essentia's ``TriangularBands`` builds for this configuration."""

    spectrum_size = FRAME_SIZE // 2 + 1
    # Essentia computes in float32 ("Real"): keep the band edges in float32 like it does.
    low, high = _hz_to_mel_slaney(np.array([0.0, SAMPLE_RATE / 2]))
    increment = (high - low) / (N_BANDS + 1)
    edges_mel = low + increment * np.arange(N_BANDS + 2)
    edges = _mel_to_hz_slaney(edges_mel).astype(np.float32).astype(np.float64)
    scale = (SAMPLE_RATE / 2.0) / (spectrum_size - 1)
    bank = np.zeros((spectrum_size, N_BANDS), dtype=np.float64)
    for i in range(N_BANDS):
        f0, f1, f2 = edges[i], edges[i + 1], edges[i + 2]
        step1, step2 = f1 - f0, f2 - f1
        jbegin = int(np.ceil(f0 / scale))
        jend = int(np.floor(f2 / scale))
        for j in range(jbegin, jend + 1):
            freq = j * scale
            bank[j, i] = (freq - f0) / step1 if freq < f1 else (f2 - freq) / step2
        bank[jbegin : jend + 1, i] /= (step1 + step2) / 2.0  # "unit_tri"
    return bank


@lru_cache(maxsize=1)
def _hann() -> np.ndarray:
    i = np.arange(FRAME_SIZE, dtype=np.float64)
    return 0.5 - 0.5 * np.cos(2.0 * np.pi * i / (FRAME_SIZE - 1.0))


def frame_count(n_samples: int) -> int:
    """How many frames Essentia's ``FrameCutter`` emits for a signal of this length."""

    if n_samples <= 0:
        return 0
    # Frame k starts at 256 k - 256; the last is the first whose centre (start + 256) >= length.
    return -(-n_samples // HOP_SIZE) + 1


def melspectrogram(signal: np.ndarray) -> np.ndarray:
    """``[frames, 96]`` log-compressed mel bands, as ``TensorflowInputMusiCNN`` makes them."""

    signal = np.asarray(signal, dtype=np.float32).reshape(-1)
    count = frame_count(signal.size)
    if count == 0:
        return np.zeros((0, N_BANDS), dtype=np.float32)
    half = FRAME_SIZE // 2
    padded = np.zeros(half + count * HOP_SIZE + FRAME_SIZE, dtype=np.float32)
    padded[half : half + signal.size] = signal
    windows = np.lib.stride_tricks.sliding_window_view(padded, FRAME_SIZE)[::HOP_SIZE][:count]
    spectrum = np.abs(np.fft.rfft(windows.astype(np.float64) * _hann(), n=FRAME_SIZE, axis=1))
    bands = (spectrum * spectrum) @ mel_filterbank()
    shifted = bands * 10000.0 + 1.0
    return np.log10(np.maximum(shifted, 1e-30)).astype(np.float32)


def patches(mel: np.ndarray) -> np.ndarray:
    """``[patches, 128, 96]``: 128-frame patches every 62 frames, an incomplete tail discarded."""

    mel = np.asarray(mel, dtype=np.float32)
    if mel.shape[0] < PATCH_SIZE:
        return np.zeros((0, PATCH_SIZE, N_BANDS), dtype=np.float32)
    starts = range(0, mel.shape[0] - PATCH_SIZE + 1, PATCH_HOP)
    return np.stack([mel[s : s + PATCH_SIZE] for s in starts]).astype(np.float32)
