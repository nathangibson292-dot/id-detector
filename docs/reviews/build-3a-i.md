# Build 3a-i — Canonical projection

## Changes

- `src/id_detector/present/exports.py` — added the typed `ProjectionEntry` list and frozen `CanonicalProjection`, made suppression a single derivation step, rendered JSON/Markdown/CUE from its shown entries, recorded `suppressed_count`, removed M3U generation, and dropped Version/Role from Markdown.
- `src/id_detector/present/page.py` — consumed the canonical projection for rows, hero counts, confidence legend and suppression note; made Copy use projection data rather than the mutable DOM; removed the M3U button; bumped `PAGE_VERSION` from 20 to 21.
- `src/id_detector/present/bundles.py` — builds the projection once per publication and passes that exact object to page and export rendering.
- `src/id_detector/present/server.py` — refreshes stale immutable bundles before library cards read their canonical JSON summary.
- `src/id_detector/present/__init__.py` — exposed the projection types and builder through the presentation package.
- `tests/fixtures/present/garage.json` — added the ordinary-mix projection fixture, including an acquisition link and an ID gap.
- `tests/fixtures/present/boiler.json` — added the F-2/F-3 regression fixture with five sub-floor rows and one explicitly suppressed row.
- `tests/fixtures/present/crowd.json` — added a crowd-sourced row, a contradicting alternative, and a contested version.
- `tests/test_projection.py` — added the phase gate for all consumers, suppression counts, immutable frozen-snapshot re-rendering, stale library refresh, M3U removal and all five playlist contracts.
- `tests/test_stage2b_pipeline.py` — updated the legacy Markdown expectation to the required reduced columns.
- `tests/test_stage9_exports.py` — updated the legacy export-set expectation to pin CUE generation and retired M3U non-generation; retained the old standalone renderer test because physical deletion is scheduled later.
- `tests/golden/local-free/tracklist.json` — regenerated the strict semantic golden after the intentional projection schema change.

## Tests added

`tests/test_projection.py` checks each named fixture against one projection: ordered JSON entries, page rows, hero track count, confidence legend, card count/bar, Copy payload, CUE, Markdown, and suppressed-row count. It also verifies hidden rows are absent from every export, refresh changes the floor from six hidden rows to one while leaving the original bundle byte-identical, and the library refreshes a stale bundle before computing its card.

The same test file explicitly checks stable shown-row anchors, both playlist identifiers, CSS/JS injection, shown-only actions plus offline hiding, and the unchanged result URL shape.

## Golden change

The regenerated entry objects add `identity`, `display_label`, `tier`, and `hidden_reason`; the document adds `suppressed_count`. These are the canonical projection fields required by this cycle. `analysis_key` also appears in the regenerated file but remains excluded by the existing strict semantic comparator as a volatile bundle identity. No comparison rule was weakened.

## Ambiguity resolved

The canonical projection contains every ordered entry and stores the already-decided `hidden_reason`; its `shown_entries` property is the only filtered view used by the page hero/legend, Copy, JSON, Markdown, CUE and card. Hidden rows remain in the page DOM behind the existing reveal because removal of that reveal is explicitly cycle 3a-ii; Copy deliberately remains tied to `shown_entries` even if the reveal is opened. JSON exports only shown entries and records `suppressed_count`, so it cannot become a second tracklist.

M3U generation and its UI button are removed in this cycle. The unused standalone renderer remains for the later physical-deletion cycle, matching the plan's disabled-then-deleted instruction and avoiding an out-of-scope API deletion.

`PAGE_VERSION` is now 21. Because bundle identity is `sha256(run_id, presentation_version)`, refreshed analyses intentionally receive new bundle ids; existing sealed bundles are never rewritten.

## Playlist contract preservation

1. Every shown track `<tr>` still has `id="<episode_id>"`; the phase test asserts it for every shown fixture row.
2. The unchanged `row_actions_html(entry)` call receives canonical entries containing both `candidate_id` and `episode_id`, and its emitted `data-candidate-id` / `data-episode-id` are asserted.
3. `page.py` still imports `PLAYLIST_CSS` and `PLAYLIST_JS` and injects them at the existing head/body points; both complete payloads are asserted in rendered HTML.
4. `row_actions_html` is called only when `hidden_reason is None`; hidden rows have no controls. The existing playlist script still hides all controls for non-HTTP(S), including `file://`, and the offline guard is asserted.
5. `_mix_card_html` still emits `/{source_key}/{media_key}/present/index.html`; the exact shape is asserted.

## Verification output

### `uv run pytest -q`

```text
........................................................................ [  6%]
........................................................................ [ 13%]
........................................................................ [ 19%]
........................................................................ [ 26%]
........................................................................ [ 33%]
........................................................................ [ 39%]
........................................................................ [ 46%]
........................................................................ [ 53%]
........................................................................ [ 59%]
........................................................................ [ 66%]
........................................................................ [ 73%]
........................................................................ [ 79%]
........................................................................ [ 86%]
........................................................................ [ 93%]
........................................................................ [ 99%]
...                                                                      [100%]
============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
1083 passed, 93 deselected, 1 warning in 397.73s (0:06:37)
```

### `uv run ruff check .`

```text
All checks passed!
```

### `uv run ruff format --check .`

```text
266 files already formatted
```

`uv run ruff format .` was run before the check and reported `1 file reformatted, 265 files left unchanged` on the final formatting pass.

### `uv run python scripts/audit_fixtures.py`

```text
audited 441 files
fixture audit passed
```

### `uv run pytest tests/test_projection.py -q`

```text
.....                                                                    [100%]
5 passed in 5.50s
```

### Compatibility gate

Command:

```text
uv run pytest tests/test_playlists.py tests/test_golden_local_free.py tests/test_phase1a_bundles.py tests/test_phase1a_cached_open.py tests/test_phase2b_retention.py tests/test_stage7_server.py tests/test_stage10_webapp.py -q
```

Output:

```text
........................................................................ [ 58%]
....................................................                     [100%]
============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
124 passed, 1 warning in 139.16s (0:02:19)
```

### `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_serve.ps1`

```text
smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'
```

### `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/gate_local_mode.ps1`

```text
complete; 6 matches; 0 failures; 7 physical attempts; 1 generations (stop=max_generations); 3 episodes; tracklist=\\?\C:\Users\natha\AppData\Local\Temp\idea-local-gate-affd2d083e4f41dc8d15aed7287cfb5f\work\a63408abeea9adf1c0d4452a6509d9ee4e5399d192f32ee7307bbcef39c83e27\e2179d997e03bdd132bbd451535df92691a581bdeb9b0a6bb73358b54b08575d\present\bundles\389dc021e5dd6bbcbe863a69c168b228f69d165a93b5234992185eb30cbec9c4\tracklist.json
prepared offline cached mix e2179d997e03bdd132bbd451535df92691a581bdeb9b0a6bb73358b54b08575d
local gate passed: idea.cmd; cached mix open; audio metadata + seek; probe logged audio-ok
Evidence: C:\Users\natha\AppData\Local\Temp\idea-local-gate-affd2d083e4f41dc8d15aed7287cfb5f
```

### `git status --short`

```text
 M src/id_detector/present/__init__.py
 M src/id_detector/present/bundles.py
 M src/id_detector/present/exports.py
 M src/id_detector/present/page.py
 M src/id_detector/present/server.py
 M tests/golden/local-free/tracklist.json
 M tests/test_stage2b_pipeline.py
 M tests/test_stage9_exports.py
?? docs/reviews/build-3a-i.md
?? tests/fixtures/present/
?? tests/test_projection.py
```

### `git diff --stat`

```text
 src/id_detector/present/__init__.py    |   6 ++
 src/id_detector/present/bundles.py     |  11 ++-
 src/id_detector/present/exports.py     | 142 ++++++++++++++++++++++++++-------
 src/id_detector/present/page.py        |  49 +++++++-----
 src/id_detector/present/server.py      |  15 +++-
 tests/golden/local-free/tracklist.json |  16 +++-
 tests/test_stage2b_pipeline.py         |   5 +-
 tests/test_stage9_exports.py           |  11 ++-
 8 files changed, 194 insertions(+), 61 deletions(-)
```

`git diff --stat` does not include the three untracked deliverables shown by status: the build report, the three fixture JSON files, and `tests/test_projection.py`.

### `git status --short -- work data`

```text
```

The required scoped status is empty.

## Could not do

Nothing. No live provider calls were made, no dependency was added, and no prohibited file was edited.

## Review + fix pass (sol xhigh review folded in; resumed after an interrupted pass)

### Tree state found

HEAD `6ef821f`, branch `main`, no stash entries. The working tree already carried **more** than the
builder's report describes: `git diff --stat` showed `exports.py` at 261 changed lines against the
report's 142 and `page.py` at 104 against 49, plus a fourth fixture (`tests/fixtures/present/cluster.json`)
the report never mentions and rewritten `expected` blocks in the other three. So the interrupted fixer
had landed most of the P0 work and stopped partway. `uv run pytest -q` passed 1083 on arrival, which is
exactly the problem sol predicted: the gate could not see the defects.

Concretely, what was already applied and correct:

- **suppression before collapse** — `exports.py::_derive_projection_entries` now splits
  `suppressed_episodes` from `live_episodes`, passes only `live_episodes` to `group_display_tracks`,
  and re-appends each suppressed match as a row of its own;
- **projection-owned hero coverage** — `CanonicalProjection.covered_ms` + `_covered_ms`, with
  `page.py::_stats_html(projection, duration_ms)` and `gap_count`/gap markers filtered by the projection;
- **timeline lanes and spans from the projection** — the second `group_display_tracks` pass in
  `render_page` is gone (this was the edit the previous pass was mid-way through);
- **Copy from `COPY_ENTRIES`** rather than the mutable DOM.

What was NOT done: `tests/test_projection.py` had not been updated to consume any of it. It still
hardcoded `collapse=False`, still parametrised only three fixtures, never referenced the `expected`
blocks, and never loaded `cluster.json` at all. The fixtures were ahead of the gate, so both P0 fixes
shipped **untested** and the golden revert had not happened.

### Findings

| Tag | Source | Evidence | Verdict |
|---|---|---|---|
| P0 honesty — suppression after collapse | sol | `exports.py:355-398,384-391,430-432`; `grouping.py:369-389` | **Already fixed, was untested.** Verified correct; regression test added. |
| P0 — hero % bypasses the projection | sol | `page.py:605-606,617,1323` | **Already fixed, was untested.** Verified correct; regression test added. |
| P0 — unrelated `analysis_key` in the frozen golden | sol | `tests/golden/local-free/tracklist.json:3` | **Still open. Reverted.** |
| P1 — the gate avoids the production path | sol | `tests/test_projection.py:225-228` vs `providers/base.py:129` | **Still open. Fixed.** |
| P1 — weak assertions | sol | `tests/test_projection.py:289-292` and throughout | **Still open. Fixed.** |
| P2 — dead M3U remnants | sol | `exports.py:726-747`; `present/__init__.py:13,63`; `test_stage9_exports.py:62` | **Correctly deferred** — see below. |
| P2 — projection typing/immutability | sol | `exports.py` `CanonicalProjection` | **Still open. Fixed.** |
| P0 (mine) — the fixture helper fabricated impossible coverage | this pass | `tests/test_projection.py::_inputs` durations block | **Found and fixed.** |

**P0 — suppression applied after collapse.** Confirmed fixed in the tree, and confirmed *correct* rather
than merely different: `label_by_episode` is now built from `live_episodes` only, so a suppressed
identity cannot reach a shown row's `overlap_labels` and from there the CUE's `REM` lines either — that
would have been the same defect by a fourth route. To prove the fix is load-bearing I re-applied the old
behaviour as a probe (`suppressed_episodes = []`, `live_episodes = list(episodes.episodes)`) and re-ran:
2 failures, **both only in the `collapse=True` variants**. That is direct evidence for sol's P1 — with the
old `collapse=False`-only gate this P0 was invisible, and the `flat` variants still pass under the probe.

**P0 — hero percentage.** Also confirmed fixed. Probe: `_covered_ms` forced to `return base`. Result: 8
failures including the dedicated hero test. The fixed figure is honest for the right reason —
`semantics.py:157` builds `evidence_supported_ms` with `normalise_intervals` and `semantics.py:195`
asserts the partition covers the media exactly once, so `base − (time proved only by hidden rows)` is
precisely the shown rows' union. Time a hidden row merely *shares* with a shown one still counts, which
is correct: the shown row proves it.

**P0 — golden `analysis_key`.** Still present; reverted that one line and nothing else. I audited the
rest myself rather than trusting sol: the remaining delta is exactly 14 insertions / 1 deletion —
`identity`, `display_label`, `tier`, `hidden_reason` on each of the three entries (12) plus
`suppressed_count` on the document (the 1-for-2 line change on `status`). Every one is a field this
cycle introduces. No `badge`, `tiers`, `suppressed`, `start_ms`/`end_ms` or `primary_role` value moved.
`scripts/make_golden.py` and `tests/test_golden_local_free.py` are untouched by this cycle
(absent from `git status`), so the comparator is unweakened; `analysis_key` sits in its `IGNORED_KEYS`
(`make_golden.py:39-47`) and comes from bundle publication (`bundles.py:333-337`), not from here.
(Note: my first revert attempt rewrote the file with CRLF endings. Caught it from git's warning and
rewrote in binary — the file is LF-only, as `HEAD` is.)

**P1 — the gate avoids the production path.** `_publish` now takes `collapse` and every surface test is
parametrised over `(("collapsed", True), ("flat", False))`, with `collapse=True` first because it is the
`AppConfig` default. `build_projection` is also handed `same_track_bridge_ms=config.same_track_bridge_ms`
so the projection under test is built with the same knobs `publish_result` uses.

**P1 — weak assertions.** Each fixture now carries a hand-written `expected` block **per collapse mode**
(tracks, suppressed, gaps, pct, crowd, badges, note, the exact copied lines, alternatives). Every
assertion compares against *that*, never against a second call into the code under test. Specifically:

- hero coverage and gap count are pinned to hand-computed figures (`<b>50%</b>`, `ID gap`);
- the confidence legend is checked per badge *and* per bar width, plus a negative check that no badge
  the fixture does not expect appears at all;
- the library card is checked for its count, every bar width and the tooltip text, not just its URL;
- Copy is exercised by extracting the shipped `tracklistText()` **and running it under node** over the
  page's own `COPY_ENTRIES` — reading the payload and re-joining it in Python would only prove the
  payload, and the defect was the function;
- CUE is compared identity-by-identity (parsed `PERFORMER`/`TITLE` pairs zipped against the expected
  labels), not just its `TRACK` count;
- Markdown is parsed into cells and its Track column compared as a list, so a missing or extra row fails;
- the row regex is `<tr class="(track[^"]*)"[^>]*\sid="…"`. The `\s` is the point: the old
  `[^>]*id="…"` also matched the tail of `data-episode-id="…"`, so it passed with the `<tr id>` contract
  gone. The captured class is asserted to carry `short` iff the row is hidden;
- crowd provenance is asserted independently — the fixture states the count, the `(from comments)` copy
  line and `data-crowd="1"` on the row.

**P2 — dead M3U remnants: deferred, with reason.** `PLAN-v2.md:22` lists M3U among the things "disabled
before the beta, deleted after (§5 M2)". Generation, the `media_target` parameter and the UI button are
gone this cycle; `render_m3u`/`_m3u_seconds`, their `present/__init__.py` export and
`test_stage9_exports.py::test_m3u_seeks_each_entry_with_extvlcopt_start_time` are the disabled-not-yet-deleted
remainder and belong to M2. Deleting them here would be an out-of-cycle public-API change. The tests do
assert no `tracklist.m3u` is written and no button is rendered.

**P2 — typing/immutability.** `frozen=True` protected the tuple but not the dicts inside it, and one
projection object is handed to the page, then the exports, then the card. The stored rows are now private
(`_entries`) and `entries`/`shown_entries` hand out copies, so no surface can edit a row another surface
is about to render. `suppressed_count`/`gap_count` read `_entries` directly rather than paying for a copy.

**P0 (found in this pass) — the fixture helper fabricated coverage the set could not have.** `_inputs`
built `evidence_supported_ms` as `sum(end - start)` over every track. That is not what a real run
produces: the fuser's duration block is a partition built from interval **unions**. On the new
overlapping `cluster` fixture the naive sum gave 340 s of evidence in a 300 s set — the hero pinned at a
clamped 100 % where the honest figure is 60 % — so the fixture's own hand-computed expectation
disagreed with the code and neither was trustworthy. The helper now unions the spans via
`normalise_intervals`/`interval_length`, derives `no_evidence_ms` from the gaps and puts the remainder in
`unscanned_ms`, and then **asserts the six parts sum to the media length**, mirroring the invariant
`semantics.py:195` enforces in production. With that, all four fixtures' hand-written percentages hold
exactly: garage 85, boiler 50, crowd 92, cluster 60. Had this been left, the cycle would have shipped a
gate whose coverage numbers were meaningless on any overlapping mix — which is every real DJ set.

### Independent checks beyond sol

- **Every consumer reads the one projection.** Page table, hero, legend, timeline lanes and spans, gap
  markers, library card count and bar, Copy, CUE, Markdown and JSON: verified by reading and now pinned
  by test. `render_page` no longer re-groups; lanes come from the projection's track entries and
  `EPISODE_SPANS` from its shown ones, both asserted against the projection's episode-id order.
- **Re-render mints a new bundle and never rewrites a sealed one, after retention has pruned `fuse/`.**
  `fuse/episodes.json` is deleted before the refresh, so the run is reconstructed from the bundle's frozen
  snapshot. The first bundle is re-hashed byte-for-byte afterwards, and the new bundle's page, JSON, CUE
  and Copy output are checked to agree with each other, not merely to exist.
- **Suppressed rows stay suppressed identically in page and exports, counts matching.** The exhaustive
  list equalities cover listed rows; a hidden identity smuggled in as a note is checked separately and by
  whole delimited value — a substring search tripped over `South — Night Bus` being a prefix of
  `South — Night Bus (Dub)`, which is exactly the false confidence the original substring assertions gave.
- **`test_stage2b_pipeline.py` and `test_stage9_exports.py` were followed, not relaxed.** stage2b's
  Markdown expectation moved to the new required columns *and* adds negative checks that `Version`/`Role`
  and `UNVERIFIED` are gone — strictly stronger. stage9 drops the removed `media_target` argument and the
  M3U file assertions, keeps `REM LAYER`, and adds `result.cue_path` and a `not exists` check for the
  retired file. Nothing was loosened.
- **`_fresh_sets` is new behaviour worth noting** (the library refreshes stale bundles before reading
  cards). Verified it is safe to put on the home-page path: `ensure_fresh_page` returns early on a
  version stamp check (`refresh.py:51`) and swallows any exception so one bad set cannot break serving.

### What changed in this pass

- `tests/golden/local-free/tracklist.json` — removed the out-of-cycle `analysis_key`; LF endings restored.
- `tests/test_projection.py` — rewritten around the fixtures' `expected` blocks; both collapse modes; the
  `cluster` fixture wired in; durations helper corrected to a real union partition with an invariant
  assertion; the assertions listed under P1 above. 5 tests → **16**.
- `src/id_detector/present/exports.py` — `CanonicalProjection` rows made genuinely immutable.

### Tests added

`tests/test_projection.py`, 16 tests (from 5):

- `test_every_surface_renders_the_same_projection` — 4 fixtures × 2 collapse modes, every surface against
  the fixture's hand-computed expectation.
- `test_a_suppressed_match_neither_rides_along_nor_hides_its_honest_group` — the P0 regression, both modes.
  `cluster.json` sets two traps: `Work` (an honest primary with a `contradicted` near-duplicate that must
  not ride along as a silent alternative) and `Night Bus` (a suppressed match with the *stronger* badge
  that would win primary selection and hide its honest `(Dub)` group-mate). `Rise` proves a legitimate
  near-duplicate cluster still folds.
- `test_the_hero_percentage_describes_only_the_rows_the_tracklist_lists` — the F-2 regression, both modes;
  asserts `50%` is rendered, that `90%` appears nowhere, and computes the 90 % the old code *would* have
  produced from the same inputs so the contrast is pinned rather than asserted from memory.
- `test_refresh_mints_a_complete_consistent_bundle_from_the_frozen_run` — both modes, `fuse/` pruned first.
- `test_library_refreshes_a_stale_bundle_before_reading_its_card`.
- `test_the_projection_cannot_be_edited_by_the_surface_rendering_it` — the immutability contract.

Both P0 fixes were verified by re-applying the defect as a probe and confirming the new tests fail, then
restoring. Neither would have failed the gate as it stood.

### Playlist contract (the other session's feature) — preserved

1. `id="<episode_id>"` on every shown `<tr>` — `page.py:463`. Now asserted with `\sid="`, which the old
   regex did not actually pin; the new test would fail if the attribute were dropped.
2. `data-candidate-id` + `data-episode-id` via `row_actions_html` — `page.py:475`; projection entries
   still carry both `episode_id` and `candidate_id` (`ProjectionEntry`), asserted per shown row.
3. `PLAYLIST_CSS`/`PLAYLIST_JS` imported (`page.py:30`) and injected at both points — CSS into the head
   (`page.py:1375`), JS into the body (`page.py:1372`); both full payloads asserted in the rendered HTML.
4. Controls only on shown rows (`row_actions_html(entry) if not hidden`), and hidden on `file://`
   (`playlists/assets.py:21-24`). Asserted positively on shown rows, negatively on hidden rows, plus the
   offline guard. Suppression moving before collapse does not change which rows get controls — it changes
   which rows exist — and hidden rows still render with no `pl-actions`.
5. `/{source_key}/{media_key}/present/index.html` — `server.py:886`, unchanged and asserted.

`tests/test_playlists.py` was not read for modification and passes unchanged. No file owned by the other
session was touched: `playlists/**`, `tests/test_playlists.py`, `README.md`, `idea.cmd` and
`present/theme.py` are all absent from `git status`.

### Verification output

`uv run pytest -q`

```text
1094 passed, 93 deselected, 1 warning in 423.93s (0:07:03)
```

`uv run pytest tests/test_projection.py -q`

```text
................                                                         [100%]
16 passed in 11.74s
```

Compatibility gate (`tests/test_playlists.py tests/test_golden_local_free.py tests/test_phase1a_bundles.py
tests/test_phase1a_cached_open.py tests/test_phase1a_compat.py tests/test_phase2b_retention.py
tests/test_stage7_server.py tests/test_stage9_exports.py tests/test_stage10_webapp.py
tests/test_stage2b_pipeline.py`)

```text
170 passed, 4 deselected, 1 warning in 115.62s (0:01:55)
```

`uv run ruff check .`

```text
All checks passed!
```

`uv run ruff format --check .`

```text
266 files already formatted
```

`uv run python scripts/audit_fixtures.py`

```text
audited 442 files
fixture audit passed
```

`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_serve.ps1`

```text
smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'
```

`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/gate_local_mode.ps1`

```text
complete; 6 matches; 0 failures; 7 physical attempts; 1 generations (stop=max_generations); 3 episodes; tracklist=\\?\C:\Users\natha\AppData\Local\Temp\idea-local-gate-c765f52b456b40e88809afc1910d0611\work\a63408abeea9adf1c0d4452a6509d9ee4e5399d192f32ee7307bbcef39c83e27\e2179d997e03bdd132bbd451535df92691a581bdeb9b0a6bb73358b54b08575d\present\bundles\bb7c9496a534ed866cf49fc68eca05a8007a9316712e67e7f8b2664f667e33e9\tracklist.json
prepared offline cached mix e2179d997e03bdd132bbd451535df92691a581bdeb9b0a6bb73358b54b08575d
local gate passed: idea.cmd; cached mix open; audio metadata + seek; probe logged audio-ok
Evidence: C:\Users\natha\AppData\Local\Temp\idea-local-gate-c765f52b456b40e88809afc1910d0611
```

`git status --short -- work data`

```text
```

Empty, as required.

Release-1 pooled score (`scripts/score_corpus.py --run-list data/local/release-1-runs-free.json`)

```text
scored 7 mix(es) [draft truth, matched by work]: likely 8971/10000, listed n/a, recall 6422/10000
```

`likely 8971 / recall 6422` — unmoved, so presentation filtering did not change what is scored.

No live provider call was made; the CLI ran only through `scripts/gate_local_mode.ps1`, no real URL was
analysed, and `idea gc` was never run against the real `work/` tree.

### Could not do

Only the M3U physical deletion, deferred to milestone M2 by `PLAN-v2.md:22` as described above.
