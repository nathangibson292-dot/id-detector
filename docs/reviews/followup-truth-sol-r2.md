## A — Audit of the fixer’s verdicts

The record marks all nine findings reproduced and fixed. Static review confirms each cited regression would fail against the pre-pass implementation, but four findings remain incomplete.

| Round-1 finding | Round-2 verification |
|---|---|
| B1 cached exposure (P0) | **Incomplete.** The fix at `src/id_detector/truth_review.py:479-499` rewrites a missing sidecar on every reveal. `tests/test_truth_corpus_followup_sol.py:71-88` would fail when reverted (`[True, False]`). However, an unlocked actor can delete the sidecar while `_predictions()` runs; see B1 below. |
| B2 first-pass provenance downgrade (P0) | **Fixed.** `_pinned_exposure` and the annotation builder at `src/id_detector/truth.py:820-923` OR existing exposure into every first-pass replacement. Tests at `tests/test_truth_corpus_followup_sol.py:107-128` exercise annotation and interactive verification and would observe `False` if reverted. |
| B3 generic scorer certification (P0) | **Fixed in this path.** `src/id_detector/benchmark/scorer.py:1348-1362,1383-1389,1458-1466` gates `"certified"` on independence. `tests/test_truth_corpus_followup_sol.py:175-206` has a certifying independent control and becomes certified when the gate is reverted. Upstream evidence/integrity bypasses remain under B1/B2 below. |
| B4 frozen-file integrity (P0) | **Incomplete.** `src/id_detector/benchmark/scorer.py:1265-1345` now hashes recorded truth, evidence, and annotations and detects added passes. Tests at `tests/test_truth_corpus_followup_sol.py:217-263` would fail when reverted. It still does not prove that the truth being scored is the manifest’s hashed truth, nor reject all linked ancestors/manifests; see B2. |
| B5 unsafe seed/freeze writers (P0) | **Incomplete.** Writes now reach `PinnedDirectory` through `src/id_detector/truth.py:1254-1263,1332-1455` and `src/id_detector/truth_paths.py:194-234`. Tests at `tests/test_truth_corpus_followup_sol.py:271-350` would overwrite/follow links when reverted. The validation-to-pin directory-swap race remains; see B3. |
| B6 freeze snapshot race (P0) | **Fixed for cooperating writers.** Locks are acquired in sorted `path_key` order at `src/id_detector/truth.py:1332-1342` and held through manifest publication. `tests/test_truth_corpus_followup_sol.py:358-394` would allow the reveal to land in the old implementation. Lock timeout behavior itself remains defective; see B5. |
| B7 backward state transitions (P0) | **Fixed.** Guards at `src/id_detector/truth.py:473-499` are repeated with a truth-byte digest check inside the lock at `:837-880`; all three entry points use it at `:994-1250`. The seven negative cases at `tests/test_truth_corpus_followup_sol.py:458-491` would rewrite state when reverted. Legitimate unresolved resolution remains allowed and is exercised by `tests/test_stage2a_truth.py:127-149`. |
| B8 frozen-manifest discovery (P1) | **Incomplete.** `src/id_detector/truth.py:1291-1312` rejects wrong names and undiscoverable directories; `tests/test_truth_corpus_followup_sol.py:499-516` would fail when reverted. Two advertised discoverable locations generate incorrect relative paths; see B4. |
| B9 internal invisible furniture (P1) | **Fixed.** `scripts/audit_fixtures.py:277-347` removes `Cf` throughout matching while preserving joiners after the first letter. Tests at `tests/test_truth_corpus_followup_sol.py:524-548` would miss the six internal cases when reverted. A read-only probe also confirmed `*NSYNC`, `-M-`, `−M−`, Unicode-hyphen variants, and `Mall‍Grab` pass. |

No verdict was marked “not reproducible” or “already fixed.” The record’s three explicit corrections are directionally right, but its claims of “full frozen re-verification” and fully pinned destinations remain overclaims.

## B — Correctness bugs in the fix

1. **P0 — Exposure can still disappear between rewrite and response.** `src/id_detector/truth_review.py:492-499` writes the sidecar, sets an in-memory flag, then calls `_predictions()`. `PinnedDirectory` holds only the set directory; it does not keep the sidecar open against deletion. A concurrent actor that ignores the advisory truth lock can unlink it while predictions are assembled. If the process then exits before save, restart sees no evidence and the set can be verified, frozen as independent, and scored into L3. The current regression merely observes that the sidecar exists when `_predictions()` begins.

2. **P0 — The manifest can authenticate a different truth file from the one scored.** `load_truth_directory` loads the caller-selected file at `src/id_detector/benchmark/scorer.py:1199-1210`, but `truth_is_frozen_verified` selects a manifest entry only by `set_id` and hashes that entry’s separate path at `:1282-1295`. Copy a frozen set into a new sibling below the same corpus, alter its labels/times while retaining `set_id`, `corpus_version`, and verified flags, then point `idea benchmark score` or an L3 run-list at the copy. The ancestor manifest is found, the untouched original is hash-verified, and the altered copy is scored as verified and independent. With enough copied sets, certification statuses can become `"certified"`. The verifier also follows a linked `corpus-version.json` at `:1232-1237` and checks only final file components, not a parent junction escaping the set.

3. **P0 — Destination identity is not carried from validation into the pin.** `record_path` checks only the final file and resolves its parent at `src/id_detector/truth.py:412-426`. Freeze validates paths, then later recomputes lock identities and opens directories without an `expected_key` at `:1332-1342`. Moving a set after `_freeze_plan` and replacing its old name with a junction lets freeze lock and pin the replacement target, then overwrite truth there. The same gap exists in `_write_record_file` at `:1257-1263`; it additionally calls `os.makedirs` before validating against `work/`, so a rejected seed can already have created directories beneath work.

4. **P1 — Two advertised manifest locations strand the freeze.** `_manifest_destination` permits the set directory, corpus directory, or directory above at `src/id_detector/truth.py:1291-1312`, but `_publish_freeze` always makes entry paths relative to `truth_dir` at `:1483-1501`. The verifier interprets them relative to the manifest directory. For `freeze_truth(/corpus/set, out=/corpus/corpus-version.json)`, the manifest records `ground_truth.json`, and verification searches `/corpus/ground_truth.json` instead of `/corpus/set/ground_truth.json`. Only the same-directory case used by the changed test works.

5. **P1 — The advertised lock timeout does not apply to competing threads.** `_TruthLock.acquire` blocks indefinitely on `self._guard.acquire()` at `src/id_detector/truth.py:592-599`; the timeout only governs the later OS lock. A same-process thread holding a record lock can therefore hang freeze/reveal/save forever. Also, if `_unlock_os()` raises at `:602-606`, `_guard.release()` is skipped, leaking the in-process lock. Existing tests cover another process and same-thread re-entry, not competing-thread timeout or release after exceptions.

6. **P1 — An already-frozen corpus can be rewritten by freeze again.** `src/id_detector/truth.py:1365-1441` validates content but never calls `refuse_frozen`; `:1476-1512` then replaces every truth and the manifest. Re-running freeze with another version mutates a state the tool otherwise declares terminal. There is no idempotency or refusal test.

## C — Regressions

Static inspection finds no unrelated 4a-ii or `idea.cmd` regression:

- Loopback construction remains at `src/idea_web/truth_review.py:110-123`.
- GET routes remain non-mutating; audio requires the exact media-key path at `:50-68`.
- Every POST passes the loopback Host/Origin middleware and CSRF check before either mutation at `:83-99`.
- `--set` remains regex-gated and content-selected at `src/id_detector/truth_review.py:87-112`.
- The cross-process writer lock remains shared by review, verify, second pass, resolve, and freeze.

Calibration validation is not independence-gated, but `src/id_detector/calibrate/validate.py:404-505` explicitly emits only controlled, provisional, zero-denominator certification entries and labels the artifact “not real-mix certification.” It cannot currently emit a certification-bearing accuracy claim.

The `tests/test_stage2a_truth.py` edit is necessary for the new naming/location constraint, and its assertion is unchanged. Existing committed frozen manifests are all named `corpus-version.json` at their corpus roots, so the constraint does not strand them.

## D — Test quality

I could not run `uv` or pytest in this read-only sandbox; the reported 627/734 pass counts remain unverified. Static counting supports the fixer’s “31 failures” claim, and every principal regression in section A would fail if its production fix were reverted.

I did run the read-only audit with bytecode disabled: `audited 460 files` and `fixture audit passed`.

Missing regressions:

- Delete exposure during `_predictions()`, then restart and attempt certification.
- Score a modified duplicate whose `set_id` points to a different manifest entry path.
- Replace a set directory between freeze planning and pinning, including a junction into a sibling set and into `work/`.
- Verify every advertised manifest location through `truth_is_frozen_verified`.
- Competing-thread lock timeout, partial multi-lock acquisition cleanup, and unlock exceptions.
- Attempt to freeze an already-frozen corpus.
- Reject linked manifests and reparse-point ancestors, not only linked leaf files.

The existing test edits are not weakened: two failure seams moved to `_write_set_file`, one to `_write_exposure`, transaction cleanup was strengthened, and scorer expectations only gained the new independence field.

## E — Scope

`git status --short -- data work` is empty. No dependency file or prohibited second-session file changed: playlists, `tests/test_playlists.py`, `README.md`, `idea.cmd`, presentation theme, and `tests/test_phase0a_security.py` are untouched. `git diff --check` is clean. All four untracked files were read.

## Required fixes

1. **P0** — Bind each loaded truth path and its exact raw bytes to the corresponding manifest entry; reject a different same-`set_id` location, linked manifest, and any reparse component escaping the set.
2. **P0** — Close the reveal deletion window: assemble predictions before the final exposure commit, then reassert and verify durable evidence immediately before returning while holding whatever deletion-denying handle is available; fail closed and add the concurrent-unlink regression.
3. **P0** — Preserve the validated canonical directory identity through every seed/freeze/manifest pin using `expected_key`; reject pre-existing ancestor links and validate the nearest existing ancestor before creating directories.
4. **P1** — Either constrain manifests to `truth_dir/corpus-version.json` or compute every manifest entry relative to the actual manifest directory; test all accepted placements through review and scoring.
5. **P1** — Apply the deadline to the in-process `RLock`, guarantee `_guard.release()` even when OS unlock fails, and test timeout and cleanup after partial multi-record acquisition.
6. **P1** — Make freeze refuse an existing covering frozen manifest, or make an identical re-freeze strictly read-only and reject any version/content change.

VERDICT: FIX_FIRST