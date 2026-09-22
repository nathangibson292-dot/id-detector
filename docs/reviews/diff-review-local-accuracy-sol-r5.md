## A — Locking

Confirmed closed.

- Alias locks canonicalize to `work/.locks/media-<media-key>.lock` at [jobs.py:47](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/jobs.py:47).
- `_refuse_stored` compares the stored and already-held canonical lock paths and does not reacquire when they match ([pipeline.py:213](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/pipeline.py:213)). A genuinely distinct stored lock is acquired and released in `finally` ([pipeline.py:221](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/pipeline.py:221), [pipeline.py:260](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/pipeline.py:260)); the caller’s canonical media lock is released by the outer pipeline `finally` ([pipeline.py:1896](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/pipeline.py:1896)). No deadlock or lock leak found.
- The direct regression uses different filenames with identical bytes, runs the real pipeline, and asserts publication plus zero AudD/Shazam calls ([test_refusion.py:231](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/tests/test_refusion.py:231)).
- The worker regression runs through `LocalWorker`, requires a succeeded job and new re-fused publication, and pins zero provider calls and unchanged reservation/dispatch counts ([test_refusion_money.py:62](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/tests/idea_web/test_refusion_money.py:62)).
- The recorded reversion made both regressions fail with the false busy refusal; restoration passed the focused run ([build-local-accuracy-fixes.md:1270](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/docs/reviews/build-local-accuracy-fixes.md:1270)).
- A real second process remains excluded across sibling aliases ([test_refusion.py:1047](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/tests/test_refusion.py:1047)).

## B — Skip reporting

Confirmed closed. Titles come from validated `ingest/source.json` rather than hashes ([server.py:122](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/idea_web/server.py:122)). Browser and CLI share `owner_status_lines()` ([server.py:51](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/idea_web/server.py:51)); the browser polls after startup ([pages.py:195](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/idea_web/pages.py:195)) and the CLI prints the completed report ([cli.py:919](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/cli.py:919)).

Exact reported owner-visible wording:

```text
Saved-result upkeep complete: 11 mixes improved; 3 deliberately left as they are.
Speed Garage & Bass Mix - Holly Olivia (March 26): The result was left as it is because it is a paid (Deep) result, and the windows its second opinion checked were chosen by the older fusion rules, so it cannot simply be rebuilt.
Garage Mix - Dec 25: The result was left as it is because it is a paid (Deep) result, and the windows its second opinion checked were chosen by the older fusion rules, so it cannot simply be rebuilt.
BENWAL | FULL CLOSING SET | DGTL AMSTERDAM 2026 | 5.4.26: The result was left as it is because its stored records disagree with each other: the final generation reference disagrees with its generation sidecar.
To analyse a skipped mix again, re-run it with --refresh. Warning: re-running a paid (Deep) mix will spend real AudD credit.
```

This is accurate and actionable: “deliberately left” is informational, the reasons are ordinary-language descriptions of the actual Deep and BENWAL safeguards, and the final sentence gives the remedy while explicitly denying any implication that Deep refresh is free. The regression pins the protected Deep and inconsistent-provenance cases across browser, status endpoint, polling asset, and CLI ([test_refusion.py:545](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/tests/test_refusion.py:545), [test_refusion.py:600](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/tests/test_refusion.py:600)).

## C — Suite and scope

- The report contains one foreground, unsharded `uv run pytest` invocation on the final tree: 2,129 collected, 2,032 selected, **2,030 passed, 2 skipped, 97 deselected** ([build-local-accuracy-fixes.md:1319](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/docs/reviews/build-local-accuracy-fixes.md:1319), [build-local-accuracy-fixes.md:1330](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/docs/reviews/build-local-accuracy-fixes.md:1330)).
- The 97 deselections match the unchanged default `not slow and not live` policy ([pyproject.toml:48](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/pyproject.toml:48)) and the established slow-module marking ([conftest.py:15](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/tests/conftest.py:15)).
- Current Git checks against `21589a0` show no changes under `data/corpus/`, `work/`, `profiles/`, `tests/golden/local-free/`, or any named second-session file. Current status matches the report, and `git diff --check` is clean ([build-local-accuracy-fixes.md:1420](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/docs/reviews/build-local-accuracy-fixes.md:1420)).
- Existing assertion changes are version-string updates from fusion 2 to fusion 3. The two non-version legacy-page replacements strengthen fail-closed preservation/no-publication checks; they do not weaken a pin.

## Required fixes

### Realistic

- P0/P1/P2: None.

### Adversarial

- P0/P1/P2: None.

## Follow-ups, not blockers

None introduced by fix pass 4.

VERDICT: OK_TO_COMMIT