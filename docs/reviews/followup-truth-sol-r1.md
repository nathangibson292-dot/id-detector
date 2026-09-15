## A — Audit of the fixer’s verdicts

No finding was marked “not reproducible” or “already fixed”; all seven were marked reproduced.

| Retro finding | Review result |
|---|---|
| B2 certification independence | **Incomplete.** The new L3 and `benchmark certify` checks work for intact evidence, but other certification and evidence-erasure paths remain. |
| B3 link-safe destinations | **Incomplete.** Review-session pair writes are substantially improved, but `seed_truth` and `freeze_truth` still bypass the pinned writer. |
| B1 bulk offset | **Fixed for the tested flow.** Role endpoints replay the same offset/clamping sequence, preview performs no I/O, and undo owns only timing fields. |
| B4 Windows namespace aliases | **Fixed for the required `\\?\C:`, `\\.\C:`, and UNC spellings.** Containment and lock identity now share `path_key`. |
| A1 crash-atomic pair | **Fixed for annotation/truth commits.** The journal permits completion or byte-for-byte rollback after process death. |
| A2 mixed annotators | **Fixed for the web tool and independent-annotation mode.** The interactive mode preserves foreign rows but can create a contradictory pass header; forward-state guards remain incomplete. |
| A3 frozen terminal state | **Incomplete.** The record itself admits manifests written elsewhere via `--out` are undiscoverable. |
| A4 furniture audit | **Incomplete.** Required leading Unicode cases work, but internal zero-width characters still bypass detection. |

Static comparison shows each principal regression test would fail if its corresponding fix were reverted. The positive-control audit parameters intentionally pass both versions and are not evidence of a fix.

## B — Correctness bugs in the fix

1. **P0 — Cached exposure can be deleted and predictions served without restoring evidence.** At `src/id_detector/truth_review.py:475-492`, the durable write occurs only when `self.predictions_visible` is false. Reopen a session after a reveal, delete `review-exposure.json`, then reveal again: the cached true flag skips the pinned write and `_predictions()` is returned with no exposure file present.

2. **P0 — Another first-pass entry point can erase exposure provenance.** At `src/id_detector/truth.py:586-606` and `:891-916`, `idea truth verify --annotation` writes only the supplied `annotation_provenance`; the CLI supplies none. After deleting the sidecar, a same-annotator re-verification overwrites an exposure-bearing `annotation-first.json` without `review_provenance`. A subsequent second pass, freeze, and L3 score can call that exposed set independent.

3. **P0 — The generic benchmark scorer can still emit `status: "certified"` for exposed truth.** `src/id_detector/benchmark/scorer.py:1323` treats exposed frozen truth as verified, and `:1393-1400` awards certification without consulting `frozen_prediction_exposure`. `idea benchmark score` reaches this directly at `src/id_detector/cli.py:1228-1244`. The new test covers `scripts/score_corpus.py` and `run_certify`, not this certification producer.

4. **P0 — Frozen annotation integrity is recorded but never verified.** `freeze_truth` stores `annotation_passes`, but `src/id_detector/benchmark/scorer.py:1280-1298` checks only truth bytes and exposure evidence. Delete or alter `annotation-second.json` after freezing: L3 and `benchmark certify` can still treat the corpus as frozen, verified, and two-pass. The same code follows replacement symlinks rather than rejecting them.

5. **P0 — Freeze and seed still write through the unsafe atomic writer.** `src/id_detector/truth.py:298`, `:1206-1208`, and `:1245` use `atomic_write_json`; `src/id_detector/io.py:83` resolves the destination before writing. A `ground_truth.json` symlink into `work/` is therefore followed and overwritten during freeze. The fixer’s claim that every set-file replacement uses `PinnedDirectory` is false.

6. **P0 — Freeze does not share the truth lock with reveal/save.** Exposure is sampled at `src/id_detector/truth.py:1129-1139`, followed much later by truth and manifest writes at `:1206-1245`, without a lock. On an eligible set, freeze can observe “unexposed”, a concurrent review can reveal predictions, and freeze can publish an independent manifest. Deleting the un-hashed sidecar then makes L3 certifiable.

7. **P0 — Forward-only state is not enforced outside the web session.** `verify_truth` accepts second-pass or resolved input and `_truth_with_content` clears `second_pass_ref` and `disagreement_resolution` at `src/id_detector/truth.py:852-859`. `second_pass_truth` likewise has no resolved-state guard at `:989-1072`. A CLI call can step resolved truth backwards and overwrite its pass files.

8. **P1 — Legitimately frozen files can be reopened when `--out` is elsewhere.** `covering_freeze_manifest` searches only nearby directories at `src/id_detector/truth.py:407-426`, while the CLI permits an arbitrary manifest destination. The fixer explicitly lists this as non-blocking, but it leaves retro A3 reproducible.

9. **P1 — Internal zero-width characters bypass the furniture audit.** `scripts/audit_fixtures.py:307-315` removes `Cf` characters only at the beginning. A read-only pure-function probe confirmed both `"-\u200b Mall Grab"` and `"0:17:09\u200b - Mall Grab"` return no defect, although they display as stale furniture.

## C — Regressions

No unrelated 4a-ii or `idea.cmd` regression is visible in the diff:

- `src/idea_web/truth_review.py` remains loopback-bound, POST mutations remain behind Host/Origin and CSRF checks, GET remains read-only, and audio routing remains exact.
- `idea.cmd`, playlist-owned files, presentation theme, and `tests/test_phase0a_security.py` are untouched.
- No new dependency was added.

The exposure durability failure in B1 is within the truth-review behavior itself, not a FastAPI routing regression.

## D — Test quality

I could not run `uv` or pytest in this read-only sandbox, so the reported pass counts and exact baseline failure count remain unverified.

I did run the read-only audit with bytecode disabled: `python -B scripts/audit_fixtures.py` reported `audited 460 files` and `fixture audit passed`.

Missing regressions correspond directly to the bugs above:

- Delete exposure after session initialization, then reveal again.
- Re-verify after exposure and assert provenance cannot downgrade.
- Exercise exposed truth through `score_corpus`/`idea benchmark score`.
- Delete or link every frozen annotation pass and require verification failure.
- Freeze through linked truth/manifest destinations and beneath `work/`.
- Interleave freeze with reveal/save.
- Reopen resolved truth through every CLI pass entry point.
- Freeze with an off-tree manifest, then attempt review.
- Test zero-width characters between timestamp, marker, and whitespace.

The three existing tests changed only their injection seams; their assertions were not weakened.

## E — Scope

`git status --short -- data work` is empty. No prohibited second-session file, dependency manifest, `tests/test_phase0a_security.py`, `README.md`, or `idea.cmd` changed. `git diff --check` is clean. All three untracked files were inspected.

## Required fixes

1. **P0** — Make exposure monotonic across every reveal and first-pass writer: re-read/reassert durable evidence under the canonical lock before returning predictions, and OR existing annotation provenance into every replacement.
2. **P0** — Gate `score_corpus_detailed` certification statuses on independence, so no direct caller or `idea benchmark score` can certify exposed truth.
3. **P0** — Re-hash every manifest `annotation_passes` entry and reject missing, altered, symlinked, junction-backed, or escaping evidence.
4. **P0** — Route seed, freeze, manifest, and all truth writes through the pinned, work-aware writer; hold canonical locks while taking the exposure snapshot and publishing the freeze.
5. **P0** — Enforce forward-only state inside the write lock for `verify_truth`, `second_pass_truth`, and `resolve_truth`; refuse resolved/second-pass inputs rather than clearing their fields.
6. **P1** — Make frozen status discoverable independently of arbitrary `--out`, either by constraining the manifest location or persisting a protected per-set freeze marker.
7. **P1** — Normalize or reject `Cf` characters throughout the potential furniture prefix, not only before its first visible character.
8. **P1** — Add the missing adversarial regressions listed in section D and demonstrate each fails against the reverted implementation.

VERDICT: FIX_FIRST