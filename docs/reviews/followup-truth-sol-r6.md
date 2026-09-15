## A — Audit of the fixer’s verdicts

The Round 6 “single gateway” verdict is incorrect. Repository-wide search finds exactly one production caller of `open_corpus`: draft-manifest at `src/id_detector/truth.py:2027`.

- No Round 5 finding was marked not reproducible; all six were marked reproduced. The earlier “already refused” junction-into-`work/` control is consistent with the pinned-directory work-root check.
- The frozen-seed, fixed draft destination, exact controlled-target, descendant-link, and nested-scoring tests are causally tied to their local fixes.
- The ledger regression is not deterministic, and the AST guard does not prove the claimed class fix; see D.
- Static review found the earlier exposure OR-ing, evidence hashing, reveal hold, expected-key pinning, forward-only checks, transaction recovery, field preservation, namespace canonicalisation, and lock `finally` release intact.
- `--out` is gone from draft-manifest’s CLI/API, and no production caller or documentation still supplies it.
- The current `release-1` root has no `render_manifest.json`. Directly rendering over that exact root is refused, while an existing marked controlled corpus can be replaced.

Production corpus access inventory:

| Path | Gateway first? |
|---|---|
| Draft-manifest | Yes |
| Seed, verify, second pass, resolve, freeze, ledger append | No |
| Review lookup, GET/session load, reveal and save | No |
| Controlled renderer and CLI | No |
| Generic scorer and `scripts/score_corpus.py` | No |
| Formal certification and calibration validation | No |
| Benchmark corpus runner, shortlist, hints, ablations, transform schedule | No |
| `scripts/make_controlled_predictions.py` | No |
| Fixture/corpus audit | No |

## B — Correctness bugs in the fix

1. **P0 — A mistyped nested seed corrupts a frozen corpus.** `seed_truth` derives the supposed root as the output’s grandparent (`src/id_detector/truth.py:184-195`, `:786-789`) and its final scan is scoped only to the destination set directory (`:347-362`). Seeding:

   `release-1/new-group/new-set/ground_truth.json`

   locks and checks `release-1/new-group`, never the frozen `release-1` root, then creates an invalid nested truth inside the frozen corpus. Verify/second-pass/resolve similarly derive the wrong root at `:1368-1373`, `:1497-1502`, and `:1603-1608`. The Round 6 frozen-seed test covers only `release-1/new-set/ground_truth.json`.

2. **P0 — The renderer can likewise create a nested corpus inside `release-1`.** `render_controlled` locks only the supplied output (`src/id_detector/benchmark/controlled.py:679-695`), and a nonexistent directory is accepted at `:705-710`. A typo such as `--out data/corpus/release-1/new-controlled` publishes a complete nested corpus at `:772-809`, invalidating the frozen layout. It also has no `work_root` prohibition.

3. **P0 — A deleted exposed set can disappear from generic certification.** `truth_is_frozen_verified` builds all manifest entries but checks only currently loaded truths (`src/id_detector/benchmark/scorer.py:1322-1324`) and returns true without asserting manifest-set equality (`:1418`). `corpus_independent` likewise checks only present candidates/truths (`:1453-1460`). If an exposed set is removed from an 11-set frozen corpus, predictions for the remaining ten can satisfy `verified`, `independent`, and the `"certified"` status gate at `:1481-1495` and `:1556-1564`. This regresses exact manifest binding and permits exposed population removal to change the certification claim.

4. **P0 — Calibration contains additional unguarded corpus writers.** `run_calibration_validation` reads through raw `_truth_files` and, by default, writes `calibration-validation.json` directly into the frozen corpus without its lock (`src/id_detector/calibrate/validate.py:308-320`, `:445-446`). `_frozen_subset_truth` also creates truth records and a frozen manifest beneath `work/` (`:284-295`, `:473-487`), contrary to the stated work-tree rule.

5. **P1 — The gateway itself performs resolution/read before link refusal.** On mutation, `open_corpus` acquires `corpus_write_lock` and reads frozen status before calling `build()` (`src/id_detector/truth.py:987-996`). Lock identity invokes `path_key` at `:809`, which resolves the path, while `corpus_manifest_frozen` may read the manifest at `:936-948`. Root link refusal occurs only later through `require_corpus_directory` at `:974-977`. The gateway also records `work_root` but does not enforce that the root is outside it.

6. **P1 — Multiple readers still bypass both the gateway and, in several cases, its validation.** Direct validator bypasses include review (`src/id_detector/truth_review.py:91-111`), scorer (`src/id_detector/benchmark/scorer.py:1225-1244`), and certification (`src/id_detector/calibrate/certify.py:140-187`). Raw traversal remains in:

   - `src/id_detector/benchmark/corpus.py:65-76`
   - `src/id_detector/benchmark/ablations.py:132-150`
   - `src/id_detector/benchmark/transforms_schedule.py:92-96`
   - `src/id_detector/calibrate/certify.py:253-256`
   - `src/id_detector/calibrate/validate.py:315-320`
   - `scripts/score_corpus.py:437-449`
   - `scripts/audit_fixtures.py:351-368`

   These can enumerate or read linked/nested corpus content before a later scorer rejects it.

## C — Regressions

- Round 2’s exact-manifest-binding guarantee is incomplete because surplus/missing manifest entries are not checked.
- Round 5’s frozen-seed and controlled-render classes remain open through nested output paths.
- No regression was found in the loopback server, CSRF plus Origin/Host mutation guard, mutation-free GET routes, traversal-safe audio/set IDs, exposure-before-response ordering, or cross-process save lock.
- Bulk offset still applies the same translated/clamped boundaries to role segments; preview remains non-persistent and undo owns only offset timing fields.
- `*NSYNC`, `-M-`, and deliberate Unicode punctuation without the old parser’s separator grammar remain accepted. Unicode/BOM/zero-width furniture cases are detected.
- Both committed frozen controlled manifests passed a read-only static digest/state audit. This was not an execution of the project verifier.

## D — Test quality

1. **P1 — The AST guard is easily bypassed and already misses real bypasses.** It scans only five files and six attribute names (`tests/test_truth_gateway_guard.py:19-45`). It misses `open`, `Path.open`, `read_text`, `read_bytes`, `glob.glob`, helper extraction, and all the raw readers listed above. It never asserts that public paths call `open_corpus`, and it allowlists the whole renderer function.

2. **P1 — The ledger test releases both children concurrently but does not synchronize the actual read-modify-write.** Each child’s pre-barrier read at `tests/test_truth_corpus_followup_r4.py:204-216` is outside `record_exposure_in_ledger`, which rereads the ledger internally at `src/id_detector/truth.py:512`. Without the lock, scheduling one complete append before the other can still pass. The claimed revert failure is probabilistic.

3. **P1 — Existing assertions were weakened.** The review link test now accepts the old silent-skip `"set not found"` behavior despite claiming to require link refusal (`tests/test_truth_review.py:784-789`). The controlled failed-render test now checks only an unchanged marker and that some audio exists, rather than byte-identical preservation of the previously published corpus and audio (`tests/test_stage2a_controlled.py:177-202`).

4. The 22 deselections do not indicate a new hidden normal gate. `pyproject.toml:47-54` still excludes `slow` and `live`; `tests/conftest.py:14-28` still marks the same four modules slow, while three new renderer tests are explicitly slow and were reportedly run separately.

I could not run `uv` or pytest in this read-only sandbox. I did run the read-only fixture audit with `python -B`; it reported 460 files audited and passed.

## E — Scope

- No changes exist under `data/corpus/`, `work/`, playlists, `README.md`, `idea.cmd`, theme code, `tests/test_phase0a_security.py`, `io.py`, `jobs.py`, `retention.py`, or `tests/test_phase2b_retention.py`.
- No dependency or pytest-configuration change was made.
- All untracked files were reviewed. `git diff --check` was clean.
- No provider, live URL, pipeline, or garbage-collection command was invoked.

## Required fixes

### Realistic

1. **P0:** Make every mutating entry point accept an explicit corpus root, enter `open_corpus(..., mutate=True)` before any predicate, and select destinations/records through its handle. Add frozen-parent regressions for nested seed, verify, review, freeze, and controlled-render paths.
2. **P0:** Route calibration validation through the gateway; move generated reports outside frozen corpus roots and stop constructing truth corpora beneath `work/`.
3. **P0:** Require a directory score’s loaded `(set_id, relative path)` inventory to exactly equal the frozen manifest inventory before verified/independent status is possible. Add a regression deleting an exposed manifest set and proving certification is impossible.
4. **P1:** Move root link refusal ahead of lock-key resolution and manifest reads, and make `open_corpus` itself reject mutation roots within `work_root`.
5. **P1:** Migrate every listed reader to `open_corpus(..., mutate=False)` and consume only `CorpusHandle.truth_files`.
6. **P1:** Replace the five-file call-name AST test with repo-wide enforcement plus direct public-entry-point tests proving every corpus path invokes the gateway.
7. **P1:** Put the ledger test barrier inside the append’s post-read/pre-write window, and restore exact link-refusal and byte-for-byte failed-render assertions.

### Adversarial

8. **P2:** Validate the controlled marker as a real, non-link, schema-correct marker tied to that target corpus; the current presence-only `isfile` check would trust an accidentally copied or linked marker.

VERDICT: FIX_FIRST