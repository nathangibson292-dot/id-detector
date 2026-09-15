## A — Audit of the fixer’s verdicts

All original retro findings were marked reproduced; none were dismissed as “not reproducible” or “already fixed.”

| Round-2 finding | Final assessment |
|---|---|
| P0-1 exact manifest binding | **Core fix works.** `LoadedTruth` preserves path/raw bytes and the verifier checks entry path, file identity and loaded-byte digest at `src/id_detector/benchmark/scorer.py:1205-1236,1289-1342`. Tests `tests/test_truth_corpus_followup_r2.py:96-133` would fail if reverted. Ancestor-link handling remains incomplete; see B4. |
| P0-2 reveal deletion window | **Fixed only through `reveal_predictions()`’s return.** Predictions are assembled before the final reassertion and returned inside the Windows deletion-denying hold at `src/id_detector/truth_review.py:479-510` and `src/id_detector/truth_paths.py:271-301`. Tests at `tests/test_truth_corpus_followup_r2.py:141-209` would fail if reverted. Exposure can still be lost afterward; see B1. |
| P0-3 validated destination identity | **Fixed for the tested freeze swap and pre-existing-link cases.** Planned `expected_key` values are used at `src/id_detector/truth.py:1297-1324,1368-1380`; seed validates before `makedirs` at `:1266-1294`. Tests `:231-300` would fail if reverted, except the junction-into-`work/` guard that honestly passed before. Adversarial TOCTOU gaps remain below. |
| P1-4 manifest placement | **Incomplete.** Entry paths are now correctly relative to the manifest folder at `src/id_detector/truth.py:1532-1552`, and the test’s set/above variants would fail if reverted. The test uses a truth-file path, however, and misses directory-based scorer/certification discovery; see B3. |
| P1-5 lock deadline/release | **Fixed.** One deadline covers the `RLock` and OS lock, with guard release in `finally`, at `src/id_detector/truth.py:596-618`. Tests `tests/test_truth_corpus_followup_r2.py:345-419` would fail against the prior code. |
| P1-6 re-freeze | **`freeze_truth` itself is fixed.** The in-lock guard is at `src/id_detector/truth.py:1411-1417`; tests `tests/test_truth_corpus_followup_r2.py:427-437` would fail if reverted. Other entry points can still overwrite frozen state; see B2. |

The claimed 16/18 reverted failures are honest: the two old-code passes are guards—the junction-into-`work/` case and corpus-directory manifest placement—not evidence of a fix.

Earlier fixes remain present: first-pass exposure OR-ing (`truth.py:832-846,914-925`), scorer independence gating (`benchmark/scorer.py:1443-1527`), sorted freeze locks through publication (`truth.py:1368-1391`), forward-only in-lock checks (`truth.py:477-503,868-884`), and full-prefix `Cf` handling (`scripts/audit_fixtures.py:304-328`).

## B — Correctness bugs in the fix

1. **P0 — Exposure can realistically be erased after reveal and before the first save.** The undeletable handle closes as `reveal_predictions()` returns at `src/id_detector/truth_review.py:502-505`, before FastAPI serializes and sends the response at `src/idea_web/truth_review.py:95-96`. If the owner subsequently removes the newly generated, still-untracked sidecar—for example with ordinary cleanup—then closes/restarts the tool, initialization sees no exposure at `truth_review.py:456-467`. The restarted save records `false` at `:546-558`; freeze then hashes only the surviving false annotation. Neither freeze nor Git catches this: before its first commit the deleted sidecar has no Git history.

2. **P0 — Ordinary re-running of seed or draft-manifest commands can corrupt frozen/manual corpus state.** `seed_truth` unconditionally calls the replacing writer at `src/id_detector/truth.py:303-305`; `_write_record_file` replaces an existing destination at `:1286-1294` without checking for truth, annotations, exposure, or a covering frozen manifest. Re-running a seed command over reviewed truth converts it back to draft while leaving stale sibling passes. Worse, `write_draft_manifest` accepts non-draft/frozen records and overwrites its destination with `"frozen": false` at `:1567-1597`; `idea truth manifest-draft` exposes this directly at `src/id_detector/cli.py:1858-1872`. Running it against an existing frozen `corpus-version.json` removes the terminal freeze marker.

3. **P1 — Two advertised manifest placements still fail for normal directory-based scoring/certification.** `find_freeze_manifest()` searches only the directory itself when its argument is a directory at `src/id_detector/benchmark/scorer.py:1252-1263`. Thus a manifest accepted in the directory above a corpus is missed by `idea benchmark score --truth <corpus-dir>`, while a set-local manifest is missed when the containing one-set corpus directory is passed. `calibrate/certify.py:135-144` is stricter still and accepts only the corpus-root manifest. The regression at `tests/test_truth_corpus_followup_r2.py:308-323` tests only truth-file entry points, so it does not prove “every advertised placement.”

4. **P1 — The claimed ancestor-link refusal is inconsistent and its promised guidance is absent.** `find_truth_path()` resolves the corpus at `src/id_detector/truth_review.py:92` before checking components, so `idea truth review --corpus <junction-or-symlink-root>` follows it instead of refusing it. The scorer likewise omits the manifest root and its ancestors from `_refuse_links_below()` at `src/id_detector/benchmark/scorer.py:1383-1392`. Freeze’s direct refusal message at `src/id_detector/truth.py:422-428` does not tell the owner to pass the real path. This contradicts Residual risk 6.

## C — Regressions

No unrelated 4a-ii or `idea.cmd` regression is visible:

- Loopback serving remains at `src/idea_web/truth_review.py:110-123`.
- Every mutation remains POST-only behind Host/Origin middleware and CSRF at `:83-99`; GET routes do not mutate.
- Audio routing still requires the exact media-key path at `:50-68`.
- `--set` remains regex-gated and content-selected at `src/id_detector/truth_review.py:87-112`.
- Cross-process locking remains shared by review, verification, later passes, and freeze.
- `idea.cmd`, playlist files, theme, and `tests/test_phase0a_security.py` are untouched.

The second `tests/test_stage2a_truth.py` assertion change is necessary and not weaker: a manifest in `tmp_path` must record `truth/set-one/ground_truth.json`, not the previously unverifiable `set-one/ground_truth.json`.

I independently resolved and SHA-256 checked all 170 entries in the two committed frozen manifests; all paths are relative to their manifest folders, all hashes match, and no annotation pass recorded as `null` has appeared. The 25 paths comprising `release-1` and its descendants, plus all ancestors used by review/freeze/scoring, currently contain no reparse point. Therefore the intended refusal would not break the owner’s present corpus path.

## D — Test quality

I could not run `uv` or pytest in this read-only sandbox, so the fixer’s pytest counts remain unverified. The system Python also lacked the project dependencies needed to invoke the runtime verifier.

I did run the read-only audit with bytecode disabled:

```text
audited 460 files
fixture audit passed
```

The real corpus therefore passes the Unicode furniture heuristic. Static inspection confirms `*NSYNC`, `-M-`, Unicode-minus/hyphen forms without furniture whitespace, and embedded-name joiners pass, while leading whitespace/BOM/zero-width Unicode furniture is caught.

No existing assertion was weakened: scorer expectations only gained independence fields, the truth-review failure seams follow the new writer, and transaction cleanup assertions were strengthened.

Missing regressions:

- Reveal, return, delete the sidecar, restart, save/second-pass/freeze/score.
- Re-seed an existing reviewed/frozen record and run `manifest-draft` over a frozen manifest.
- Verify every accepted manifest placement using `score_corpus_detailed(<directory>)` and `benchmark certify`.
- Pass the corpus through a linked ancestor and require refusal plus “pass the real path” guidance.

## E — Scope

`git status --short -- data work` is empty; the audit did not change it. No dependency file or prohibited second-session file changed. `git diff --check` is clean. All five untracked files were read. No provider, real URL, server, or GC operation was invoked.

## Adversarial residual risks

- **P0 [adversarial]** On POSIX, an actor with directory write permission can unlink exposure during the final hold (`truth_paths.py:283-287`); precise timing between verification and return is required.
- **P0 [adversarial]** A process deliberately ignoring the truth lock can overwrite a set file between an in-lock check and atomic replacement, losing that edit.
- **P0 [adversarial]** `_write_record_file` validates before `os.makedirs` (`truth.py:1275-1287`). An actor swapping the nearest ancestor to a junction in that interval can cause directories to be created under `work/` before the second `record_path` rejects the write.
- **P0 [adversarial]** `expected_key` is a canonical path string, not a stable filesystem ID. Replacing a planned set with a fresh real directory at the same pathname can satisfy it.
- **P0 [adversarial]** A local actor able to rewrite the unsigned manifest can recompute all hashes and falsify certification evidence; the record bounds this honestly to unreviewed local/committed tampering.
- **P1 [adversarial]** A process can hold the `%TEMP%\idea-truth-*.lock` byte lock and deny writers. Mere pre-creation of an ordinary file is not sufficient, so Residual risk 5 overstates that part.

## Required fixes

### Realistic

1. **P0** — Persist exposure in a second monotonic per-set/corpus record that survives deletion of the newly generated sidecar, and consult it inside every save, pass, freeze, scorer, and certification path; add the reveal→delete→restart→certify regression.
2. **P0** — Make `seed_truth` refuse any existing truth destination and make `write_draft_manifest` refuse a covering or destination `frozen: true` manifest; assert every existing truth, annotation, exposure, and manifest byte remains unchanged.
3. **P1** — Either constrain freeze output to the corpus-root manifest used by all consumers, or make directory-based scorer and certification discovery search the same three locations; regression-test all accepted placements through the generic CLI.
4. **P1** — Check link components before resolving corpus/scorer paths, include the manifest root and its ancestors, and tell the owner explicitly to pass the resolved real path.

### Adversarial

1. **P0 [adversarial]** — Root recursive directory creation in a pinned nearest-ancestor handle and carry a stable directory file ID—not only a pathname key—from validation through every pin.
2. **P0 [adversarial]** — Either harden against non-cooperating file writers and unsigned-manifest rewriting, or record explicit owner acceptance that hostile same-user local processes are outside the threat model.
3. **P1 [adversarial]** — Document that an actively held temp lock can deny writes and decide whether per-user protected lock-file placement is required.

VERDICT: FIX_FIRST