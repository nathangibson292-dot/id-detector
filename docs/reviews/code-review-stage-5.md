# Stage 5 code review — calibration & test

*Reviewer: Claude (Opus) — read-only review against docs/PLAN.md rev 5.2.*

**Verdict: FIX_FIRST.** The committed deliverable is honest and the certification gate genuinely cannot be gamed today: production stays heuristic (no calibration model committed for free/max_accuracy), and `certify` on the *verified frozen* controlled corpus returns every triple `provisional` with n=0. The CP math, the five-condition certified gate, the cumulative at-or-above-tier population, the refusals (exit 2 for unfrozen corpus / missing manifest / reused test_version), the monotone PAV calibrator, PI coverage/width/Winkler, and privacy all verified sound. Two P1s sit on the future real-mix fit/certify path.

### [P1] `certify` never applies the selected `--profile` or its calibrator to real-mix sets
`run_certify` threads the calibrator into controlled sets but calls `_run_real` with no profile and no calibrator; `_run_real` shells out to `analyse … --no-hints` with no `--profile` and `fusion=None`, so real-mix predictions are profile-agnostic and always heuristic. The documented `certify --corpus <real-v1> --profile free` would certify predictions that don't match the profile's shipped behaviour. Latent today (no real corpus frozen) but wrong for the eventual real-mix certification. Report's "Deviations: None" is inaccurate here.
**Where.** `src/id_detector/calibrate/certify.py:212-218`; `src/id_detector/benchmark/corpus.py:248-275`.
**Fix.** Pass `--profile` (and toggles) into `_run_real` so real-set predictions are the profile's production output and a committed calibration model is honoured; until wired, the real-set branch must refuse rather than silently score heuristic profile-agnostic predictions. Correct the deviations claim.

### [P1] Features not recomputed identically at fit vs analyse time (`recording_supported`)
`reconstruct.py:76` computes `recording_supported` as ≥1 recording node & not contested; the authoritative analyse-time rule (`identity.py:361-376`, used at `episodes.py:482-484`) is not-contested AND (≥2 recording nodes OR a recording node with ≥2 sources). Fit-time is strictly more permissive. This feeds the version ordering index (`features.py:193-199`) and the hard version→unclear cap (`model.py:414`), so the version dimension would be fit on one distribution and applied on another — masked on controlled data (oracle attaches ≥2 ids) but wrong for a real single-engine ISRC. Violates the plan's "features recompute identically".
**Where.** `reconstruct.py:76,91` vs `episodes.py:482-484,593` and `identity.py:361-376`.
**Fix.** Share one helper so `recording_supported` (and `n_competing_candidates`) are computed identically in both places.

### [P2] Certification population is a blocklist (`"controlled"`/`"self-index"` excluded), not the plan's strata-1–2 allowlist — a future mislabelled stratum could leak in. `scorer.py:1277-1283`. Allowlist real-mix strata.
### [P2] `_split_sets` ignores its `seed` (dead param, misleading provenance); `certify`'s printed `n_test_predictions` sums 15 cumulative tier populations (double-counts), disagreeing with `validate.py`'s episode count. `validate.py:100-106,410`; `certify.py:243`.
### Nit. stage-5.md:144 says "audited 328 files"; audit now reports 329.

## Verified
`ruff check` clean; fixture audit 329 files passed; `pytest -q` 480 passed / 3 deselected; Stage 5 suite 11 passed. `certify` on frozen controlled-synth-1 → 0 certified, 15×provisional, n=0 (corpus verifies frozen yet nothing certifies); repeat/unfrozen/missing-manifest all exit 2. Committed `calibration-validation.json` labelled `population: controlled -- not real-mix certification`, all provisional. Fitted models and certify reports land under gitignored `data/local/`; nothing forbidden committed.

REVIEW VERDICT: FIX_FIRST
