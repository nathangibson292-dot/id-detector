"""Generate the Local Free golden: ``tests/golden/local-free/tracklist.json``.

The golden is the ``tracklist.json`` the **free recipe** produces, offline, over the committed
60 s fixture (``tests/fixtures/audio/tone-60s.wav``: three tones, seven frozen windows) with the
scripted Shazam fake ``tests/fakes/scripts/golden-free.json`` (every window a match except the
fourth).  ``tests/test_golden_local_free.py`` re-runs exactly this pipeline and compares the two
documents **semantically** — ignoring the volatile identity fields listed in
:data:`IGNORED_KEYS` and every timestamp — so a change in what the local free tier lists is
caught, while a re-run's run id or a later cycle's bundle/analysis identifiers are not.

Regenerate with ``uv run python scripts/make_golden.py`` after a *deliberate* result change and
commit the new file with the change that caused it.  No provider is ever contacted.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # `uv run python scripts/make_golden.py` — tests/ is not installed
    sys.path.insert(0, str(ROOT))

from id_detector.providers.base import AppConfig  # noqa: E402
from id_detector.recipes import FREE_RECIPE  # noqa: E402

AUDIO = ROOT / "tests" / "fixtures" / "audio" / "tone-60s.wav"
SCRIPT = ROOT / "tests" / "fakes" / "scripts" / "golden-free.json"
GOLDEN = ROOT / "tests" / "golden" / "local-free" / "tracklist.json"

#: Fields that identify a particular run, bundle or analysis rather than its result (plan
#: 0b-ii).  Some are not written by ``tracklist.json`` yet — later cycles add them — so the
#: comparison is forward-compatible.
IGNORED_KEYS: frozenset[str] = frozenset(
    {
        "run_id",
        "generated_by",
        "requested_recipe_id",
        "algorithm_version",
        "analysis_key",
        "presentation_version",
    }
)
#: Any key that names a timestamp (``started_at``, ``finished_at``, ``generated_at``, ...).
_TIMESTAMP_SUFFIXES = ("_at",)
_TIMESTAMP_KEYS = frozenset({"timestamp", "timestamps"})

#: What the golden run fixes beyond the free recipe's defaults, and why (all speed-only or
#: fixture-driven; none changes how a real mix is fused):
#:  - ``recognise_concurrency=1`` and an unbounded Shazam ceiling: the fake answers instantly and
#:    a single worker keeps the run short and its dispatch order stable;
#:  - ``present_min_track_ms=0``: the fixture's tones each play ~20 s, under the 30 s on-air floor
#:    a real set needs, so the floor would list nothing and the golden would be empty.
GOLDEN_CONFIG = AppConfig(
    recognise_concurrency=1,
    shazam_requests_per_minute=1_000_000,
    present_min_track_ms=0,
)


def is_timestamp_key(key: str) -> bool:
    return key in _TIMESTAMP_KEYS or key.endswith(_TIMESTAMP_SUFFIXES)


def semantic(value: Any) -> Any:
    """``value`` with every ignored identity field and timestamp removed, recursively."""

    if isinstance(value, Mapping):
        return {
            str(key): semantic(item)
            for key, item in value.items()
            if str(key) not in IGNORED_KEYS and not is_timestamp_key(str(key))
        }
    if isinstance(value, list):
        return [semantic(item) for item in value]
    return value


def run_local_free(work_root: Path) -> Path:
    """Run the free recipe over the fixture with the scripted fake; return ``tracklist.json``."""

    from id_detector import cli
    from tests.fakes.providers import FakeShazamHTTP

    code = asyncio.run(
        cli._analyse(
            str(AUDIO),
            work_root=work_root,
            print_raw=False,
            refresh=False,
            max_requests=GOLDEN_CONFIG.max_requests,
            tracklist=None,
            no_hints=True,
            app_config=GOLDEN_CONFIG,
            max_generations=GOLDEN_CONFIG.rescan_max_generations,
            novelty=True,
            recipe=FREE_RECIPE,
            shazam_http_client=FakeShazamHTTP(SCRIPT),
        )
    )
    if code != 0:
        raise RuntimeError(f"the golden run did not complete (exit code {code})")
    from id_detector.present.bundles import result_dir

    (source,) = work_root.glob("*/*/ingest/source.json")
    tracklist = result_dir(source.parents[1]) / "tracklist.json"
    return tracklist


def main() -> int:
    work_root = Path(tempfile.mkdtemp(prefix="idea-golden-"))
    produced = json.loads(run_local_free(work_root).read_text(encoding="utf-8"))
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(
        json.dumps(produced, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    entries = produced.get("entries", [])
    print(
        f"wrote {GOLDEN.relative_to(ROOT).as_posix()}: status={produced.get('status')} "
        f"achieved={produced.get('achieved')} entries={len(entries)} (work root {work_root})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
