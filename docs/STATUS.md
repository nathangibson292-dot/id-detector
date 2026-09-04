# id-detector — status

*Consolidated at Stage 9 (final v1 stage). Plan: [PLAN.md](PLAN.md) rev 5.2. This file is the honest,
one-page answer to "what actually works, and what is only claimed?"*

## Acceptance status at a glance

Legend: **met** = the stage's plan gate is satisfied as written · **met (controlled only)** = proven
on the synthetic/controlled stratum, where exact truth exists, but not certified on real mixes ·
**met (Shazam only)** = the free engine is done; paid/optional engines are gated on owner action ·
**pending owner** = blocked on a decision or resource only the owner can provide · **excluded (v1)** =
deliberately out of v1.

| Stage | Capability | Status | Why / what is outstanding | Report | Review |
|---|---|---|---|---|---|
| 0 | Preflight & contracts | **met** | doctor passes; every record has schema + golden + semantic vectors; derive + audit pass | [stage-0](stage-reports/stage-0.md) | [review](reviews/code-review-stage-0.md) |
| 1 | Plumbing (ingest/decode/windows/Shazam/job store) | **met** | five-point failure injection = one submission each; `physical_attempts` matches the fake server; ~1 h set completes and survives Ctrl-C + crash + cache re-run; Windows path/process tests | [stage-1](stage-reports/stage-1.md) | [review](reviews/code-review-stage-1.md) |
| 2a | Scorer & controlled slice | **met** | scorer vectors pass; controlled-transform truth validates with the audible rule | [stage-2a](stage-reports/stage-2a.md) | [review](reviews/code-review-stage-2a.md) |
| 2b | Corpus dev-1 + baseline fuser | **met** (corpus is dev-only) | baseline report on `dev-1` committed pseudonymously; identity veto, one-sided bounds, durations partition, gaps, provisional tiers all vector-tested. `dev-1` is a *development* set, not a certification corpus | [stage-2b](stage-reports/stage-2b.md) | [review](reviews/code-review-stage-2b.md) |
| 3 | Adapters & shortlist | **met (Shazam) / pending owner (paid) / excluded (Panako)** | Shazam shortlisted with live numbers; AudD + ACRCloud built with recorded fixtures + entitlement smoke but **not evaluated (no credentials)**; Panako **excluded — no JDK**; reference-pool recognition excluded from v1 | [stage-3](stage-reports/stage-3.md) | [review](reviews/code-review-stage-3.md) |
| 4a | Hints | **met (on held-out dev-2)** | parser fixtures pass; on `dev-2` the fused-vs-audio gate meets +coverage with a one-sided cluster bound > 0 and precision non-inferior | [stage-4a](stage-reports/stage-4a.md) | [review](reviews/code-review-stage-4a.md) |
| 4b | Transforms & schedule | **met** | insertion vectors pass for every factor (duration, first/last mapped sample, bias, slope); paired benchmark chose the grid/schedule with false-match rate reported | [stage-4b](stage-reports/stage-4b.md) | [review](reviews/code-review-stage-4b.md) |
| 4c | Rescans, scanners, events | **met (controlled only)** | all four gates met on the controlled stratum (356 boundaries; per-type event P/R ≥ 80 % on ≥ 30 cases; `best_start` p90 −20 % relative; no double submission) | [stage-4c](stage-reports/stage-4c.md) | [review](reviews/code-review-stage-4c.md) |
| 4d | Profile freeze | **met** | `free` and `max_accuracy` frozen from the 4c ablations; both are Shazam-only in v1; AudD/ACRCloud/Panako recorded `eligible_when_available` | [stage-4d](stage-reports/stage-4d.md) | — |
| 5 | Calibration & test | **pending owner** | the calibration/certification machinery is built and validated on controlled data; **no owner-verified real-mix corpus exists**, so real-mix tiers stay `provisional` with `n_test_predictions: 0`. No certification is fabricated | [stage-5](stage-reports/stage-5.md) | [review](reviews/code-review-stage-5.md) |
| 6 | Where to get it | **met (code) / pending owner (audit)** | Deezer/Apple/MusicBrainz/Discogs lookups, SoundCloud flags, gated/search links, direct-only-on-strong-agreement — all built and run live; the ≥ 95 % on ≥ 60 links **audit gate is pending owner marking** of a stratified sample | [stage-6](stage-reports/stage-6.md) | — |
| 7 | Web page | **met** | player + timeline (evidence, PI shading, unresolved zones, gaps) + badges + roles + acquire chips; seek lands within 1 s of target; loopback read-only server; rescan queue | [stage-7](stage-reports/stage-7.md) | — |
| 8 | Panako full | **excluded (v1)** | conditional on the owner's JDK decision; not built | — | — |
| 9 | Polish | **met** | CUE flattening (+ REM overlaps) and M3U exports; single documented `id-detector.toml` with `config show/init`; owner README; this file; fast default test suite; 39-vs-55 resolved (below) | [stage-9](stage-reports/stage-9.md) | — |

**Bottom line for v1:** the tool runs end-to-end on the free Shazam profile, is deterministic and
privacy-audited, and is honest about uncertainty. **Every real-mix accuracy tier is `provisional`**
because no funded, second-pass-verified corpus exists. Paid engines and Panako are wired but gated on
owner action.

### Standing owner decisions (from the plan)

1. Fund a real calibration/test corpus (held references, blind second pass) to certify tiers, or ship
   v1 with provisional tiers (**current default**).
2. Provide AudD/ACRCloud trial credentials + a hard test budget to evaluate the paid engines.
3. Provide a JDK for a minimum Panako path, or leave reference-pool recognition excluded from v1
   (**current default**).

The code-review round-by-round record is in [reviews/README.md](reviews/README.md).

---

## Resolution: the dev-1 "39 → 55 episodes" question (authoritative)

**Verdict: granularity / conflation of two different mixes — NOT fragmentation. No code change was
needed; the current fuser reproduces every committed number exactly.**

The "39 → 55 behavioural change" was reported as if the *same* cached generation-0 observations
re-fused to a different episode count. They do not. The two numbers come from **two different Boiler
Room sets** that were both used as `dev-1` captures:

| | Set A | Set B |
|---|---|---|
| Set | **Kaytranada** Boiler Room Montreal | **DJ Three** 60-min Boiler Room mix |
| Work dir | `data/local/work-dev1-live/9474…/1501…/` | `work/c5dc…/ec0a…/` |
| Duration | 2,525,123 ms (~42 min) | 3,587,506 ms (~60 min) |
| gen-0 observations | 281 (187 final matches) | 399 (301 final matches) |
| Committed `fuse/episodes.json` | **39 episodes**, 0 gaps, 37 distinct candidates | **55 episodes**, 0 gaps, 48 distinct candidates |
| Re-fuse with current `build_episodes` | **39** (identical) | **55** (identical) |

**How this was verified, without any network.** For Set A the committed `episodes.gen0.done.json`
sidecar lists its exact upstreams; those five files were hashed and confirmed byte-for-byte
(`observations.gen0.jsonl` = `3f67bdd6…`, `windows.gen0.jsonl` = `96d709a1…`,
`identities.gen0.json` = `41d27f2d…`, `hints.jsonl` = `b468d644…`, `pcm.json` = `fc59b31b…`, from
invocation `3cef4e5fb3c08345f5a8`). Re-fusing those exact inputs with the current fuser produced **39
episodes / 0 gaps / 37 candidates** — identical to the committed file. Set B's committed file
(already rev-5.2 schema) likewise re-fused to **55**.

So 39 vs 55 is simply a 42-minute Kaytranada set with 37 identified tracks versus a 60-minute DJ
Three set with 48 identified tracks. **More distinct tracks in a longer, different mix — granularity.**

**Fragmentation was specifically ruled out.** A fragmentation regression would show a single
continuous track chopped into consecutive occurrences (`occurrence_index` 0, 1, 2 …) across small
recognition gaps below the 30 s replay threshold. Auditing every multi-occurrence candidate in both
sets:

- Set A has exactly **2** candidates with a second occurrence; Set B has **7**.
- Every one is a genuine **reference recurrence**: the reference position returns to the same or an
  earlier region after a > 30 s mix gap (e.g. Set A `dcd68806bb24`: the intro region, ref ≈ 0, is
  replayed 36 s later; `cf6ad835af84`: ref recurs ~5 s earlier after 36 s), or two isolated
  single-point matches minutes apart (e.g. Set B `b99bc38f8996`: matches at 9 s and 729 s). This is
  exactly the plan's `replay` predicate ("the same ref region recurs after > 30 s").
- **None** is a continuous, forward-advancing, reference-consistent track split over a sub-30 s gap.

**Why the alignment code cannot fragment a continuous track.** `align_candidate_points` applies
*continuation-by-reference-consistency before replay*: a reference-consistent point continues the
occurrence for any gap ≤ 120 s, guarded only against `gap > 30 s AND same_ref_region` (a same-region
recurrence). A forward-advancing consistent match after a 30–120 s recognition drought therefore
**continues one occurrence** (its reference has moved on, so `same_ref_region` is false). This is
locked in by `test_replay_and_continuation_gap_boundaries`,
`test_reference_consistent_forward_run_across_a_long_drought_stays_one_occurrence`,
`test_anonymised_real_anchor_excerpt_is_one_continuous_occurrence`, and the new
`test_real_dev1_intro_replay_is_granularity_not_fragmentation` (built from anonymised mix/ref anchor
pairs of the real Set A intro-replay candidate — no labels).

**Note on the old 39 file.** The committed Set A `episodes.json` predates rev 5.2 (its episodes have
no `rejected_evidence` field), yet its episode **count is invariant** under the Stage 4b
per-trial-selection / `T_ind` change and the Stage 4c alignment narrowing (39 → 39): those changes
move tiers, proved bounds, and provenance, not the number of occurrences. There is nothing to fix.
