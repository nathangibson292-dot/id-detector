## A — Audit of the fixer’s verdicts

All seven original retro findings are recorded as **Reproduced**; none was dismissed as “not reproducible” or “already fixed.” The three Round-3 adversarial items are honestly listed as unfixed accepted residual risks at `docs/reviews/followup-truth-corpus.md:838-864`. I do not re-raise them.

| Round-3 finding | Review |
|---|---|
| R-P0-1 exposure ledger | **Incomplete.** Append/OR behavior is implemented at `src/id_detector/truth.py:415-527`, reveal writes both records at `src/id_detector/truth_review.py:475-526`, passes carry exposure at `truth.py:942-1056,1324-1389`, freeze records line digests at `truth.py:1585,1714-1718`, and verification checks them at `benchmark/scorer.py:1353-1376`. `tests/test_truth_corpus_followup_r3.py:105-134` would fail on a wholesale revert at line 113. The supported single-set layout still loses the ledger when copied; see B1. |
| R-P0-2 safe reruns | **Incomplete.** Sequential refusals exist at `truth.py:190-204,1425-1428,1731-1781`, and direct/CLI regressions at `tests/test_truth_corpus_followup_r3.py:142-279` would fail if reverted. The record’s claim that all state is rechecked inside the lock is false; see B2. |
| R-P1-3 manifest placement | **Core behavior fixed.** `_manifest_destination` constrains output to the supplied corpus root at `truth.py:1478-1508`; non-root cases in `tests/test_truth_corpus_followup_r3.py:313-351` and `tests/test_truth_corpus_followup_r2.py:310-329` would fail if reverted. The root case is only a control and passes both versions. Both committed frozen manifests remain at their roots; all 170 entry hashes independently matched. Owner-facing CLI help contradicts the new rule; see C1. |
| R-P1-4 links before resolution | **Incomplete.** The shared guidance exists at `truth_paths.py:164-180`, and the tests at `tests/test_truth_corpus_followup_r3.py:359-408` catch the original lack of refusal. They assert only eventual refusal, however; several paths resolve/read first or emit different guidance. See B3. |

Previously verified protections remain present: exact manifest binding to path, loaded bytes, and digest (`benchmark/scorer.py:1211-1242,1299-1412`); deletion-denying reveal hold (`truth_review.py:475-511`, `truth_paths.py:287-317`); expected-key pinning (`truth.py:1406-1429,1528-1540`); one lock deadline and `finally` release (`truth.py:706-753`); sorted freeze locks; frozen-corpus refusal; forward-only in-lock checks; first-pass exposure OR-ing; scorer independence gating; and full-prefix `Cf` handling.

The ledger writer is monotonic for cooperating processes: `truth.py:454-485` takes the ledger’s cross-process lock, rereads inside it, and replaces the file only with the old bytes plus a line. Same-set CLI operations also serialize through the truth lock. No production writer removes or downgrades a ledger line.

## B — Correctness bugs in the fix

1. **P0 — The exposure ledger is outside the corpus for a supported single-set layout.** `exposure_ledger_path` and its writer always use `truth_path.parent.parent` at `src/id_detector/truth.py:402-403,463-485`, while freeze explicitly accepts a corpus whose `ground_truth.json` is directly in the supplied directory at `truth.py:1460-1464`. Its manifest is written inside that directory, but its ledger is one level above it; verification also accepts this outside-corpus evidence at `benchmark/scorer.py:1359-1372`. Realistic failure: reveal predictions for such a set, delete the sidecar, copy that corpus folder to another location, restart, save, and freeze. The external ledger is not copied, so the copy becomes falsely independent and certifiable.

2. **P0 — Rerun predicates are not protected from cooperating two-tab races.** Seed checks all managed siblings before taking a lock at `truth.py:190-204`, but its in-lock `create_only` check covers only the destination at `truth.py:1420-1428`. An already-open review tab can create exposure after preflight and before seed commits; seed then writes a draft beside it instead of refusing. More seriously, draft-manifest validates destination and truth state at `truth.py:1738-1767`, then writes later at `:1781`; freeze publishes the same manifest without acquiring its lock at `truth.py:1724`. If the draft command is paused while another tab verifies and freezes, it can resume and replace the new `frozen: true` manifest with its stale `frozen: false` inventory.

3. **P1 — Link refusal still occurs after resolution/read, and not every message gives the required remedy.** `find_truth_path` resolves before refusing at `src/id_detector/truth_review.py:96-98`; `TruthReviewSession` resolves at `:431` before checking the original path at `:435`; scorer loading resolves and parses at `benchmark/scorer.py:1223-1237` before manifest discovery refuses; and `corpus_independent` resolves at `:1446`. Certification reads its manifest and walks truth at `calibrate/certify.py:135-166` without an initial link-component refusal. Manual messages at `truth_review.py:441-444`, `truth.py:1061-1064`, and `truth_paths.py:437-440` omit “pass the resolved real path.” The Round-3 tests would pass despite these ordering/message defects.

## C — Regressions

1. **P1 — `idea truth freeze --help` still advertises two now-refused placements.** `src/id_detector/cli.py:1832-1835` says the manifest may be in the set directory, corpus directory, or directory above, while `truth.py:1494-1500` permits only the supplied corpus directory. This is a direct owner-facing regression from the intended single-location behavior.

The 4a-ii route protections remain intact: POST-only mutations, Host/Origin middleware and CSRF at `src/idea_web/truth_review.py:42-99`, exact audio route at `:50-68`, and loopback server construction at `:110-123`. `--set` remains regex- and content-selected at `src/id_detector/truth_review.py:89-118`. `idea.cmd` is unchanged.

## D — Test quality

I could not run `uv` or pytest in this read-only sandbox, so the fixer’s reported suite counts remain unverified.

I did run the read-only audit with bytecode disabled:

```text
audited 460 files
fixture audit passed
```

An in-memory probe generated the exact ledger-line shape for all seven `release-1` set IDs: zero URL, handle, long-ID, identifier-field, or raw-fragment findings. `tests/test_truth_review.py:414-432` also implicitly audits a generated ledger after reveal. The ledger is not git-ignored.

Important missing regressions:

- The Round-3 lifecycle test calls independence helpers but never produces an actual scorer/L3 report from the ledger-only state.
- No test deletes, truncates, or rewrites the ledger after freeze and verifies rejection.
- No cross-process/two-set append test proves both concurrent ledger entries survive.
- No single-set corpus reveal → delete → copy → restart → freeze test.
- No barrier test places exposure after seed preflight, or freezes between draft-manifest validation and publication.
- Link tests prove eventual refusal, not refusal before `real_path`/`resolve`/file reads, and do not cover all nonstandard messages.

The three requested expectation changes are justified and not weaker: the overlay test deliberately removes its fixture truth before reseeding; non-root manifest placements are now expected to refuse; and `test_stage2a_truth.py` writes the manifest at the corpus root with `set-one/ground_truth.json`. Other modified failure seams preserve or strengthen their assertions.

## E — Scope

`git status --short -- data work` is empty. No dependency file changed. `src/id_detector/io.py`, `jobs.py`, `retention.py`, `tests/test_phase2b_retention.py`, all prohibited second-session files, `tests/test_phase0a_security.py`, `README.md`, and `idea.cmd` are unchanged. All six untracked files were inspected, and `git diff --check` is clean.

The 36 `release-1` files/directories and their worktree ancestors contain no reparse point. No server, provider, real URL, or GC command was invoked.

## Required fixes

### Realistic

1. **P0** — Either reject the single-set-root layout everywhere or pass an explicit corpus root through review/exposure code so the ledger is always beside the manifest; add the reveal → sidecar deletion → corpus-folder copy → restart → save/freeze/score regression.
2. **P0** — Recheck every managed seed sibling inside the held truth lock, and serialize draft-manifest and freeze publication on the same manifest lock with all truth/destination predicates repeated in-lock; add deterministic two-process barrier tests.
3. **P1** — Refuse link components before every `real_path`, `resolve`, enumeration, or read in review, scoring, draft-manifest, and certification paths; route every link refusal through `link_refusal` and test the exact guidance.
4. **P1** — Update `idea truth freeze --help` and stale manifest-location comments to describe only `<supplied-corpus>/corpus-version.json`, with a CLI-help regression.
5. **P1** — Add end-to-end ledger-only scoring/L3 and certification tests, post-freeze deletion/truncation/rewrite tests, and a concurrent two-set append test.

### Adversarial

None. The three owner-accepted residual risks are recorded honestly and require no action in this threat model.

VERDICT: FIX_FIRST