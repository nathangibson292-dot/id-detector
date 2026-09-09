"""Assert fields on the newest analysis invocation under a work root."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _entries(work_root: Path) -> list[tuple[Path, int, dict[str, Any]]]:
    entries: list[tuple[Path, int, dict[str, Any]]] = []
    for path in sorted(work_root.rglob("invocations.jsonl")):
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if isinstance(value, dict):
                entries.append((path, line_number, value))
    return entries


def _newest(entries: list[tuple[Path, int, dict[str, Any]]]) -> tuple[Path, int, dict[str, Any]]:
    if not entries:
        raise ValueError("no invocations.jsonl entries found")
    return max(
        entries,
        key=lambda item: (
            str(item[2].get("finished_at") or item[2].get("started_at") or ""),
            str(item[0]),
            item[1],
        ),
    )


def _lookup(entry: dict[str, Any], key: str) -> Any:
    if "." in key:
        value: Any = entry
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                raise KeyError(key)
            value = value[part]
        return value
    if key in entry:
        return entry[key]
    matches = [
        mapping[key]
        for mapping in (entry.get("counts"), entry.get("costs"))
        if isinstance(mapping, dict) and key in mapping
    ]
    if len(matches) != 1:
        raise KeyError(key)
    return matches[0]


def _expected(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--expect", nargs="+", required=True, metavar="KEY=VALUE")
    args = parser.parse_args()
    try:
        path, line_number, entry = _newest(_entries(args.work_root))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"journal assertion failed: {exc}")
        return 1
    failures: list[str] = []
    for expression in args.expect:
        key, separator, raw = expression.partition("=")
        if not separator or not key:
            failures.append(f"invalid expectation {expression!r}")
            continue
        try:
            actual = _lookup(entry, key)
        except KeyError:
            failures.append(f"{key}: field not found")
            continue
        expected = _expected(raw)
        if actual != expected:
            failures.append(f"{key}: expected {expected!r}, got {actual!r}")
    location = f"{path}:{line_number}"
    if failures:
        for failure in failures:
            print(f"FAIL {failure}")
        print(f"journal assertion failed at {location}")
        return 1
    print(f"journal assertion passed at {location}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
