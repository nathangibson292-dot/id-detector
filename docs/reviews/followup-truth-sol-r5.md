## A — Audit of the fixer’s verdicts

The follow-up record marks all seven original retro findings as reproduced; none was dismissed as “not reproducible.” I independently confirmed the substantive fixes for exposure propagation, exact manifest binding, bulk offsets, crash recovery, field preservation, frozen/mixed-annotator refusal, namespace normalization, and the Unicode furniture heuristic.

The intended lock implementation itself is sound:

- Standard seed, verify, second-pass, resolve, review save/reveal, draft-manifest, and freeze wrappers acquire the corpus lock.
- Per-record locks are acquired after the corpus lock.
- One deadline covers thread and OS acquisition, with release in `finally`.
- Read-only review GET routes do not acquire it.

However, the claim that every production corpus mutation now shares that lock is false because of the paths in section B.

The earlier “already refused” junction-into-`work/` variant is supported by the pinned-directory location check after handle resolution. The accepted hostile same-user residual risks remain outside scope.

Read-only checks performed:

- `python -B scripts/audit_fixtures.py` passed: 460 files audited.
- `data/corpus/release-1/` has the supported layout: seven direct set directories, seven truth files, and no root-level truth.
- Static verification of both committed frozen manifests (`controlled-synth-1`, `controlled-events-1`) found 170/170 matching truth hashes and no state/version/pass defects.
- I could not execute the project verifier because the available Python environment lacks `pydantic`.
- As instructed, I did not run `uv` or pytest in the read-only sandbox.

## B — Correctness bugs in the fix

1. **P0 — A frozen corpus still accepts a newly seeded set.**  
   [`src/id_detector/truth.py:170`](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/src/id_detector/truth.py:170>) checks layout before locking, while the locked implementation at [`truth.py:206`](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/src/id_detector/truth.py:206>) examines only files beside the new destination. It never checks `<corpus>/corpus-version.json`. After freezing a corpus, `seed_truth(out_path=corpus/new-set/ground_truth.json, ...)` can therefore add an uncovered draft and invalidate the terminal frozen state and manifest coverage. The pre-lock directory inspection at [`truth.py:840`](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/src/id_detector/truth.py:840>) also permits concurrent outer/nested seeds to infer different corpus roots and create an unsupported nested layout.

2. **P0 — Draft-manifest can write anywhere while holding the wrong corpus lock.**  
   [`src/id_detector/truth.py:1876`](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/src/id_detector/truth.py:1876>) locks only `truth_dir`, but never requires `out_path == truth_dir / "corpus-version.json"`. The write at [`truth.py:1944`](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/src/id_detector/truth.py:1944>) also disables the record lock and supplies no `work_root`. A draft of corpus A can target corpus B’s manifest: it can validate B’s absent/draft file, race B’s freeze under a different lock, then replace B’s frozen manifest with `frozen:false`. It can also write into `work/`. The CLI continues to advertise an arbitrary output at [`src/id_detector/cli.py:1863`](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/src/id_detector/cli.py:1863>).

3. **P0 — The controlled-corpus renderer is an unlocked whole-corpus writer.**  
   [`src/id_detector/benchmark/controlled.py:655`](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/src/id_detector/benchmark/controlled.py:655>) accepts an arbitrary output directory, writes `ground_truth.json` records, then replaces the complete destination at [`controlled.py:636`](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/src/id_detector/benchmark/controlled.py:636>) and [`controlled.py:756`](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/src/id_detector/benchmark/controlled.py:756>) without the corpus lock or frozen-corpus protection. The public CLI exposes this unrestricted `--out` at [`src/id_detector/cli.py:1248`](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/src/id_detector/cli.py:1248>). An accidental render targeting `release-1`, including while review/freeze is active, can replace the irreplaceable corpus despite every truth command cooperating with the new lock.

4. **P1 — Descendant links are still traversed before the common refusal.**  
   [`src/id_detector/truth.py:822`](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/src/id_detector/truth.py:822>) performs `rglob` and `path_key` before refusing each discovered component. Scoring reads candidates directly at [`src/id_detector/benchmark/scorer.py:1225`](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/src/id_detector/benchmark/scorer.py:1225>), and its independence scan repeats the problem at [`scorer.py:1447`](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/src/id_detector/benchmark/scorer.py:1447>). Certification reads a possibly linked manifest and enumerated evidence at [`src/id_detector/calibrate/certify.py:136`](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/src/id_detector/calibrate/certify.py:136>) and [`certify.py:151`](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/src/id_detector/calibrate/certify.py:151>). Draft-manifest reads candidates at [`truth.py:1914`](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/src/id_detector/truth.py:1914>). A direct child junction or linked `corpus-version.json` is consequently enumerated/read before the mandated `link_refusal` guidance.

5. **P1 — Generic scoring still accepts unsupported nested layouts.**  
   [`src/id_detector/benchmark/scorer.py:1232`](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/src/id_detector/benchmark/scorer.py:1232>) rejects a truth directly in the supplied directory but recursively accepts `corpus/group/set/ground_truth.json`. It does not use `require_corpus_directory`, so the “one supported layout” invariant is not enforced by scoring.

## C — Regressions

No regression found in the 4a-ii web protections: the server remains loopback-bound; POST mutations pass through Host/Origin middleware and CSRF validation; GET routes do not mutate corpus state; audio routing is exact; and reveal persists exposure before returning predictions.

Exact manifest/evidence binding, forward-only checks, exposure OR-ing and ledger monotonicity remain present. Bulk offset applies the same shift/clamping to role segments, preview is client-only, and undo restores only offset-owned time fields.

`idea.cmd`, the second-session files, and the previously verified retention/I/O files are untouched.

## D — Test quality

The required concurrent-ledger regression is not concurrent. At [`tests/test_truth_corpus_followup_r4.py:211`](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/tests/test_truth_corpus_followup_r4.py:211>), the list comprehension calls `_release` sequentially. `_release` writes “go” and immediately waits in `communicate()` at [`test_truth_corpus_followup_r4.py:96`](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/tests/test_truth_corpus_followup_r4.py:96>), so process two is not released until process one has completed. The test passes without serialization and does not satisfy the mandatory revert-failure criterion: **P1**.

The seed/reveal and draft/freeze tests do use explicit process barriers and lock timeouts rather than sleeps. The ledger-only L3, certification-helper refusal, and post-freeze tamper assertions are causally meaningful.

There are no regressions covering frozen-corpus new-set seeding, arbitrary/cross-corpus draft output, controlled-render replacement, descendant-link traversal, or nested scoring.

## E — Scope

- No changes under `data/corpus/` or `work/`.
- No dependency changes.
- No changes to playlists, `tests/test_playlists.py`, `README.md`, `idea.cmd`, theme code, `tests/test_phase0a_security.py`, `io.py`, `jobs.py`, `retention.py`, or `tests/test_phase2b_retention.py`.
- `git diff --check` is clean.
- No existing test weakening was identified.

## Required fixes

### Realistic

1. **P0:** Make seed accept or establish an explicit canonical corpus root, acquire that root’s lock before every layout/frozen-state predicate, refuse any existing frozen manifest, and add barrier tests for frozen-new-set and concurrent outer/nested seeds.
2. **P0:** Require draft-manifest output to be exactly `<corpus>/corpus-version.json`, validate it against `work/`, and keep validation plus write under that same corpus lock; add a cross-corpus draft-versus-freeze barrier regression.
3. **P0:** Put controlled-corpus publication under the same canonical target-corpus lock and refuse replacing a frozen corpus, or restrict it to a dedicated non-corpus destination that cannot overlap `data/corpus`; test it against concurrent review/freeze.
4. **P1:** Refuse every descendant link before enumeration or reading, including set-directory junctions and the manifest itself, through `link_refusal` with the exact required guidance.
5. **P1:** Route directory scoring through the supported-layout validator and reject recursively nested truth files.
6. **P1:** Release both ledger child processes before waiting for either, using an explicit shared barrier/event positioned so both have observed the pre-append state; prove the test fails with serialization removed.

### Adversarial

None. The documented hostile same-user lock/path replacement risks remain accepted by the stated threat model.

VERDICT: FIX_FIRST