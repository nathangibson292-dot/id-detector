"""Obtain and verify the pinned Panako jar and write its Windows-ready configuration.

Used by ``id-detector panako-setup`` and ``scripts/setup_panako.py``.  Downloads a single pinned
release asset, verifies its sha256 (never trusting an unverified binary), writes the
``config.properties`` Panako reads from beside its jar, and creates the git-ignored index-store
root.  A prebuilt jar is expected; building Panako from source is out of scope, so a failed or
unavailable download prints the exact manual step and fails gracefully.
"""

from __future__ import annotations

import asyncio
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from id_detector.io import native_path
from id_detector.providers.panako import (
    PANAKO_DOWNLOAD_URL,
    PANAKO_JAR_NAME,
    PANAKO_JAR_SHA256,
    PANAKO_JAR_SIZE,
    PanakoRuntime,
    render_config_properties,
    resolve_java,
)

#: Git-ignored home for the jar + config (``data/local`` is already ignored wholesale).
DEFAULT_TOOL_DIR = Path("data/local/panako")
#: Git-ignored root under which each reference-pool index keeps its LMDB store.
DEFAULT_INDEX_ROOT = Path("data/local/panako-db")


class PanakoSetupError(RuntimeError):
    """A setup step failed in a way the operator must resolve manually."""


def jar_path(tool_dir: Path = DEFAULT_TOOL_DIR) -> Path:
    return tool_dir / PANAKO_JAR_NAME


def sha256_of(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_jar(path: Path) -> tuple[bool, str]:
    """Return ``(ok, detail)`` for the jar at ``path`` against the pinned size and sha256."""

    if not path.is_file():
        return False, f"missing: {path}"
    size = path.stat().st_size
    if size != PANAKO_JAR_SIZE:
        return False, f"size mismatch: expected {PANAKO_JAR_SIZE}, got {size}"
    digest = sha256_of(path)
    if digest != PANAKO_JAR_SHA256:
        return False, f"sha256 mismatch: expected {PANAKO_JAR_SHA256}, got {digest}"
    return True, f"verified sha256 {digest}"


def manual_instructions(tool_dir: Path = DEFAULT_TOOL_DIR) -> str:
    """Exact manual step for obtaining the pinned jar when the download is unavailable."""

    return (
        "Automatic download unavailable. Obtain Panako manually:\n"
        f"  1. Download {PANAKO_DOWNLOAD_URL}\n"
        f"  2. Save it as {native_path(jar_path(tool_dir))}\n"
        f"  3. Confirm sha256 == {PANAKO_JAR_SHA256}\n"
        "  4. Re-run `id-detector panako-setup` to verify and configure.\n"
        "Building Panako from source is out of scope for this stage."
    )


def download_jar(
    dest: Path, *, url: str = PANAKO_DOWNLOAD_URL, timeout: float = 300
) -> None:
    """Download the pinned jar to ``dest`` (atomic via a temp file), then leave verification to
    the caller.  Raises :class:`PanakoSetupError` on any network failure."""

    os.makedirs(native_path(dest.parent), exist_ok=True)
    temporary = dest.with_suffix(dest.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "id-detector/panako-setup"})
    try:
        with (
            urllib.request.urlopen(request, timeout=timeout) as response,  # noqa: S310 (pinned URL)
            temporary.open("wb") as handle,
        ):
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                handle.write(block)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        temporary.unlink(missing_ok=True)
        raise PanakoSetupError(f"download failed from {url}: {exc}") from exc
    os.replace(native_path(temporary), native_path(dest))


def write_config(tool_dir: Path = DEFAULT_TOOL_DIR) -> Path:
    """Write ``config.properties`` beside the jar (Windows ``cmd.exe`` decoder, OLAF/LMDB)."""

    os.makedirs(native_path(tool_dir), exist_ok=True)
    config_path = tool_dir / "config.properties"
    config_path.write_text(render_config_properties(), encoding="utf-8", newline="\n")
    return config_path


@dataclass(frozen=True)
class SetupResult:
    jar: Path
    config: Path
    index_root: Path
    sha256: str
    java: Path | None
    help_first_line: str
    downloaded: bool


async def run_setup(
    *,
    tool_dir: Path = DEFAULT_TOOL_DIR,
    index_root: Path = DEFAULT_INDEX_ROOT,
    allow_download: bool = True,
) -> SetupResult:
    """Ensure a verified jar, config and index root, and confirm the jar starts."""

    jar = jar_path(tool_dir)
    ok, detail = verify_jar(jar)
    downloaded = False
    if not ok:
        if not allow_download:
            raise PanakoSetupError(f"jar not present/verified and download disabled: {detail}")
        download_jar(jar)
        downloaded = True
        ok, detail = verify_jar(jar)
        if not ok:
            jar.unlink(missing_ok=True)
            raise PanakoSetupError(f"downloaded jar failed verification: {detail}")

    config = write_config(tool_dir)
    os.makedirs(native_path(index_root), exist_ok=True)

    resolution = resolve_java()
    help_line = "JDK not found — cannot run Panako"
    java = None
    if resolution is not None:
        java = resolution.path
        runtime = PanakoRuntime(java=resolution.path, jar=jar, java_source=resolution.source)
        from id_detector.process import run_process

        result = await run_process(
            [*runtime.base_args(), "configuration"], timeout=60, check=False
        )
        help_line = _first_meaningful_line(result.stdout + result.stderr)

    return SetupResult(
        jar=jar,
        config=config,
        index_root=index_root,
        sha256=PANAKO_JAR_SHA256,
        java=java,
        help_first_line=help_line,
        downloaded=downloaded,
    )


def _first_meaningful_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and "reflections" not in stripped.lower():
            return stripped
    return "no output"


def setup_sync(**kwargs: object) -> SetupResult:
    return asyncio.run(run_setup(**kwargs))  # type: ignore[arg-type]
