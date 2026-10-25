"""The Discogs-EffNet style model: one-time download, integrity check, CPU inference.

Weights: MTG Essentia ``discogs-effnet-bsdynamic-1.onnx`` (the official ONNX export, dynamic batch)
and its metadata JSON, from https://essentia.upf.edu/models/.  Licence CC BY-NC-SA 4.0 (personal,
non-commercial use).  They are downloaded on first use into a git-ignored folder (``models/genre``
beside the project, or ``IDEA_GENRE_MODEL_DIR``) and never committed.

The ONNX graph has two outputs: ``embeddings`` (1280 numbers per patch) and ``activations`` (the
400 Discogs-style sigmoid probabilities).  The separately published ``genre_discogs400`` head is a
single dense layer plus sigmoid over those embeddings; applying its weights to ``embeddings``
reproduces ``activations`` to 2.4e-7 (see ``docs/reviews/build-genre.md``), so ``activations`` *is*
the genre_discogs400 answer and one 18 MB file is all that is needed.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from id_detector.genre import frontend, vocab

BASE_URL = "https://essentia.upf.edu/models/feature-extractors/discogs-effnet/"
LICENCE = "CC BY-NC-SA 4.0 (MTG, Universitat Pompeu Fabra) - personal, non-commercial use"


@dataclass(frozen=True)
class ModelFile:
    name: str
    sha256: str
    size: int

    @property
    def url(self) -> str:
        return BASE_URL + self.name


ONNX = ModelFile(
    "discogs-effnet-bsdynamic-1.onnx",
    "a280825b334797cf677939db8cd5762c0392aedd0ca6415dbc1cd083f045e43c",
    18_027_718,
)
METADATA = ModelFile(
    "discogs-effnet-bsdynamic-1.json",
    "a2e85b2e7372d5f8e0f35bdd6aeae1139f101087d183d0b2fb60b0ea0f01a0ff",
    14_986,
)
FILES = (ONNX, METADATA)
DOWNLOAD_BYTES = sum(f.size for f in FILES)


class ModelError(RuntimeError):
    pass


def default_model_dir() -> Path:
    override = os.environ.get("IDEA_GENRE_MODEL_DIR", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "models" / "genre"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_ready(directory: Path) -> bool:
    """Whether every file is present at its expected size (the hash is checked on download)."""

    return all(
        (directory / f.name).is_file() and (directory / f.name).stat().st_size == f.size
        for f in FILES
    )


#: ``(done_bytes, total_bytes)`` progress callback.
Progress = Callable[[int, int], None]
_DOWNLOAD_LOCK = threading.Lock()


def ensure_model(
    directory: Path,
    progress: Progress | None = None,
    *,
    fetch: Callable[[str, Path, Callable[[int], None]], None] | None = None,
) -> None:
    """Download any missing model file, verify its SHA-256, and move it into place atomically.

    ``fetch(url, destination, on_bytes)`` is the network step (tests pass a fake).  Only one
    download runs at a time; a second caller waits and then finds the files ready.
    """

    with _DOWNLOAD_LOCK:
        if is_ready(directory):
            return
        directory.mkdir(parents=True, exist_ok=True)
        fetch = fetch or _http_fetch
        done = sum(
            f.size
            for f in FILES
            if (directory / f.name).is_file() and (directory / f.name).stat().st_size == f.size
        )
        for item in FILES:
            target = directory / item.name
            if target.is_file() and target.stat().st_size == item.size:
                continue
            partial = directory / (item.name + ".part")
            base = done

            def on_bytes(count: int, base: int = base) -> None:
                if progress is not None:
                    progress(min(base + count, DOWNLOAD_BYTES), DOWNLOAD_BYTES)

            try:
                fetch(item.url, partial, on_bytes)
                if partial.stat().st_size != item.size or _sha256(partial) != item.sha256:
                    raise ModelError(f"{item.name} did not match its published checksum")
                os.replace(partial, target)
            finally:
                if partial.exists():
                    partial.unlink()
            done += item.size
        if progress is not None:
            progress(DOWNLOAD_BYTES, DOWNLOAD_BYTES)


def _http_fetch(url: str, destination: Path, on_bytes: Callable[[int], None]) -> None:
    import httpx

    received = 0
    with (
        httpx.stream("GET", url, timeout=httpx.Timeout(30.0), follow_redirects=True) as response,
        open(destination, "wb") as handle,
    ):
        response.raise_for_status()
        for chunk in response.iter_bytes(1 << 16):
            handle.write(chunk)
            received += len(chunk)
            on_bytes(received)


class StyleModel:
    """CPU inference over mel patches.  Thread-safe: onnxruntime sessions may run concurrently."""

    def __init__(self, directory: Path) -> None:
        import onnxruntime as ort

        if not is_ready(directory):
            raise ModelError("the genre model has not been downloaded")
        metadata = json.loads((directory / METADATA.name).read_text("utf-8"))
        if tuple(metadata.get("classes") or ()) != vocab.labels():
            raise ModelError("the model's style list does not match the built-in vocabulary")
        options = ort.SessionOptions()
        options.log_severity_level = 3
        self._session = ort.InferenceSession(
            str(directory / ONNX.name), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self._input = self._session.get_inputs()[0].name

    def activations(self, patches: np.ndarray) -> np.ndarray:
        """``[patches, 400]`` style probabilities for ``[patches, 128, 96]`` mel patches."""

        if patches.shape[0] == 0:
            return np.zeros((0, 400), dtype=np.float32)
        outputs = self._session.run(["activations"], {self._input: patches.astype(np.float32)})
        return np.asarray(outputs[0])


def predict(signals: list[np.ndarray], infer: Callable[[np.ndarray], np.ndarray]) -> np.ndarray:
    """Mean style probabilities over every patch of every excerpt (Essentia's usual averaging)."""

    batches = [frontend.patches(frontend.melspectrogram(s)) for s in signals]
    stacked = [b for b in batches if b.shape[0]]
    if not stacked:
        raise ModelError("the audio was too short to analyse (needs about 2 seconds)")
    activations = infer(np.concatenate(stacked))
    return np.asarray(activations, dtype=np.float64).mean(axis=0)
