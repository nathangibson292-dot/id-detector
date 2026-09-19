## A — the wrong row

The stated regression is not reproducible from the supplied report or cached inputs.

- The report says no newly listed row names an unplayed track and identifies the `99b8…` changes as “Makes Me,” an Original Don tier rise, and hiding Adagio; it does not claim Mears is new ([build report:116](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/docs/reviews/build-local-hint-corroboration.md:116), [build report:126](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/docs/reviews/build-local-hint-corroboration.md:126)).
- The pre-change accuracy report already lists “Mears — Be My Lover” among the wrong rows ([release-1 report:30](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/docs/accuracy/release-1-free-draft.md:30)).
- A read-only replay of the cached `99b8…` inputs under both `3ff64d2` and the modified source found Mears visible in both, with no `hint_supported` flag and no hint resolving to its work.

A hint can nevertheless promote a hidden episode generally: attachment adds one full trial ([episodes.py:761](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/episodes.py:761)), sets `hint_supported` ([episodes.py:829](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/episodes.py:829)), bypasses scatter/contradicted/buried suppression ([episodes.py:1026](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/episodes.py:1026)), and bypasses the short-row floor ([exports.py:204](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/present/exports.py:204)). That is a class of false-row risk, not a one-off, whenever identity matching is wrong.

The narrow safeguard is to retain the timing attachments and short-row exemption, but never let an indirect hint override `scatter` or `contradicted`, and only allow the exemption after an unambiguous field-level label match. That should preserve the nine genuine new rows while blocking unsafe promotion.

## B — timing window

A hint cannot normally attach to a differently named work: plays are indexed by `hint_work_id`, then `hint_backed_plays` receives only that work’s episodes ([episodes.py:726](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/episodes.py:726), [episodes.py:738](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/episodes.py:738)). Thus track boundaries themselves provide no protection; identity matching is the sole barrier.

There are two weaknesses:

- One hint can corroborate multiple episodes: the direct-intersection branch returns every matching play, despite the “ONE play” description applying only off-support ([episodes.py:100](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/episodes.py:100), [episodes.py:106](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/episodes.py:106)).
- Off-support selection prefers the longest proved play before proximity ([episodes.py:113](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/episodes.py:113)). Within −180/+90, it may therefore choose a farther occurrence over a nearer one. The reach also includes every gap in an episode hull ([episodes.py:81](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/episodes.py:81)).

A different similarly named track becomes reachable when loose identity matching merges it into the same work. Ordinary named hints add assertions to every audio node that passes, without an ambiguity veto ([identity.py:579](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/identity.py:579)). Bare-hyphen hints, by contrast, correctly require exactly one resulting work ([identity.py:673](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/identity.py:673)).

## C — label matching

The “both parts” implementation is not field-level. It pools every hint token, then lets any pooled token satisfy artist credit and any pooled token satisfy title ([identity.py:413](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/identity.py:413), [identity.py:430](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/identity.py:430)). Verified false positives include:

- `DJ Artist — Love` corroborates `DJ Love — Artist`: artist/title tokens cross-satisfy the guards.
- `Artist — Run It Back` corroborates `Artist — Run It`: containment is asymmetric, so a longer different hint title passes ([identity.py:230](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/identity.py:230)).
- `DJ — Love` corroborates `DJ Alice — Love`: the common token `dj` counts as the credited name.
- `Artist — Run`/`Runs` and `Artist — Angel`/`Anger` corroborate through the permissive near-spelling rule ([identity.py:210](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/identity.py:210)).
- `Artist — ID` is considered a real title and corroborates the same placeholder label; `_NO_TITLE_WORDS` omits `id` ([identity.py:278](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/identity.py:278), [identity.py:345](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/identity.py:345)). Parser filtering only catches exact `ID`, `ID-ID`, or artist `ID` plus an ID title, not `Artist - ID` ([parse.py:53](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/hints/parse.py:53), [parse.py:321](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/hints/parse.py:321)).

New false negatives include `Artist — Song` against `Artist — Song feat. Guest`, and against `Artist — Song Original Mix`: unbracketed featured names remain required title tokens, while only selected single trailing version words are removed ([identity.py:276](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/identity.py:276), [identity.py:405](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/identity.py:405)). The report also acknowledges abbreviation and two-word-spelling losses ([build report:155](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/docs/reviews/build-local-hint-corroboration.md:155)).

Accent, punctuation, dotted-initial and feature-marker tokenization is shared ([identity.py:303](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/identity.py:303)); joined-word folding is reciprocal ([identity.py:422](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/identity.py:422)). Cleanup is not literally identical: annotations/hedges are hint-only, while version-bracket and trailing-version handling are audio-only.

## D — policy and blast radius

Policy is preserved narrowly:

- Ordinary eligible `Artist - Title` answers can still create hint-only tracks under the pre-existing policy ([episodes.py:1061](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/episodes.py:1061)).
- Bare-hyphen answers receive no node and can only point at an already recognised work ([identity.py:596](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/identity.py:596)). They cannot create a track.

Only the two allowed fusion files plus new report/tests/fixture are changed; no protected file, corpus, work tree, profile, or golden is in the diff.

The logic is not Local-Free-only. Both Free and paid/Deep fusion call the same identity and episode functions; `profile` affects certification, not hint attachment ([episodes.py:677](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/episodes.py:677), [episodes.py:1405](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/episodes.py:1405)). Paid observations can therefore also be merged or promoted.

No calibration/scorer/profile source changed, but calibrated fusion receives the changed supporting-hint feature ([episodes.py:901](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/episodes.py:901)). Attachment also removes `hint_cluster` rescan requests ([episodes.py:1326](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/episodes.py:1326)), so a full rerun can differ beyond presentation.

The golden generator explicitly runs with `no_hints=True` ([make_golden.py:98](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/scripts/make_golden.py:98)); its files are unchanged.

## E — tests

The focused file passed locally: `31 passed`. Full pytest and the pipeline golden test could not run because this read-only sandbox has no writable temporary directory. Three non-writing golden integrity tests passed.

Coverage is insufficient for the identified risks:

- The fixture spaces tracks ten minutes apart, so it does not exercise a real 3–5-minute boundary ([fixture:2](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/tests/fixtures/deep/hint-corroboration.json:2)).
- Its wrong-place case is ten minutes away, not inside −180/+90 ([fixture:38](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/tests/fixtures/deep/hint-corroboration.json:38)).
- It tests a shorter hint against a longer title, but not the inverse substring, common credits, cross-field token swaps, placeholders, or two directly intersected episodes ([test_hint_corroboration.py:184](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/tests/test_hint_corroboration.py:184)).
- Not every test would fail on a revert: many positive/negative cases and the version-policy test cover old behavior. “The old file cannot collect” is not a substitute for fix-specific regression proof ([build report:145](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/docs/reviews/build-local-hint-corroboration.md:145)).

## Required fixes

- **P1:** Replace pooled-token “both parts” matching with explicit two-field orientation checks. Require title equivalence after allowed cleanup, a distinctive credited-name match, reject ID/TBC/unknown placeholders, and refuse an ordinary hint that matches more than one pre-hint audio work.
- **P1:** Make one hint back at most one episode, including direct intersections; rank direct plays by overlap and indirect plays deterministically. Do not let an indirect hint override `scatter` or `contradicted`.
- **P1:** Add regression tests for inverse title substrings, common artist words, cross-field swaps, one-edit distinct titles, `Artist - ID`, adjacent previous/next tracks, and one hint intersecting two episodes. Each must fail under the unsafe rule it protects against.
- **P2:** Reconcile the Mears claim with the report and cached replay; record its exact old/new episode ID, flags, hidden reason, supporting hint, and presentation-floor decision before changing timing constants.

VERDICT: FIX_FIRST