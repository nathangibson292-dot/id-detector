"""Offline sampling-policy simulation over stored per-window Shazam answers (no network).

Usage: ``uv run python scripts/simulate_sampling.py <copy-of-work-root> <report.json>``

For every stored mix (point it at a COPY of ``work/``; audio is not needed): load the exact
evidence today's result was fused from (``refusion.load_fusion_inputs``), re-fuse it with today's
fusion (the baseline), then for each policy keep only the Shazam generation-0 answers that policy
would have asked for, re-fuse, and diff the tracklists (every row, hidden ones included: artist,
title, confidence badge, hidden reason, start, end, on-air time).  A policy may ship only with an
empty diff on every mix.  See docs/reviews/build-scan-speed.md for the 2026-10-25 results.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

from id_detector.contracts import WindowRecord
from id_detector.fuse.episodes import build_episodes
from id_detector.fuse.identity import build_identity_graph
from id_detector.io import native_path, read_text
from id_detector.orchestrate import scanned_windows
from id_detector.present.bundles import load_run_snapshot
from id_detector.present.exports import flatten_tracklist
from id_detector.providers.base import AppConfig
from id_detector.refusion import (
    _source_fuse_dir,
    _thresholds,
    is_deep,
    load_fusion_inputs,
)

CONFIG = AppConfig()

ERR = "\x00err"
NONE = "\x00none"


def window_id_of(obs) -> str | None:
    for sid in obs.source_ids:
        if sid.startswith("window:"):
            return sid.removeprefix("window:")
    return None


def controllable(obs) -> bool:
    return obs.provider == "shazam" and obs.generation == 0 and obs.transform.type == "none"


# ---------------------------------------------------------------- policies
def p_all(a):
    return set(range(len(a)))


def p_every(n):
    def f(a):
        s = set(range(0, len(a), n))
        s.add(len(a) - 1)
        return s

    f.__name__ = f"every{n}"
    return f


def _same(x, y, none_agrees):
    if x in (ERR,) or y in (ERR,):
        return False
    if x == NONE or y == NONE:
        return none_agrees and x == y
    return x == y


def p_refine(n, none_agrees=False, bisect=False):
    """Coarse every-n pass, then fill each gap whose two ends disagree (or are unknown)."""

    def f(a):
        last = len(a) - 1
        asked = set(range(0, len(a), n)) | {last}
        anchors = sorted(asked)

        def fill(i, j):
            if j - i <= 1:
                return
            if _same(a[i], a[j], none_agrees):
                return
            if bisect:
                m = (i + j) // 2
                asked.add(m)
                fill(i, m)
                fill(m, j)
            else:
                asked.update(range(i + 1, j))

        for i, j in zip(anchors, anchors[1:], strict=False):
            fill(i, j)
        return asked

    f.__name__ = f"refine{n}{'-none' if none_agrees else ''}{'-bisect' if bisect else ''}"
    return f


def p_runskip(k, s, none_agrees=False):
    """Sequential: after k identical answers in a row, skip s windows and probe the next one;
    when the probe disagrees, back-fill the skipped windows."""

    def f(a):
        asked = set()
        i = 0
        run_key, run_len = None, 0
        n = len(a)
        while i < n:
            if run_len >= k and run_key is not None:
                probe = min(n - 1, i + s)
                asked.add(probe)
                if _same(a[probe], run_key, none_agrees) and probe > i:
                    i = probe + 1
                    run_len += 1
                    continue
                # back-fill
                for j in range(i, probe):
                    asked.add(j)
                # reset run on the probe's answer stream
                run_key, run_len = None, 0
                for j in range(i, probe + 1):
                    if run_key is not None and _same(a[j], run_key, none_agrees):
                        run_len += 1
                    else:
                        run_key, run_len = (
                            (a[j] if a[j] != ERR and (a[j] != NONE or none_agrees) else None),
                            1,
                        )
                i = probe + 1
                continue
            asked.add(i)
            if run_key is not None and _same(a[i], run_key, none_agrees):
                run_len += 1
            else:
                ok = a[i] != ERR and (a[i] != NONE or none_agrees)
                run_key, run_len = (a[i] if ok else None), 1
            i += 1
        return asked

    f.__name__ = f"runskip{k}x{s}{'-none' if none_agrees else ''}"
    return f


def p_noneskip(k):
    """Skip only the interior of long confirmed no-match stretches: every other window inside
    a run of >= k consecutive no-matches (both neighbours of a skipped window are no-matches)."""

    def f(a):
        asked = set(range(len(a)))
        i = 0
        while i < len(a):
            if a[i] != NONE:
                i += 1
                continue
            j = i
            while j < len(a) and a[j] == NONE:
                j += 1
            if j - i >= k:
                for m in range(i + 1, j - 1, 2):
                    asked.discard(m)
            i = j
        return asked

    f.__name__ = f"noneskip{k}"
    return f


def p_nonecausal(k):
    """Causal no-match skipping: ask every even window; ask an odd window unless it sits inside a
    stretch where the k asked even windows on EACH side all answered no-match."""

    def f(a):
        n = len(a)
        asked = set(range(0, n, 2)) | {n - 1}
        for m in range(1, n - 1, 2):
            left = [m - 1 - 2 * t for t in range(k)]
            right = [m + 1 + 2 * t for t in range(k)]
            idx = left + right
            if min(idx) < 0 or max(idx) >= n or not all(a[i] == NONE for i in idx):
                asked.add(m)
        return asked

    f.__name__ = f"nonecausal{k}"
    return f


EXTRA = [
    p_runskip(10, 1),
    p_runskip(20, 1),
    p_noneskip(5),
    p_noneskip(10),
    p_nonecausal(1),
    p_nonecausal(3),
    p_nonecausal(6),
]

POLICIES = [
    p_every(2),
    p_every(3),
    p_refine(2),
    p_refine(3),
    p_refine(4),
    p_refine(2, none_agrees=True),
    p_refine(4, bisect=True),
    p_refine(8, bisect=True),
    p_runskip(2, 1),
    p_runskip(3, 1),
    p_runskip(3, 2),
    p_runskip(4, 1),
    p_runskip(5, 1),
    p_runskip(3, 1, none_agrees=True),
]


# ---------------------------------------------------------------- fusion
def fuse(snapshot, inputs, observations, all_windows, manifest):
    overlap, separation = _thresholds(manifest, snapshot.metadata)
    windows = (
        scanned_windows(all_windows, observations) if all_windows is not None else inputs.windows
    )
    identity = build_identity_graph(snapshot.source.media_key, observations, hints=inputs.hints)
    episodes, _ = build_episodes(
        media_key=snapshot.source.media_key,
        duration_ms=snapshot.duration_ms,
        observations=observations,
        windows=windows,
        identity=identity,
        hints=inputs.hints,
        generation=inputs.generation,
        config=CONFIG,
        calibrator=None,
        overlap_min_ms=overlap,
        separation_min_ms=separation,
    )
    rows = flatten_tracklist(
        episodes,
        identity.record,
        None,
        collapse=CONFIG.collapse,
        same_track_bridge_ms=CONFIG.same_track_bridge_ms,
        min_track_ms=CONFIG.present_min_track_ms,
        include_hidden=True,
    )
    return rows


def row_key(r):
    return (
        r.get("artist"),
        r.get("title"),
        r.get("badge"),
        r.get("hidden_reason"),
        r.get("start_ms"),
        r.get("end_ms"),
        r.get("on_air_ms"),
    )


def label_key(r):
    return (r.get("artist"), r.get("title"), r.get("badge"), r.get("hidden_reason"))


def diff(base, other, key):
    b = Counter(key(r) for r in base)
    o = Counter(key(r) for r in other)
    return sorted((b - o).elements(), key=str), sorted((o - b).elements(), key=str)


def main() -> None:
    root = Path(native_path(Path(sys.argv[1])))
    out = Path(sys.argv[2])
    report = {"mixes": [], "policies": [p.__name__ for p in (POLICIES + EXTRA)]}
    for media_dir in sorted(p for p in root.glob("*/*") if p.is_dir()):
        name = media_dir.parent.name[:8] + "/" + media_dir.name[:8]
        entry = {"mix": name}
        try:
            snapshot = load_run_snapshot(media_dir)
            manifest = snapshot.manifest
            entry["deep"] = is_deep(manifest, snapshot.metadata)
            fuse_dir = _source_fuse_dir(snapshot, media_dir)
            inputs = load_fusion_inputs(media_dir, fuse_dir)
        except Exception as exc:  # noqa: BLE001
            entry["skipped"] = f"{type(exc).__name__}: {exc}"
            report["mixes"].append(entry)
            print(name, "SKIP", entry["skipped"], flush=True)
            continue
        all_windows = []
        for path in sorted((media_dir / "windows").glob("windows.gen*.jsonl")):
            all_windows += [
                WindowRecord.model_validate_json(line)
                for line in read_text(path).splitlines()
                if line.strip()
            ]
        window_ids = {w.id for w in all_windows}
        obs = list(inputs.observations)
        ctrl = [o for o in obs if controllable(o)]
        fixed = [o for o in obs if not controllable(o)]
        if all_windows and not all(window_id_of(o) in window_ids for o in ctrl):
            all_windows = None  # pruned / mismatched windows: fall back to answered supports
        # the gen-0 sweep, in mix order
        by_window: dict[str, list] = {}
        for o in ctrl:
            by_window.setdefault(window_id_of(o), []).append(o)
        order = sorted(by_window, key=lambda w: by_window[w][0].support_ms[0])
        answers = []
        for w in order:
            o = by_window[w][0]
            if o.status == "error":
                answers.append(ERR)
            elif o.status == "match":
                answers.append(
                    o.provider_ids.get("shazam")
                    or (o.raw_label.artist, o.raw_label.title).__repr__()
                )
            else:
                answers.append(NONE)
        entry["windows"] = len(order)
        entry["fixed_observations"] = len(fixed)
        entry["providers"] = sorted({o.provider for o in obs})
        entry["window_records"] = all_windows is not None
        base = fuse(snapshot, inputs, obs, all_windows, manifest)
        stored = None
        tl = snapshot.directory / "tracklist.json"
        if tl.is_file():
            stored = json.loads(read_text(tl)).get("entries")
        shown = [r for r in base if r.get("hidden_reason") is None]
        if stored is not None:
            m, e = diff(stored, shown, label_key)
            entry["baseline_vs_stored_label_diff"] = len(m) + len(e)
        entry["rows"] = len(shown)
        entry["policies"] = {}
        for policy in POLICIES + EXTRA:
            asked_idx = policy(answers)
            asked = {order[i] for i in asked_idx}
            sub = fixed + [o for w in asked for o in by_window[w]]
            rows = fuse(snapshot, inputs, sub, all_windows, manifest)
            strict = diff(base, rows, row_key)
            labels = diff(base, rows, label_key)
            entry["policies"][policy.__name__] = {
                "asked": len(asked),
                "saved": len(order) - len(asked),
                "strict_diff": [list(map(str, strict[0])), list(map(str, strict[1]))],
                "label_diff": [list(map(str, labels[0])), list(map(str, labels[1]))],
            }
            print(
                name,
                policy.__name__,
                f"saved {len(order) - len(asked)}/{len(order)}",
                f"strict -{len(strict[0])}/+{len(strict[1])}",
                f"labels -{len(labels[0])}/+{len(labels[1])}",
                flush=True,
            )
        report["mixes"].append(entry)
    out.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
