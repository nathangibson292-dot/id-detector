# Build — offline accuracy pass (`fusion:6`)

Uncommitted on worktree `agent-acc01` at `596e24d`. No commit, no branch, no dependency. Nothing
under `present/**`, `idea_web` templates/pages/progress, `webapp/**`, `playlists/**`,
`README.md` or `idea.cmd` was touched. The golden Local Free file is unchanged and its test passes.

**No provider request left this machine.** Every measurement ran on copies of the owner's cache
(`C:\acc01\w`, `C:\acc01\s-before`, `C:\acc01\s-after`; 1,238 non-audio files each, raw replies and
audio excluded) inside `scripts/offline_guard.py`, with `IDEA_TEST_MODE=1`, an empty
`AUDD_API_TOKEN` and `IDEA_ENGINE_SHAZAM=off` set for those processes only: **0 network attempts in
every run**. The owner's `work/`, `data/` and `id-detector-deepscan` were only read. One scratch
slip to own up to: my first copy command still pointed at a previous builder's disposable scratch
copy (`C:\ifa\w`) and re-copied the same cache files over it twice. That folder is outside the repo
and is not owner data.

## Result

| Same evidence, same scorer | work recall | work precision | `likely` |
|---|---|---|---|
| Before: HEAD, `fusion:5` (the owner's stored results) | 164/218 · 75.2 % | 166/198 · 83.8 % | 63/63 · 100 % |
| **After: `fusion:6`** | **165/218 · 75.7 %** | **167/199 · 83.9 %** | **64/64 · 100 %** |

Both sides were measured through the **real start-up path**. `refuse_stale_result` ran on two fresh
copies of the cache. On the HEAD copy every free mix was already `current` at fusion:5. On the new
copy 12 mixes were re-fused in 75.5 s. The two Deep mixes were refused as paid and `819e1ee83956`
was refused for its inconsistent sidecar, as before. Then `make_run_list` resolved all seven to
`bundle result, fusion:5` and `fusion:6` respectively, and `score_corpus` printed "Every run was
decided by fusion:5" and "… by fusion:6". The in-memory harness gives identical numbers. The two
precision figures are each the score of one fusion version; no score pools two versions.

Note on the baseline: the last accuracy report gave 167/198. HEAD gives 166/198 on the same
evidence. The difference is one display label: Mall Grab 17:15 is now shown and scored as
"kettama - let me see" instead of "KETTAMA - ID (LET ME SEE U / ROK DA HOUSE!) (UR)". The identity
nodes are identical, so the cause is in the presentation (`present/exports.py`
`_fuller_crowd_label`), which is UI-owned. I recorded it and did not touch it.

Per mix (recall · work precision · likely):

| Mix | Before (`fusion:5`) | After (`fusion:6`) |
|---|---|---|
| `dj-heartstring-youtube-set` | 11/21 · 11/14 · 4/4 | 11/21 · 11/14 · 4/4 |
| `final-new-set-christmas` | 20/23 · 20/22 · 11/11 | 20/23 · 20/22 · 11/11 |
| `mall-grab-boiler-room-melbourne-22` | 17/24 · 18/25 · 3/3 | 17/24 · 18/25 · 3/3 |
| `mph-youtube-set` | 34/62 · 34/41 · 10/10 | **35/62 · 35/42 · 11/11** |
| `redo-of-best-set` | 17/18 · 18/25 · 10/10 | 17/18 · 18/25 · 10/10 |
| `new-mix-jan-24th` | 27/30 · 27/31 · 10/10 | 27/30 · 27/31 · 10/10 |
| `boomtown-mix` | 38/40 · 38/40 · 15/15 | 38/40 · 38/40 · 15/15 |
| **Pooled** | **164/218 · 166/198 · 63/63** | **165/218 · 167/199 · 64/64** |

**Every page row that changed, across all 15 re-fusable cached mixes** (truth or not):
- added: MPH set, 32:27, "MPH - Brainwashing" (`unclear`, one 12 s window). Truth places it at
  31:36 plus the set's usual +17 s clock, so the row sits about 40 s after it, inside the comment's
  lead-in reach. **A track gained.**
- badge up: MPH "Joy Orbison - flight fm" possible → likely, "MPH - Doubt" unclear → possible,
  "MPH - Raw" unclear → possible. All three are correct, and the `likely` one keeps `likely` at
  100 %.
- nothing else changed on any mix, with or without truth. No row was removed and no wrong row was
  added.

This is a small gain: one track, from one mix. It is shipped because it qualifies, because the
mechanism is sound, and because it changes nothing anywhere else. It is not a breakthrough.

## 1. Where the misses and the wrong rows are (from stored evidence)

**54 missed truth works.** I searched each one across every stored observation, every episode
(listed or hidden, by bucket), every listed row's alternatives and every hint:

| Where the missed track is in the stored evidence | Works | Recoverable offline? |
|---|---|---|
| Nowhere: no recognition, no hint ("ID" rows, owner's own unreleased edits) | 13 | No |
| Only in a listener's comment tracklist / MixesDB line (no audio heard it) | 36 | Only by listing the tracklist itself (see idea 4); not honestly measurable |
| Only in an unpositioned comment answer (Simula - Daddy Issues, Chase & Status - Headtop) | 2 | No: no time, no audio |
| Heard by the engine, hidden as `short` | 3 | Brainwashing (12 s + a comment tracklist line): **recovered**; DJ SCHEMA (12 s, no hint); Cesco (24 s, no hint): see idea 2 |
| In a hidden `ambiguous` / `buried` / `contradicted` / `scatter` episode, or as an alternative | **0** | Nothing to recover |

Hidden buckets, every episode scored by the scorer's own rule: ambiguous 102 wrong / 0 right,
buried 25 / 0, contradicted 23 / 0, scatter 7 / 0, short 215 / 24. Of the 24 right `short`
episodes, 21 are extra fragments of works already listed and 3 are the misses above. **The
suppression rules are hiding nothing correct.**

**37 wrong listed rows (32 works).** Most of the cost sits in 30–59 s rows with no comment support:
22 wrong rows against 37 right. The features I checked are on-air time, window count, anchor share,
ambiguous windows, alternatives, global alignment, hull/on-air and overlap with neighbouring rows.
None of them separates those rows without losing correct tracks (for example, "no global alignment
and no alternatives" drops 20 wrong rows but also Diffrent, Prozak, Testpress and Skin On Skin).
Three wrong rows are arguably the right song under another credit or version: Mark Krupp vs Boy
Better Know "Too Many Man", Headie One vs Beau James "18Hunna", and "I'm ready - Upper90". Crediting
them would mean changing the scorer, which this pass does not do. The residual wrong rows named in
the last report (De Barrio, Mears, iluv) still stand.

## 2. Ideas tried

| # | Idea | Measured (pooled recall · precision · likely) | Verdict |
|---|---|---|---|
| 1 | **A listener's comment tracklist line may corroborate a recognised track** (until now only uploader/pinned/MixesDB/1001TL lines could; any single listener *answer* already could) | 164→**165** · 166/198→**167/199** · 63/63→**64/64** | **Kept** |
| 2 | Lower the 30 s on-air floor (floor sweep on the idea-1 results) | 12 s: 167 · 174/402; 21 s: 166 · 168/228; 24 s: 166 · 168/211; **30 s: 165 · 167/199**; 33 s: 164 · 166/192; 36 s: 163 · 165/191; 42 s: 162 · 164/188 | Dropped: 30 s is still the best; 24 s buys Cesco for 12 wrong works |
| 3 | A weak row mostly (≥ 60 %) under a longer listed row is buried | 165→**164** (loses Deadly Habitz - Henney & Coke); removes only a duplicate Mears row | Dropped |
| 4 | Tracklist lines also *contradict* (hide) a different engine row at their position | 165→**156** · 167/199→158/187 | Dropped: listeners' clocks run 17–84 s off the video, so lines hide the right track (9 correct MPH tracks lost) |
| 5 | List comment tracklist lines as rows of their own (hint-only) | not built | Rejected unmeasured: on the three timed mixes the truth *is* the public comment tracklist, so any "gain" is the answer key scoring itself; idea 4 shows the clocks are too loose to place them anyway |
| 6 | Merge fragments of one track split across windows | — | Nothing to gain: no missed work has two or more stored fragments; merging duplicates of wrong works leaves work precision unchanged |
| 7 | Recover hidden candidates (ambiguous/buried/contradicted/scatter, alternatives) | — | Nothing to gain: 0 correct episodes in those buckets |
| 8 | Boundary/timestamp accuracy | start error after removing each set's clock offset (median absolute deviation): DJ Heartstring 57 s, Mall Grab 47 s, MPH 15 s for the first matched window; the extrapolated `best_start_ms` is no better (57/48/15 s) | Dropped: no estimator in the stored evidence does better, the truth clocks are themselves offset and unverified, and row timing lives in UI-owned presentation code |

**Why idea 1 is not circular, although the MPH truth is that same comment tracklist.** The line only
*corroborates*. It counts only when it resolves to the **same identity work the recogniser
independently heard** and sits in reach of that play (the rule a single listener answer already
follows). Every gain therefore needs the audio and the list to agree. A wrong listing would need a
listener to type exactly the wrong track the engine misheard. The line still never names a track by
itself (no hint-only row from a tracklist line) and never hides one (contradiction stays
answers/corrections only). Idea 4 shows why that matters.

## 3. Leave-one-mix-out

No threshold was tuned: the change is one rule. For each held-out mix: the keep/drop verdict on the
other six (under the brief's rule), then what the change does to the held-out mix.

| Held out | Train (6 mixes) before → after (recall · precision · likely) | Train verdict | Held-out before → after |
|---|---|---|---|
| `dj-heartstring-youtube-set` | 153/197 · 155/184 (84.2%) · 59/59 → 154/197 · 156/185 (84.3%) · 60/60 | keep | 11/21 · 11/14 · 4/4 → unchanged |
| `final-new-set-christmas` | 144/195 · 146/176 (83.0%) · 52/52 → 145/195 · 147/177 (83.1%) · 53/53 | keep | 20/23 · 20/22 · 11/11 → unchanged |
| `mall-grab-boiler-room-melbourne-22` | 147/194 · 148/173 (85.5%) · 60/60 → 148/194 · 149/174 (85.6%) · 61/61 | keep | 17/24 · 18/25 · 3/3 → unchanged |
| `mph-youtube-set` | 130/156 · 132/157 (84.1%) · 53/53 → unchanged | no change | 34/62 · 34/41 · 10/10 → **35/62 · 35/42 · 11/11** |
| `redo-of-best-set` | 147/200 · 148/173 (85.5%) · 53/53 → 148/200 · 149/174 (85.6%) · 54/54 | keep | 17/18 · 18/25 · 10/10 → unchanged |
| `new-mix-jan-24th` | 137/188 · 139/167 (83.2%) · 53/53 → 138/188 · 140/168 (83.3%) · 54/54 | keep | 27/30 · 27/31 · 10/10 → unchanged |
| `boomtown-mix` | 126/178 · 128/158 (81.0%) · 48/48 → 127/178 · 129/159 (81.1%) · 49/49 | keep | 38/40 · 38/40 · 15/15 → unchanged |

The change never hurts a held-out mix. Its gain comes entirely from MPH. When MPH is held out, the
other six show no change, so they would not have chosen the rule on their own. They would not have
been harmed by it either. The evidence is one track, and the report says so.

## 4. Code

- `src/id_detector/fuse/episodes.py` `_eligible_tracklist_hint`: a tracklist line from anyone is
  eligible to corroborate, under the unchanged guards (verified mirror, not `id_unknown`) and the
  unchanged matching (same identity work, in reach). The comment states why it never lists or hides
  a track by itself.
- `src/id_detector/recipes.py`: `FUSION_VERSION` 5 → 6, with a line saying what 6 is. Stored free
  results re-fuse offline at start-up (above). Deep results stay refused as before.

## 5. Tests

New: `tests/test_comment_tracklist_corroboration.py` (5 tests). A listener's line is eligible, and
quarantined or `id_unknown` lines are still not. A listener's line lifts a 12 s recognised track
above the floor without retiming it. A line naming another track backs nothing, hides nothing and
makes no crowd row. A line out of reach backs nothing. The version is 6.
**Revert proof:** with HEAD's `episodes.py` restored, `2 failed, 3 passed` (eligibility; short
track listed). The two "backs nothing" tests are guards that pass either way by design.

Existing assertions changed:
- Version pins `fusion:5` → `fusion:6` (the bump): `test_phase0a_money.py` (5),
  `test_phase0a_status.py` (2), `test_phase1a_compat.py` (3), `test_phase1b_targeting.py` (4),
  `test_refusion.py` (9) and `idea_web/test_refusion_money.py` (3).
- `test_refusion.py::test_the_upkeep_pass_re_fuses_stored_results_offline` asserted
  `content="26"`, but HEAD's `PAGE_VERSION` is 27 (bumped in `596e24d` without updating this
  assertion), so it fails at HEAD in both parametrisations. It now reads `PAGE_VERSION` from
  `present/page.py` (a test-only import; no UI code changed).

## 6. Gate outputs (final tree, foreground, `IDEA_TEST_MODE=1`, empty `AUDD_API_TOKEN`)

`uv run pytest --collect-only -q -p no:cacheprovider` → `2216/2313 tests collected (97 deselected)`.

```text
shard A  (tests/idea_web tests/test_[a-o]*.py tests/test_phase*.py)
1216 passed, 1 warning in 1636.71s (0:27:16)
shard B  (the other 59 root test files)
998 passed, 2 skipped, 97 deselected, 1 warning in 927.46s (0:15:27)
```

1216 + 998 + 2 = 2216 = collected; 0 failed. None of the known load-sensitive tests failed. Both
shards outran the tool's 10-minute foreground limit and finished in the background; each was waited
for, and nothing else ran meanwhile. A test side effect created the git-ignored
`data/local/hints/manual_tracklist/` in the worktree; it was deleted.

```text
$ uv run ruff check .
All checks passed!
$ uv run ruff format --check .
451 files already formatted
$ uv run python scripts/audit_fixtures.py
audited 569 files
fixture audit passed
```

`git diff --stat` (tracked files; plus the untracked new test file and this report):

```text
 src/id_detector/fuse/episodes.py      | 21 +++++++++++----------
 src/id_detector/recipes.py            |  4 +++-
 tests/idea_web/test_refusion_money.py |  6 +++---
 tests/test_phase0a_money.py           | 10 +++++-----
 tests/test_phase0a_status.py          |  4 ++--
 tests/test_phase1a_compat.py          |  6 +++---
 tests/test_phase1b_targeting.py       |  8 ++++----
 tests/test_refusion.py                | 21 +++++++++++----------
 8 files changed, 42 insertions(+), 38 deletions(-)
```

`git status --short`:

```text
 M src/id_detector/fuse/episodes.py
 M src/id_detector/recipes.py
 M tests/idea_web/test_refusion_money.py
 M tests/test_phase0a_money.py
 M tests/test_phase0a_status.py
 M tests/test_phase1a_compat.py
 M tests/test_phase1b_targeting.py
 M tests/test_refusion.py
?? docs/reviews/build-accuracy-pass.md
?? tests/test_comment_tracklist_corroboration.py
```

`git status --short -- data work` is empty.
