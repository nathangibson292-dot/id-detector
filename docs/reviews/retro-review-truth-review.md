## A. Findings both earlier reviews missed

1. **P1 — The two-file save is not crash-atomic.**  
   Evidence: `ef94b70:src/id_detector/truth.py:524-543` replaces `annotation-first.json`, then `ground_truth.json`; rollback exists only in the live process’s `except` block.  
   Failure scenario: terminate the process or lose power after line 536 but before line 537. The new annotation remains while truth remains old. If an annotation previously existed, its bytes and provenance are lost because the only backup was in memory. Per-file replacement is atomic; the save transaction is not.

2. **P1 — “Display-only” verified rows can have their annotator provenance rewritten.**  
   Evidence: `ef94b70:src/id_detector/truth_review.py:285-295` retains completed rows, but `ef94b70:src/id_detector/truth.py:590-606` reconstructs every episode with the new `first_ref`.  
   Failure scenario: row 1 was already verified by `alice`, row 2 remains draft, and the owner completes row 2. Saving changes row 1’s `annotator_ref` to `owner`, falsely attributing Alice’s work. The session accepts such mixed states at `truth_review.py:397-412`.

3. **P1 — The frozen terminal state is not guarded.**  
   Evidence: `ef94b70:src/id_detector/truth_review.py:400-403` rejects second-pass and resolved records, but checks neither `corpus_version` nor a freeze manifest.  
   Failure scenario: a frozen `dev-1` set without `second_pass_ref` reopens. Save can replace its annotation and provenance; reveal can add exposure after freezing. This steps backwards over the stated terminal state and invalidates frozen evidence.

4. **P1 — The new furniture audit is both over-broad and incomplete.**  
   Evidence: `362ae10:scripts/audit_fixtures.py:279-305` rejects any artist/title beginning with one of five glyphs.  
   Failure scenarios:

   - Correct labels such as `*NSYNC` or the artist `-M-` are falsely rejected, even though no furniture separator exists.
   - `− Mall Grab` using U+2212 MINUS SIGN, or `‐ Mall Grab` using U+2010 HYPHEN, passes.
   - Ordinary leading whitespace is covered by `\s*`, but an invisible U+200B before `- Mall Grab` bypasses the check.

## B. Earlier findings whose fix is incomplete or wrong

1. **P0 — Report P0-1’s role-preservation fix still deletes hand annotations when a bulk offset clamps a non-final row.**  
   The report claims bulk offsets translate roles (`build-truth-review.md:248-268`). The browser shifts only episode ranges (`ef94b70:src/id_detector/truth_review.py:541-558`); the server infers a translation only when all four endpoint deltas match, otherwise it clips the original, unshifted roles (`:223-245`).  
   Failure scenario using the committed 60-second fixture: apply `+25 s`. Row 2 moves from `20–40 s` to `45–60 s` without collapsing, but its original `20–40 s` role clips to zero and is dropped. Save succeeds with `role_segments=[]`. The correct preview implementation would have moved it to `45–60 s` (`:157-163`).

2. **P0 — Report P0-2 made exposure durable but did not prevent exposed truth from becoming certified truth.**  
   Exposure is recorded before predictions (`ef94b70:src/id_detector/truth_review.py:424-442`), but `freeze_truth` never rejects it and omits `review-exposure.json` from the manifest (`ef94b70:src/id_detector/truth.py:848-909,925-936`). The L3 wrapper calls a verifier concerned only with frozen truth bytes and draft status, then emits `certifiable: true` (`ef94b70:scripts/score_corpus.py:422-430,1034-1040`).  
   Failure scenario: reveal predictions, save, complete the remaining pass, freeze, then score. The output can call the result verified and certifiable although the owner’s first pass saw IDea’s answers. Current `HEAD` contains no consumer of the exposure flag outside `truth_review.py`.

3. **P0 — Report P1-6 validates only the truth path, not the sibling paths it writes.**  
   Evidence: session initialization guards `ground_truth.json` only (`ef94b70:src/id_detector/truth_review.py:397-408`); exposure and annotation paths are derived without validation (`:347-383`, `ef94b70:src/id_detector/truth.py:327-328,501-542`). The atomic writer resolves symlinks before writing (`ef94b70:src/id_detector/io.py:80-105`).  
   Failure scenario: `review-exposure.json` is a symlink into a prunable media directory beneath `work/`. Reveal writes there and returns predictions. After that work target disappears, restart sees no exposure and can save `false`. Likewise, a linked `annotation-first.json` makes verified truth depend on an annotation stored beneath `work/`.

4. **P1 — Report P1-6 also remains bypassable with Windows extended-length paths, and P1-5’s lock inherits the alias.**  
   Evidence: `_real()` retains `\\?\` (`ef94b70:src/id_detector/truth_review.py:51-75`), while the lock key uses the same spelling (`ef94b70:src/id_detector/truth.py:415-427`). An in-memory check on this host produced `c:\…\work` versus `\\?\c:\…\work`; `is_relative_to` returned false.  
   Failure scenarios: `--corpus \\?\C:\…\work\corpus` bypasses the work prohibition; two sessions opening one corpus file via ordinary and extended spellings receive different lock files, both pass the digest check, and the later commit overwrites the earlier one.

## C. Test gaps

- `tests/test_truth_review.py:468-479` tests only `+4 s`; it never clamps a non-final row or asserts every role segment’s resulting coordinates.
- The work-path tests at `:724-784` cover the truth file and ordinary aliases, not linked annotation/exposure siblings or `\\?\` spellings.
- There is no reveal → freeze → score regression proving exposed truth is non-certifiable.
- `:391-408` injects a catchable exception, not process death, and starts without an existing annotation. This does not substantiate the report’s claim of existing-annotation rollback coverage.
- Commit `362ae10` added no tests for legitimate leading punctuation, Unicode dash variants, or invisible leading characters.

I could not run `uv` or pytest in this read-only sandbox, and I did not start a server. These gaps are based on committed test inspection.

## D. What you verified is correct

- Static inspection confirms hidden HTML contains truth rows only; `_predictions()` is reached only through the protected POST route, and guesses have no accept/fill path.
- For ordinary canonical destinations, exposure is written before predictions are assembled; a normal failed exposure write prevents the response.
- Save validates row count/order, positive spans, confirmation of every draft row, and immutability of verified labels/times.
- The cross-process lock does cover the session digest re-check, `verify_truth`, commit, and caught-exception rollback when all callers use the same canonical spelling.
- Preview is browser-local, clamps ranges to media bounds, detects collapsed rows before apply, and undo restores only start/end fields.
- Mutations are POST-only and pass Host, Origin, and constant-time CSRF checks; `/csrf` has the Host check. Set lookup is content-based behind a restrictive ID regex, and the audio route requires exact path equality.
- I independently parsed the seven records at `362ae10`: 222 role segments remain, comprising 213 `uncertain` and nine `layer`.
- The `362ae10` corpus correction changes exactly 25 artist fields across 25 episodes; no non-artist field differs structurally.
- Current `HEAD` has not changed the reviewed truth-review implementation; among these files it only retains the later audit addition.

## Required follow-ups

1. **[P0]** Make freeze and every L3/certification entry point reject or explicitly mark non-certifiable any set whose exposure sidecar or first annotation records prediction visibility; integrity-hash that evidence in the manifest.
2. **[P0]** Validate every prospective truth, annotation, exposure, temporary, and rollback destination after link resolution, reject reparse points escaping the corpus set or entering `work/`, and close the check/write race with handle-rooted operations where necessary.
3. **[P0]** Carry the actual bulk-offset intent into role reconciliation so every role endpoint receives the identical shift and clamp; add the fixture `+25 s` regression.
4. **[P1]** Canonicalize Windows namespace prefixes consistently for both containment and lock identity, including `\\?\C:\…` and `\\?\UNC\…`.
5. **[P1]** Add a durable transaction/recovery record so interruption between annotation and truth replacement can restore or complete the pair after restart.
6. **[P1]** Reject mixed-annotator and frozen inputs, or preserve each preverified row’s existing annotator rather than rewriting it.
7. **[P1]** Replace the glyph-prefix furniture heuristic with context/provenance-aware detection, normalize intended Unicode variants, and regression-test `*NSYNC`, `-M-`, Unicode minus/hyphens, whitespace, BOM, and zero-width prefixes.

VERDICT: FOLLOW_UP_REQUIRED