# id-detector — status

*Consolidated at Stage 9 (final v1 stage). Plan: [PLAN.md](PLAN.md) rev 5.2. This file is the honest,
one-page answer to "what actually works, and what is only claimed?"*

## v2 (hosted product) — where the build stands

*Updated 2026-09-13. Plan: [PLAN-v2.md](PLAN-v2.md) rev 6; cycle log: [reviews/README.md](reviews/README.md).*

**Phases 0, 1 and 2 are complete and committed, plus presentation cycles 3a-i and 3a-ii and service cycle 4a-i.** Test suite: 1139 passed offline.

Phase 0 (cycles 0a-i, 0a-ii, 0a-iv+0a-iii, 0b-i+0b-iii, 0b-ii): the paid (Deep) path no longer crashes after
spending; AudD error bodies are never cached; spend is reserved, admitted per request against a hard cap and
settled/journaled honestly; every run ends with an explicit status (`complete / degraded / partial /
provider_unavailable / budget_exhausted / source_changed`) and exit code; `--recipe free|deep` (Deep is
explicit opt-in; `pricing.toml` is the single pricing authority); AudD observations carry a validated anchor
and their own vote; the paid pass is concurrent, rate-limited, retried, cancellable, and journals every
attempt durably before network I/O; config knobs are effective under profiles and `idea config show` explains
them; malformed Shazam replies count as failures, never as "no match"; a semantic Local-Free golden pins
Free-scan output; loopback CSRF/Origin protects the local server.

Accuracy tooling (before Phase 1): the corpus scorer (`scripts/score_corpus.py`), the seven-set `release-1`
truth drafts and first Free numbers ([release-1-free-draft](accuracy/release-1-free-draft.md): pooled recall
64 %, precision 75 %, "likely" 90 % — draft truth, not an L3 claim), **1b-i** secondary targeting v2
(`targeting:1`) and **1b-ii** cross-family corroboration (`fusion:2`).

Phase 1 (cycles 1a-i+1a-iii, 1a-ii, 1b-iii breaker):
- **Result bundles.** Results live in `present/bundles/<bundle_id>/` with a sealed manifest hashing every
  file, and fuse artefacts are frozen per run under `fuse/runs/<run_id>/`. Files and manifests are fsynced
  before any pointer or journal entry names them, bundles are never rewritten, and refresh mints a new one.
  `present/current` follows the newest `complete` run in local mode only.
- **Cached-open and audio.** A cached mix opens without its original media through a rebuildable
  `work/index.json` (every source alias preserved); the page streams from a traversal-safe, range-capable
  `/media/<media_key>/audio`. Pre-bundle runs on disk still open through legacy fallbacks and are never
  rewritten. Owner gate: `scripts/gate_local_mode.ps1`.
- **Analysis keys and compatibility.** `analysis_key` hashes the plan's inputs; `serves()` (`compat_version 1`)
  implements the §3.4 table — d=1 answers a d=2 request but never the reverse, unequal algorithm/adapter
  versions are never served, `partial` never, `degraded` only with `accept_degraded`, scope must match.
  Lookup is alias-wide, so the same audio reached by a second URL is served from cache instead of re-paying
  AudD. `serve_free_from_deep` and `compat_version` come from `pricing.toml` alone. A Deep request over an
  existing Free result reuses its Shazam evidence, reserves the primary only and issues zero Shazam requests.
  Re-read bytes that no longer hash to `media_key` end the run `source_changed` (exit 5).
- **Shazam breaker (D8).** Qualifying failures over all resolved attempts in a rolling 5-minute window,
  minimum sample 20: > 30 % opens 30 minutes, an exhausted daily budget opens until 00:00 UTC, three rate
  trips in one UTC day latch Shazam off until an explicit re-enable. A running free primary continues, a deep
  secondary is skipped → `degraded` naming the rule, new free work reports `waiting` (a cached compatible
  result is still served). `IDEA_ENGINE_SHAZAM=off` remains a hard off.

Phase 2 and the first presentation cycle (committed 2026-09-11/12):
- **2b — retention.** `idea gc --policy local|hosted` reclaims a finished analysis: window clips at once, PCM
  after 48 h, and on hosted the fetched original after 7 d while **local always keeps the original**. Failed
  and cancelled runs lose intermediates immediately and their media directory after 7 d if unreferenced. GC is
  opt-in, defaults to a dry run, never deletes in place (it moves to `work/.trash/<date>/` and purges only once
  provably 7 days old), rejects symlink/junction escapes, and treats anything it cannot read as a reference.
  Pruned artefacts leave a `pruned_upstream` marker; consumers re-derive windows ← PCM ← original ← re-fetch,
  and re-fetched bytes that no longer hash to `media_key` end the run `source_changed`.
- **3a-i — canonical projection.** Presentation rules live in one typed, ordered projection per run, and the
  page table, hero counts, legend, timeline lanes, library card count and confidence bar, Copy, CUE, Markdown
  and JSON all render from it, so one mix can no longer report four different tracklists. Re-rendering mints a
  new immutable bundle from the frozen snapshot with page and exports written together. Markdown drops the
  `Version`/`Role` columns the page hides (JSON keeps them); the dead M3U export is gone. Suppression is applied
  per episode **before** rows collapse, and coverage is a true union, so the "identified" percentage no longer
  double-counts overlapping tracks.

**Also on main (a second Claude session, commit `6ef821f`):** a self-contained playlists/Likes feature
(`src/id_detector/playlists/`, storage in git-ignored `data/local/playlists.json`) and the display-brand rename
**IDea → ID'er**. Internals are deliberately unchanged: the `id_detector` package, the `idea` console script,
`idea.cmd` and every `IDEA_*` variable. Its five-point result-page contract (row anchor, both row identifiers,
asset injection, controls on shown rows only, the result URL shape) is a standing constraint on every
presentation cycle.

- **3a-ii — honesty, accessibility, mobile.** The pages say only what the projection supports: the completion
  screen counts shown tracks, the `degraded` banner names the real reason from the run's frozen status, and a
  failure states what failed, where, and what it actually cost. Library cards take confidence from audio
  evidence alone and count crowd rows separately. `POST /rescan` is gone. Provider names, diagnostic tokens and
  private URL slugs are out of the interface. Progress is a wall-clock model (recognise 89 of 100 points,
  cached phases handled, never moving backwards). On a phone the "Where to get it" column is back with 44 px
  targets; the table has real semantics and no colour-only meaning; contrast ≥ 4.5:1 in both themes. Failed runs
  appear in the library with cause and cost. `scripts/check_page_js.py` node-checks every inline script across
  21 page renders; screenshots and vendored `axe-core@4.10.2` are non-blocking evidence (axe: zero violations).

Phase 4 has begun:
- **4a-i — service API and packaging.** `id_detector.service.run(RunRequest) → RunResult` is the only way to
  analyse a mix; the pipeline lives in `id_detector/pipeline.py` and the CLI and web runner are thin callers
  sharing one status-to-exit mapping. The target is a typed union (platform URL, upload id, local path), with
  local paths refused outside local mode and URLs validated at the seam so a filesystem path cannot arrive as a
  link. Nine durable checkpoints mean a crash resumes rather than repeats: resuming from the primary sweep
  issues zero AudD requests, recovered units are charged against the original reservation before any new
  dispatch, and a clip whose outcome is already known is never re-sent. `src/idea_web/` is packaged and in the
  wheel (no app yet — that is 4a-ii).

**Not started:** 4a-ii onwards (service API, FastAPI, worker, accounts,
credits), 6a–6b (ingest policy, container, launch checklist), then M2 (billing, Stripe sandbox) — see
PLAN-v2 §5.

**Owner-side, open:** verify the seven truth drafts (`idea truth verify`) and fix the ~+50 s tracklist clock
offsets on the Mall Grab and DJ Heartstring sets while verifying; AudD's reply on the per-clip subscription
rate plus a ~$20 top-up (covers Deep scoring of the whole corpus); run `scripts/spike_shazam_vps.py` (S3) on a
throwaway host to replace the **provisional 2,000/day Shazam attempt budget** with a measured ceiling, and
`scripts/spike_ingest_vps.sh` (S2) before any hosting work.

**Update 2026-09-14.** Also committed: **4b-i** durable queue and worker (`8e937ab`); **`idea truth review`** (`ef94b70`) plus a corpus furniture fix and audit check (`362ae10`); and **4a-ii** FastAPI parity (`3cb5dc2`): one FastAPI/uvicorn server for `idea serve` and `idea truth review`, a durable local job queue at `work/.idea/app.db` with a worker process supervised by `idea serve`, no writes on GET, bounded request bodies, streamed audio, and `http`/`https`-only buy and download links. Test suite: 1287 passed offline.

**Open:** sol-xhigh retro-reviews of 4a-i, 4b-i and the truth tool found blockers (see `docs/reviews/retro-review-*.md`); follow-up fix cycles are next. Until they land: **do not run any paid Deep scan** (a worker restart behind idea serve can currently re-send paid clips and lose recorded spend), **do not freeze or certify the corpus**, and **do not run idea gc --apply** (a plain idea gc preview is safe; the apply path can follow links out of the work folder). Verifying truth rows in idea truth review is still fine.

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
