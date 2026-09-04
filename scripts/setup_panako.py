"""Download, verify and configure the pinned Panako jar.

Thin wrapper around :mod:`id_detector.providers.panako_setup` so the setup can be run either as
``uv run python scripts/setup_panako.py`` or via ``id-detector panako-setup``.  Downloads a single
pinned release asset, verifies its sha256, writes the Windows-ready ``config.properties`` beside
the jar, creates the git-ignored index-store root, and confirms the jar starts.
"""

from __future__ import annotations

import sys
from pathlib import Path

from id_detector.providers.panako_setup import (
    DEFAULT_INDEX_ROOT,
    DEFAULT_TOOL_DIR,
    PanakoSetupError,
    manual_instructions,
    setup_sync,
)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    tool_dir = Path(argv[0]) if argv else DEFAULT_TOOL_DIR
    index_root = Path(argv[1]) if len(argv) > 1 else DEFAULT_INDEX_ROOT
    offline = "--offline" in argv
    try:
        result = setup_sync(tool_dir=tool_dir, index_root=index_root, allow_download=not offline)
    except PanakoSetupError as exc:
        print(exc, file=sys.stderr)
        print(manual_instructions(tool_dir), file=sys.stderr)
        return 1
    print(f"Panako jar: {result.jar} (sha256 {result.sha256}, downloaded={result.downloaded})")
    print(f"config:     {result.config}")
    print(f"index root: {result.index_root}")
    print(f"JDK:        {result.java if result.java else 'not found — Panako cannot run'}")
    print(f"Panako starts: {result.help_first_line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
