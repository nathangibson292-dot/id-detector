## A — The wrong row

**DONE.** “Mears – Be My Lover” was not newly surfaced. Both episodes were already listed, unchanged, and unsupported by hints. The apparent addition came from page grouping splitting when “Original Don” became confident between them ([build report:256](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/docs/reviews/build-local-hint-corroboration.md:256), [build report:269](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/docs/reviews/build-local-hint-corroboration.md:269)). No timing-constant fix is warranted for Mears.

## B — Timing window

**DONE.** A hint now backs at most one episode. Direct candidates rank by overlap, proved airtime, start, then key; indirect candidates rank hull containment, airtime, distance, start, then key ([episodes.py:95](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/episodes.py:95), [episodes.py:115](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/episodes.py:115)). Assignment consumes that single result ([episodes.py:751](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/episodes.py:751)).

**DONE.** Indirect-only support retains its vote and short-row exemption but cannot override its own `scatter` or `contradicted` fault, including through a `likely` badge earned from that vote ([episodes.py:779](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/episodes.py:779), [episodes.py:1077](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/episodes.py:1077)). Presentation checks suppression before the short-row exemption ([exports.py:224](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/present/exports.py:224)).

The wide window still relies on identity matching to prevent cross-track attachment. The remaining identity defects below therefore make the timing reach operationally unsafe.

## C — Label matching

**PARTIAL.** Both orientations, field-level title equality, distinctive credited names, placeholders, and insertion/deletion-only title slips are implemented ([identity.py:432](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/identity.py:432), [identity.py:463](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/identity.py:463), [identity.py:490](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/identity.py:490)). Shared tokenization handles accents, punctuation and dotted initials on both sides ([identity.py:320](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/identity.py:320)).

The ambiguity P1 is not fully closed:

- Exact-text hints skip `_named()` entirely through `named = [] if text_node in audio_text_nodes` ([identity.py:678](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/identity.py:678)). The ambiguity test explicitly reverses its hint to avoid this path ([test_hint_corroboration.py:407](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/tests/test_hint_corroboration.py:407)).
- A read-only probe confirmed an exact `Fixture Artist - Night Drive` hint matches both the solo and collaborative recognised works yet is assigned to the solo work. With adjacent plays, it gave the solo episode `hint_supported` and made the collaborative episode `contradicted`.
- Featured-credit conflict uses any token intersection, not a whole distinctive name ([identity.py:427](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/identity.py:427), [identity.py:481](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/identity.py:481)). Consequently `DJ Boring` matches `DJ Seinfeld` through “DJ”, and `Bob Jones` matches `Bob Smith` through “Bob”.
- `audio_work_key` erases featured credits when constructing ambiguity groups ([identity.py:531](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/identity.py:531), [identity.py:644](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/identity.py:644)). A probe confirmed an uncredited hint merged two previously distinct recognised works, `Song (feat. Bob Jones)` and `Song (feat. Alice Smith)`. This contradicts the existing invariant that different featured artists remain different works ([test_phase1b_fusion.py:621](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/tests/test_phase1b_fusion.py:621)).

The spelling-slip guard is therefore also only **PARTIAL**: non-exact ambiguous matches are vetoed, but an exact label that is slip-equivalent to another recognised title bypasses the scan.

**DONE.** The loose hint-to-hint ride-through is closed by excluding every audio-matched node from the loose merge ([identity.py:703](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/identity.py:703)). Legitimate well-spelled hints still attach independently through the field matcher; the targeted regression demonstrates this ([test_hint_corroboration.py:427](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/tests/test_hint_corroboration.py:427)).

## D — Policy and blast radius

**DONE.** Bare-hyphen answers receive no node and can only resolve to one existing recognised work ([identity.py:663](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/identity.py:663)). Ordinary named hints retain the pre-existing hint-only policy; placeholders receive no identity ([identity.py:660](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/identity.py:660), [episodes.py:1124](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/episodes.py:1124)).

Free and Deep share these functions, so paid fusion also changes ([episodes.py:1468](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/episodes.py:1468)). Calibration consumes the changed supporting-hint set, and attached hints alter `hint_cluster` rescans; no calibration or scoring source was edited ([episodes.py:939](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/episodes.py:939), [episodes.py:1389](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/src/id_detector/fuse/episodes.py:1389)).

The re-measure is plausible: stricter matching removes two attachments, `112 → 110`, without losing a supported episode (`71 → 71`) or any of the nine new page rows; release-1 recall remains `144/218` ([build report:358](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/docs/reviews/build-local-hint-corroboration.md:358), [build report:379](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/docs/reviews/build-local-hint-corroboration.md:379)).

Only the two fusion modules and three new review/test artifacts are dirty. No protected file, `data/corpus/`, `work/`, `profiles/`, or golden file was touched. The Local Free golden runs with `no_hints=True` ([make_golden.py:90](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/scripts/make_golden.py:90)); statically, the no-hint path remains equivalent.

## E — Tests

**PARTIAL.** The 26 new cases cover the reported substring, common-word, cross-field, edit, placeholder, timing, multi-episode and suppression risks. The builder reports 25 unsafe-rule failures; the remaining version-label test is intentionally an over-tightening guard ([build report:335](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a0c0bf29a9b5f3364/docs/reviews/build-local-hint-corroboration.md:335)).

They do not cover the exact-node ambiguity bypass or featured names sharing a common token, and the ambiguity test deliberately avoids the exact-node path. Thus the round-1 ambiguity requirement is not regression-protected.

I could not execute pytest: `uv.exe` is not launchable in this sandbox, and direct pytest aborts because no writable temporary directory exists. Static review and read-only Python probes were used; no provider or network call occurred.

## Required fixes

- **P1:** Always evaluate every ordinary hint—including an exact text-node label—against all pre-hint recognised works before assigning it. If more than one distinct work matches, create no hint identity or assertions. Add an adjacent-play regression using the exact field orientation and assert that neither work is supported or contradicted by the ambiguous hint.
- **P1:** Compare featured credits as whole distinctive credited names, not flat token intersection. Preserve different non-empty featured artists as distinct pre-hint works; an uncredited hint fitting both must be vetoed, never merge them. Add `DJ Boring`/`DJ Seinfeld` and `Bob Jones`/`Bob Smith` fusion regressions.

VERDICT: FIX_FIRST