"""Generate the deterministic three-tone WAV fixtures used by offline phase gates."""

from __future__ import annotations

import argparse
import math
import sys
import wave
from array import array
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "audio"
SAMPLE_RATE = 16_000
AMPLITUDE = 9_000
FREQUENCIES = (440.0, 554.0, 659.0)
CROSSFADE_SECONDS = 2.0
CHUNK_FRAMES = SAMPLE_RATE


def _weights(time_s: float, duration_s: int) -> tuple[float, float, float]:
    third = duration_s / 3
    half_fade = CROSSFADE_SECONDS / 2
    first_boundary = third
    second_boundary = third * 2
    if time_s < first_boundary - half_fade:
        return (1.0, 0.0, 0.0)
    if time_s < first_boundary + half_fade:
        incoming = (time_s - (first_boundary - half_fade)) / CROSSFADE_SECONDS
        return (1.0 - incoming, incoming, 0.0)
    if time_s < second_boundary - half_fade:
        return (0.0, 1.0, 0.0)
    if time_s < second_boundary + half_fade:
        incoming = (time_s - (second_boundary - half_fade)) / CROSSFADE_SECONDS
        return (0.0, 1.0 - incoming, incoming)
    return (0.0, 0.0, 1.0)


def generate(path: Path, duration_s: int) -> None:
    """Write a mono 16 kHz signed-16 WAV with three tones and two 2 s crossfades."""

    path.parent.mkdir(parents=True, exist_ok=True)
    total_frames = duration_s * SAMPLE_RATE
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        for first_frame in range(0, total_frames, CHUNK_FRAMES):
            frame_count = min(CHUNK_FRAMES, total_frames - first_frame)
            samples = array("h")
            for offset in range(frame_count):
                frame = first_frame + offset
                time_s = frame / SAMPLE_RATE
                weights = _weights(time_s, duration_s)
                value = sum(
                    weight * math.sin(2 * math.pi * frequency * time_s)
                    for weight, frequency in zip(weights, FREQUENCIES, strict=True)
                )
                # A slow deterministic envelope keeps overlapping pure-tone windows from being
                # byte-identical, so the seven frozen windows remain seven cache identities.
                envelope = 0.9 + 0.1 * math.sin(2 * math.pi * 0.17 * time_s)
                samples.append(round(AMPLITUDE * envelope * value))
            if sys.byteorder != "little":
                samples.byteswap()
            output.writeframes(samples.tobytes())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--long",
        action="store_true",
        help="generate tone-3600s.wav on demand instead of the committed 60-second fixture",
    )
    args = parser.parse_args()
    duration = 3_600 if args.long else 60
    target = FIXTURE_ROOT / f"tone-{duration}s.wav"
    generate(target, duration)
    print(target.relative_to(ROOT).as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
