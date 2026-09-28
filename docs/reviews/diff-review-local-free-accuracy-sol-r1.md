## A — The scorer and the nine

All nine newly credited tracks are genuine same-work matches. The six with timed truth overlap their own occurrences:

| Truth track | Presented row | Occurrence check |
|---|---|---|
| Another Year Alone | Another Year Alone (i love you) | 63:12–67:33 overlaps truth 63:10–67:40 |
| Feel Emotion | Feeling Emotions | 51:21–55:03 inside 50:00–55:30 |
| BB MG | BB MG (UR) | 62:30–62:40 inside 62:00–64:30 |
| Marathon | Marathon (feat. Mall Grab) | 23:00–24:18 inside 21:00–25:00 |
| Winter | Winter (feat. Mall Grab) | 4:21–6:36 inside 4:00–7:00 |
| My Mind | My Mind | starts 85:33, overlapping truth through 85:45 |
| FORZ4 | spaced Testpress spelling | Same work; order-only truth |
| 4 Raws Edit | reversed `4raws - beau james` | Same work; order-only truth |
| Shella Verse | `Flowdan - Shell a Verse` | Same work; order-only truth |

None of the nine should be rejected. The corrected baseline rise 155→164 is therefore supported.

The scorer now obtains each label through the page’s `_candidate_label` ([score_corpus.py:433](scripts/score_corpus.py:433), [exports.py:142](src/id_detector/present/exports.py:142)) and calls fusion’s `same_work_labels` ([score_corpus.py:650](scripts/score_corpus.py:650)). It is no longer the old pooled-word matcher.

Before/after provenance is sound: the archived source files hash exactly to `361fe94`; every before artifact is fusion:4, every after artifact fusion:5; all seven pairs have identical source-run IDs and input-hash maps. Mixed known versions genuinely refuse at [score_corpus.py:1200](scripts/score_corpus.py:1200). `make_run_list.py` resolves `present/current`/the selected bundle’s frozen run, not flat `fuse/episodes.json` ([make_run_list.py:80](scripts/make_run_list.py:80)).

The 168→165 precision numerator is not entirely duplicate merging: two points are duplicate-work collapses—Long Season 2→1 and 1ofthozedaze 2→1—but the third is the `kettama - let me see` label regression losing its assignment. No distinct truth track is lost from recall.

## B — Tolerant matching

The specified structural guards exist: other-field agreement and “never both loose” at [identity.py:861](src/id_detector/fuse/identity.py:861); whole ordered titles at [identity.py:707](src/id_detector/fuse/identity.py:707); artist-slip length/two-name checks at [identity.py:830](src/id_detector/fuse/identity.py:830); initials require one solid owner at [identity.py:779](src/id_detector/fuse/identity.py:779); scorer uniqueness is at [score_corpus.py:679](scripts/score_corpus.py:679). Shared words alone do not merge.

However, word-ending and phonetic tolerance remain unsafe on short one-word titles. Executing the matcher shows `Artist - Dreams` is credited against truth `Artist - Dream`, and `Artist - Night` against `Artist - Nite`. The single-title guard only rejects lengths below five ([identity.py:720](src/id_detector/fuse/identity.py:720)); uniqueness cannot help when only one of those distinct works is in the mix.

Crowd uniqueness is also not true work uniqueness: `_unique` accepts several partners merely because their titles agree ([identity.py:1257](src/id_detector/fuse/identity.py:1257)). A verified counterexample merges `Alex Anderson`, `Alex Andersson`, and `Alex Anderon` on the same title into one work, although the latter two do not match each other.

No wrong tolerance merge was found in the measured 13 cached mixes, but these counterexamples defeat the required guards.

## C — One row per track

The measured Long Season, 1ofthozedaze, and Rok da House collapses are within single occurrences and are correct.

The separate-playing boundary fails for crowd-only tracks. Identity is unioned globally, without occurrence ([identity.py:1217](src/id_detector/fuse/identity.py:1217)), then `known_work_ids` suppresses every later timestamp of that work ([episodes.py:1289](src/id_detector/fuse/episodes.py:1289)). An in-memory case with spaced-Testpress spelling at 1:40, normal spelling at 10:00, and a different confident track between them produced only the first Testpress row. This change can therefore lose a genuine replay.

## D — Phantoms

The rule is deterministic and result-local: ambiguity uses the recorded native matches, reported recording ID, and reliable anchor ([episodes.py:238](src/id_detector/fuse/episodes.py:238)); `ambiguous_evidence` and `suppressed="ambiguous"` are persisted. Hint, clean-core, and second-engine exemptions work. Clean anchored Sandstorm remains listed.

On the seven truth mixes it removes 11 rows, all wrong, and zero correct rows. Correctness cannot be established for removed rows on the six mixes without truth.

De Barrio is not a plausible standalone truth track: all six listed rows are absent from the owner’s truth, and its many appearances across two sets look like a recurring sample/false recognition. Its anchored 48/57/75-second runs legitimately exempt it from this particular ambiguity rule, so this is an acknowledged residual false positive, not an introduced blocker.

## E — Fusion version, label regression, and scope

`FUSION_VERSION` is 5. Startup re-fusion produced 11 fusion:5 results on `C:\ifa\wa`; the before copy produced 11 fusion:4 results. The two Deep results remain refused by [refusion.py:576](src/id_detector/refusion.py:576). Timestamp audits found zero post-start modifications across 38,920 owner `work/` entries, 391 corpus entries, and 6,265 Deep-scan entries.

`kettama - let me see` is a visible regression caused by this identity split: the same 17:15 row previously displayed the fuller ID label. It is not a newly listed wrong row, but the owner will notice it.

The golden blob is unchanged (`316b6171…`) and its shard reports passing. `profiles/`, truth, and all named second-session files are untouched. No renderer was changed, so no `PAGE_VERSION` bump is required. Existing assertion changes are version pins plus one stricter scorer assertion; none was loosened.

The two revert-surviving tests are genuine invariant guards: established readings remain tolerance-free, and clean Sandstorm remains listable. Suite arithmetic is consistent: 2,152 passed + 2 skipped = 2,154 collected, with all 93 root and 21 web files mapped once. No secret or unsuitable public artifact was found.

## Required fixes

### Realistic

- **P1:** Preserve occurrence identity for crowd-only tracks; do not let a globally merged work suppress a later replay. Add a far-apart, intervening-track regression test.
- **P1:** Tighten single-word stem/phonetic matching so `Dream/Dreams` and `Nite/Night` cannot be credited as the same work solely from artist agreement and corpus uniqueness.

### Adversarial

- **P1:** Make crowd tolerance uniqueness compare resolved work components, not merely partner titles; cover the three-artist bridge counterexample.
- **P2:** Cross-check a run-list-supplied `fusion_version` against artifact provenance rather than allowing a manually incorrect value to override it.

## Follow-ups, not blockers

- **P2:** Restore the fuller Mall Grab “LET ME SEE U / ROK DA HOUSE” presentation label.
- **P2:** Investigate a separate evidence rule for the six De Barrio false positives without weakening the clean-core exemption.

VERDICT: FIX_FIRST
