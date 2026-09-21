## A — Claim paths

| Path | Evidence and verdict |
|---|---|
| `idea truth freeze` | **P0.** A mixed corpus records clean sets as `"certifiable": true` solely from that set’s exposure state (`src/id_detector/truth.py:2566-2575`), even when another frozen set is exposed and the complete corpus is not independent. |
| `idea benchmark score` | Scope-safe: verification and complete-corpus independence feed `whole_corpus` (`src/id_detector/benchmark/scorer.py:1672-1680`), required before `"certified"` (`:1750-1759`). A single record from a larger corpus stays provisional. |
| `idea benchmark certify` | Its report uses the safe generic scorer after frozen/inventory and independence checks (`src/id_detector/calibrate/certify.py:263-264`, `:324-342`). However, the CLI always prints `certified corpus=...` even when `n_certified == 0` (`src/id_detector/cli.py:1500-1503`), which is misleading. Its exactly-once protection also has a realistic race; see D. |
| `idea benchmark links-score` | **P0.** Arbitrary marked JSON can yield `"pass": true` and `"status": "certified"` without any corpus, manifest, or independence check (`src/id_detector/enrich/benchmark.py:112-145`). |
| `idea benchmark freeze-profiles` | **P0.** It reads caller-selected report JSON and emits feature `certified: true` based only on comparison values and the open gate (`src/id_detector/profiles.py:246-262`, `:635-664`). No source-corpus scope or independence is verified. |
| `scripts/score_corpus.py` | **P0.** `thresholds_met` is calculated before `certification_scope` and only suppressed when the gate is closed (`scripts/score_corpus.py:1040-1065`). Thus a perfect partial/draft run emits `"thresholds_met": true` (`:1083`) and prints “all three thresholds are met” (`:1314-1317`) despite `certifiable: false`. |
| Calibrated episodes | **P0.** Any loaded frozen calibration model’s `"certified"` entries propagate while the gate is open (`src/id_detector/fuse/episodes.py:664-682`); model loading validates shape and `frozen`, not its certification provenance (`src/id_detector/calibrate/model.py:472-485`). |
| Calibration validation | Safe against positive output: its model begins provisional, its validation text says controlled/not certification, and `_score_certification` forcibly returns zero-denominator provisional entries (`src/id_detector/calibrate/validate.py:468-510`, `:571-584`). |

Certification is therefore not safe to enable.

## B — Defect 1

The new core scope rule mostly works:

- Canonical `path_key` identities collapse case, relative, namespace-prefixed, and equivalent spellings (`src/id_detector/benchmark/scorer.py:1554-1562`; `src/id_detector/truth_paths.py:61-88`).
- Missing and extra frozen records are refused by exact manifest inventory comparison (`src/id_detector/truth.py:1459-1488`).
- Mixed corpora and standalone truth are non-certifiable (`src/id_detector/benchmark/scorer.py:1563-1586`).
- Missing sets are reported, while exposure is evaluated across every on-disk frozen record (`:1602-1645`).
- Duplicate run-list sets are rejected before pooling (`scripts/score_corpus.py:1020-1030`). The helper itself deduplicates paths, but no production claim reaches it with duplicate sets.
- Draft/unfrozen run lists still score and receive development-only reasons.
- A ledger line added after freezing is refused through the exact frozen-ledger check (`src/id_detector/benchmark/scorer.py:1464-1467`).

Nevertheless, Defect 1 is not closed for the complete output contract: the separate `thresholds_met` and printed-success channels remain positive on partial or draft input. The new partial test checks only `certifiable` and the later “NOT CERTIFIABLE” text (`tests/test_certification_followup.py:196-216`), allowing the contradictory positive sentence to pass.

## C — Defect 2

Relative paths, case, existing folders, an existing junction/symlink component, and `--work-root` equal to or above the system temp directory are correctly refused (`src/id_detector/truth.py:1273-1286`; `src/id_detector/truth_paths.py:123-180`).

**P0 bypass:** a scratch directory can still be created inside a manifest-less corpus through an existing ordinary subdirectory. For example, with `TEMP=<release-1>/temp-folder`, `corpus_ancestor` finds no marker directly in `release-1`, while the fallback checks only the nearest existing parent, `temp-folder`, for set children (`src/id_detector/truth.py:1287-1290`). It never checks that an earlier ancestor is the manifest-less corpus root. `_score_certification` then creates and populates the scratch there (`src/id_detector/calibrate/validate.py:553-559`) before deleting it.

The tests cover the corpus root, a frozen root, and a set directory, but not an ordinary nested directory inside a manifest-less corpus (`tests/test_certification_followup.py:428-449`).

## D — Certify outputs and freezing

- Report and prediction paths receive initial corpus/link/work-root validation (`src/id_detector/calibrate/certify.py:257-261`).
- Predictions are revalidated with `work_root` immediately before writing (`:340-341`).
- The report is revalidated by the scorer before writing (`src/id_detector/benchmark/scorer.py:1670`, `:1786-1788`), but that second validation omits `work_root`.
- The registry is a third output and receives neither the explicit work-root guard nor locking (`src/id_detector/calibrate/certify.py:269-270`, `:344`).
- Two tabs can both pass the registry/report checks, perform the evaluation twice, overwrite/interleave artifacts, and then update the registry. A crash after provider evaluation but before line 344 likewise leaves the version reusable. This defeats the claimed exactly-once test-version rule under the stated realistic threat model.
- Freezing remains reachable only through the explicit CLI call (`src/id_detector/cli.py:1877-1901`). Draft and unverified rows are refused before publication (`src/id_detector/truth.py:2478-2525`).
- Read-only gateway checks found `controlled-events-1` (145 sets) and `controlled-synth-1` (25 sets) frozen, verified, complete, and independent. `release-1` remains manifest-less with all 222 rows draft; `dev-1` has a non-frozen manifest and all 120 rows draft.
- No protected second-session file, corpus, `work/`, profile, or golden output appears in the diff.

## E — Tests

`uv` could not execute in this sandbox. Direct pytest startup also failed because the read-only environment provides no usable temporary directory, so this review is static apart from read-only gateway verification.

Both fixtures exist, and the previously pinned modules remain explicitly open while selected tests exercise the closed state. No broad assertion was simply deleted. However:

- `tests/test_score_corpus.py:612-641` now explicitly expects the positive threshold sentence from a synthetic document containing no certification scope.
- The open-gate emitter control only proves every positive pattern appears; it never verifies provenance or complete-corpus scope (`tests/test_truth_corpus_followup_r10.py:248-257`).
- There is no mixed-exposure freeze test requiring every manifest `certifiable` flag false.
- There are no open-gate scope tests for links-score, profile freezing, or calibrated-model propagation.
- There is no nested manifest-less scratch test or concurrent/crash-retry certification test.
- Refusal messages are generally actionable, but the scratch bypass and concurrent certify path produce no refusal. `docs/STATUS.md:114` also still tells the owner the gate is switched off when production now sets it on.

## Required fixes

### Realistic

1. **P0:** Derive `thresholds_met` and its printed success sentence from the same complete, verified, timed, independent scope eligibility as `certifiable`; otherwise emit `null` and development-only wording.
2. **P0:** When freezing, compute corpus-wide independence first and make every manifest `certifiable` flag false if any frozen set is exposed.
3. **P0:** Remove or rename certification semantics from links-score/profile features unless their inputs are cryptographically bound to one verified complete independent corpus; likewise require verified certification provenance before episode propagation.
4. **P0:** Make scratch ancestry detect manifest-less corpus roots at every ancestor depth, including an existing ordinary subdirectory beneath such a root.
5. **P0:** Durably reserve and lock `(profile, test_version)` before any evaluation/provider work, hold the lock through publication, and refuse concurrent or post-dispatch retries under that version.
6. **P1:** Revalidate both certify artifacts and the registry with `work_root` immediately before each write; print “evaluated” rather than “certified corpus” when zero triples certify.
7. **P2:** Update `docs/STATUS.md` so the owner is not told the gate is closed when it is open.

### Adversarial

1. **P2:** Pin or identity-check the validated temp parent through scratch creation and cleanup; otherwise a hostile same-user process can swap a component for a junction between validation and `os.mkdir`/`rmtree`.

VERDICT: FIX_FIRST