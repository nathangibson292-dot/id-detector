## A — The wrong row

**DONE (unchanged).** Round 2 established that “Mears – Be My Lover” was already listed and only appeared new because page grouping changed ([Round-2 review:3](C:/Users/natha/AppData/Local/Temp/claude/c--Users-natha-Documents-Music-id-detector/1b09d033-8746-455c-95be-d5b8ec959ae3/scratchpad/diff-review-local-hints-sol-r2.out.md:3)). Fix pass 2 did not alter episode timing or presentation ([build report:447](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/docs/reviews/build-local-hint-corroboration.md:447)).

## B — Timing window

**DONE (unchanged).** No timing code changed in this pass; `fuse/episodes.py` remains as reviewed in Round 2 ([build report:449](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/docs/reviews/build-local-hint-corroboration.md:449)). The one-hint/one-episode and indirect-suppression conclusions are not reopened.

## C — Label matching

### 1. Exact-label ambiguity

**DONE.** Recognised components are constructed before the hint loop, including provider relationships and equivalent audio labels ([identity.py:650](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/identity.py:650)). `_named()` tests a hint against every recognised audio text node ([identity.py:681](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/identity.py:681)).

Every ordinary hint now calls `_named()`, including when its text node exactly equals an audio node. More than one recognised root exits before creating the hint mapping, node, or assertion ([identity.py:709](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/identity.py:709)).

Consequently the hint cannot support anything—support requires `hint_work_ids` ([episodes.py:763](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/episodes.py:763))—or contradict anything, because contradiction ignores answers whose work is `None` ([episodes.py:1040](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/episodes.py:1040), [episodes.py:1070](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/episodes.py:1070)).

The adjacent-play regression uses the exact `Artist - Title` orientation and explicitly checks the parsed fields, absent identity/candidate, distinct works, no support, no contradiction, no evidence, and no crowd row ([test_hint_corroboration.py:507](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/tests/test_hint_corroboration.py:507)).

### 2. Featured credits

**DONE.** Each featured name is represented by its complete distinctive word set, so `DJ Boring`/`DJ Seinfeld` and `Bob Jones`/`Bob Smith` no longer intersect merely through “DJ” or “Bob” ([identity.py:393](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/identity.py:393), [identity.py:419](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/identity.py:419), [identity.py:493](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/identity.py:493)).

Audio labels with multiple different non-empty featured-credit sets are grouped only with identical credit sets. An uncredited label is folded into the group only when zero or one distinct credited version exists, preventing it from bridging two credited works ([identity.py:668](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/identity.py:668)).

Both requested name pairs are covered at matcher and fusion levels ([test_hint_corroboration.py:531](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/tests/test_hint_corroboration.py:531), [test_hint_corroboration.py:549](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/tests/test_hint_corroboration.py:549)). The single-credited-version compatibility guard is present at [test_hint_corroboration.py:573](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/tests/test_hint_corroboration.py:573).

## D — Policy and blast radius

**DONE.** Ambiguous ordinary hints now create neither identity nor assertion. Bare-hyphen answers remain join-only and receive no independent node ([identity.py:694](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/identity.py:694)).

Current status contains only the previously reviewed fusion modules plus the report, fixture, and test file. No protected file, golden, profile, `data/corpus/`, or `work/` path is changed.

Deep uses the same fusion and therefore receives the intended correction. No calibration, scoring, presentation, or profile code changed.

The Local Free golden disables hints ([make_golden.py:98](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/scripts/make_golden.py:98)). The new recognised-work union is only consulted during hint ambiguity checking; final work construction still unions assertions separately ([identity.py:779](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/identity.py:779)). Thus the no-hint golden path is statically unchanged; the builder also reports the golden passing ([build report:538](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/docs/reviews/build-local-hint-corroboration.md:538)).

## E — Tests

**DONE.** I ran `tests/test_hint_corroboration.py` locally with capture, cache, bytecode, provider tokens, and recognition disabled: **63 passed**. The six Round 3 cases contributed six passes.

The ordinary capture-enabled invocation could not start because the read-only sandbox has no writable temporary directory; `-s -p no:cacheprovider` avoided that requirement. No network or provider call occurred.

The exact-label test would fail with the Round-2 shortcut restored. The four parameterised wrong-feature cases would fail with flat token intersection/feature-erasing grouping restored. The sixth case is intentionally an over-tightening guard and correctly passes both before and after ([build report:475](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/docs/reviews/build-local-hint-corroboration.md:475)).

## Required fixes

None. Zero P0 and P1 findings under the Round 3 stop rule.

## Follow-ups, not blockers

None found.

VERDICT: OK_TO_COMMIT