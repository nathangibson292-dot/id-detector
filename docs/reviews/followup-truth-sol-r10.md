## A — Audit of the fixer’s verdicts

- **Round-9 #1 remains partially open (P0).** The scorer, profile, calibration, episode, freeze, and certification status fields are gated. However, the link scorer still emits a positive gate result while certification is disabled; see B1. This qualifies under the stop rule as an unclosed round-9 requirement.
- **Round-9 #2 is otherwise closed.** The named public writers have initial and final explicit guards. The `atomic_write_bytes` backstop covers the three protected names and nearby corpus files without directory listing. Normal work, bundle, cache, playlist, settings, invocation, and attempt paths do not ordinarily have those names in their parent or grandparent. Six existence checks plus path canonicalisation are reasonable beside an atomic, fsynced write.
- The production exemption call graph is currently limited to `truth.write_corpus_file_through_gateway`, called only for the controlled-render staging corpus and calibration-validation scratch corpus. Owner truth continues through the pinned writer.
- **Round-9 #3 is closed.** An existing empty/unmarked root is treated as new, so the documented retry inside `release-1` is refused while a deliberate empty corpus outside another corpus remains usable.
- **Round-9 #4 is closed.** Only the under-lock call enables the bounded parent listing.
- **Round-9 #5 is closed.** Each of the four run functions publishes through `_publish_report`; its direct test uses a manifest-less corpus destination that the low-level backstop would not catch, so removing the final explicit guard would fail.
- No round-10 verdict was marked “not reproducible” or “already fixed.”

## B — Correctness bugs in the fix

1. **Realistic P0 — link-score still makes positive JSON and printed gate claims while certification is disabled.**

   [enrich/benchmark.py:139](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/src/id_detector/enrich/benchmark.py:139) leaves `"pass": true` for a passing sample even though its status is disabled. More importantly, [cli.py:1673](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/src/id_detector/cli.py:1673) prints `gate_pass=true` without printing the disabled status.

   The round-10 test’s 100/100 sample reaches this exact result. This contradicts the treatment of `thresholds_met`: the raw metrics may remain visible, but the positive gate conclusion must be unjudged while certification is off.

## C — Regressions

- No realistic false refusal was found for normal pipeline, queue, money, bundle, cache, `app.db`, attempt-journal, invocation-journal, playlist, or settings layouts.
- Truth-review routing protections have no round-10 diff, and the new I/O backstop does not intercept its pinned corpus writer. No loopback, CSRF, Origin/Host, GET immutability, traversal, exposure-order, or locking regression was found.
- `idea.cmd` and the 4a-ii serving files are unaffected.
- No other realistic regression introduced by this worktree was found.

## D — Test quality

- [test_truth_corpus_followup_r10.py:47](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/tests/test_truth_corpus_followup_r10.py:47) scans neither `"pass": true` nor `gate_pass=true`, so its own passing link-score fixture escapes the claimed positive-claim scan.
- Apart from that omission, reverting each listed certification gate produces a scanned positive pattern, and the gate-open control demonstrates those patterns are reachable.
- The seed-retry, one-parent-listing, I/O backstop, and four final-publication tests are meaningful.
- I could not run `uv` or pytest in this read-only environment. The system Python also lacks project dependencies, so I could not execute the project verifier.

## E — Scope

- `git status --short -- data/corpus work` is empty; `work/` does not exist in this worktree.
- `jobs.py`, `retention.py`, `tests/test_phase2b_retention.py`, all named second-session files, `idea.cmd`, and `tests/test_phase0a_security.py` are untouched.
- No dependency or lockfile changed; `git diff --check` is clean.
- Every untracked file was inspected; all 12 untracked Python files parse successfully.
- Read-only recomputation found exact manifest inventories and matching truth/annotation/evidence SHA-256 values for all 145 `controlled-events-1` sets and all 25 `controlled-synth-1` sets. Their corpus versions and verified, non-draft episode fields are consistent.
- A dependency-free reproduction of the furniture heuristic found zero defects across 1,096 real corpus labels; legitimate asterisk/Unicode-hyphen names passed and the stale furniture variants were caught. This was not the full project audit command.

## Pre-existing, for a later follow-up

`io.durable_replace` and `io.create_file_durably` were introduced by main’s `bb57b70` and do not exist in this worktree. Leaving them unguarded after merge would preserve a pre-existing main-side gap, not create a regression attributable to this diff. They should nevertheless call the same backstop during merge integration.

## Required fixes

### Realistic

1. **P0:** When certification is disabled, make link-score’s `gate.pass` non-positive/null and stop printing `gate_pass=true`; print the disabled status instead. Extend the closed/open tests to assert the link JSON and console output individually.

### Adversarial

None.

VERDICT: FIX_FIRST