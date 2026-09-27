# Build — paid adds to free, only where free is blind, and never without a price

Builder report for the change on top of `5a035f4` (uncommitted; the orchestrator reviews and merges).
Everything below was measured or run **offline**: fake providers in tests, and for the owner's three
mixes only the stored Free evidence in `work/` plus the paid answers already cached (and paid for) in
`id-detector-deepscan`. **New AudD requests made: 0. New Shazam requests made: 0.**

## In plain words

* Deep is now **free-first and additive**: the free Shazam sweep runs first (or, when this mix already
  has a Free result, that sweep is reused — no Shazam request, no time), then AudD checks **only the
  gaps** the free result leaves, then one fusion adds what AudD found. Paid evidence can confirm or add
  a row, never remove one the free evidence lists on its own.
* The exact price is shown **after the free pass**, before anything is reserved: on the command line
  (the existing confirmation moved there), in `idea cost` (computed offline for a Free-scanned mix), and
  — closing the money gap — in the browser, where "Max accuracy" now stops after its free pass with
  "Deep would check N gaps for about $X", Approve / Skip.
* The honest headline: **on these three mixes, AudD adds no new correct track to today's Free
  result.** The five Mall Grab tracks the earlier experiment credited to "Deep" were found by the
  newer *fusion rules* reading the mix's own tracklist hints (fusion:4), not by AudD's audio: AudD's
  stored answers never name Bear Witness, Madman, 1ofthozedaze or Get Down (see §1.4).

## 1. The measurement (item 1) — from the cache, spending nothing

`scripts/measure_additive.py` (new) re-fuses each mix's **proven** stored Free evidence with today's
fusion, adds the **cached** AudD answer for every window a policy selects (keyed exactly as the live
sweep keys it: `clip_cache_key(window.wav_sha256, "audd", CLIP_CONFIG_VERSION)`), and scores with the
project's own scorer (work-matched, time-blind; plus a time-window check). A window without a cached
answer is counted as "would cost one request" and never sent. It runs inside `scripts/offline_guard.py`
(empty `AUDD_API_TOKEN`, `IDEA_ENGINE_SHAZAM=off` in-process, every non-loopback connect/lookup raises
and is counted). Guard count for every run below: **0**.

Policies: **(a) everywhere** — every frozen window; **(b) gaps** — `select_gap_targets` (complement of
every non-suppressed free row); **(c) gaps + unsure** — `select_scan_targets` (complement of the
confident rows only). "Free (stored)" is the owner's stored Free result (fusion:1 — the baseline the
brief's table used); "Free (today)" is the same stored evidence under today's fusion (what the Free
recipe shows now). All policy rows use the additive merge of §2.

### 1.1 With the mixes' own stored hints (the real-world case)

```
Mix | Policy | Work recall | Work precision | Likely precision | Paid clips | Cached | Would cost
--- | --- | --- | --- | --- | --- | --- | ---
mall-grab-boiler-room-melbourne-22 | Free (stored) | 8/24 (33.3%) | 10/20 (50.0%) | 3/7 (42.9%) | 0 | 0 | 0
mall-grab-boiler-room-melbourne-22 | Free (today) | 13/24 (54.2%) | 21/33 (63.6%) | 3/3 (100.0%) | 0 | 0 | 0
mall-grab-boiler-room-melbourne-22 | everywhere | 13/24 (54.2%) | 22/34 (64.7%) | 3/3 (100.0%) | 600 | 600 | 0
mall-grab-boiler-room-melbourne-22 | gaps | 13/24 (54.2%) | 23/35 (65.7%) | 3/3 (100.0%) | 402 | 402 | 0
mall-grab-boiler-room-melbourne-22 | gaps_and_unsure | 13/24 (54.2%) | 23/35 (65.7%) | 3/3 (100.0%) | 441 | 441 | 0
mph-youtube-set | Free (stored) | 31/62 (50.0%) | 31/40 (77.5%) | 9/11 (81.8%) | 0 | 0 | 0
mph-youtube-set | Free (today) | 33/62 (53.2%) | 33/41 (80.5%) | 9/10 (90.0%) | 0 | 0 | 0
mph-youtube-set | everywhere | 33/62 (53.2%) | 33/41 (80.5%) | 9/10 (90.0%) | 602 | 602 | 0
mph-youtube-set | gaps | 33/62 (53.2%) | 33/41 (80.5%) | 9/10 (90.0%) | 386 | 386 | 0
mph-youtube-set | gaps_and_unsure | 33/62 (53.2%) | 33/41 (80.5%) | 9/10 (90.0%) | 511 | 511 | 0
dj-heartstring-youtube-set | Free (stored) | 10/21 (47.6%) | 13/24 (54.2%) | 3/3 (100.0%) | 0 | 0 | 0
dj-heartstring-youtube-set | Free (today) | 10/21 (47.6%) | 13/24 (54.2%) | 4/4 (100.0%) | 0 | 0 | 0
dj-heartstring-youtube-set | everywhere | 10/21 (47.6%) | 16/27 (59.3%) | 4/4 (100.0%) | 605 | 604 | 1
dj-heartstring-youtube-set | gaps | 10/21 (47.6%) | 14/25 (56.0%) | 4/4 (100.0%) | 440 | 439 | 1
dj-heartstring-youtube-set | gaps_and_unsure | 10/21 (47.6%) | 15/26 (57.7%) | 4/4 (100.0%) | 502 | 501 | 1
Pooled | Free (stored) | 49/107 (45.8%) | 54/84 (64.3%) | 15/21 (71.4%) | 0 | 0 | 0
Pooled | Free (today) | 56/107 (52.3%) | 67/98 (68.4%) | 16/17 (94.1%) | 0 | 0 | 0
Pooled | everywhere | 56/107 (52.3%) | 71/102 (69.6%) | 16/17 (94.1%) | 1807 | 1806 | 1
Pooled | gaps | 56/107 (52.3%) | 70/101 (69.3%) | 16/17 (94.1%) | 1228 | 1227 | 1
Pooled | gaps_and_unsure | 56/107 (52.3%) | 71/102 (69.6%) | 16/17 (94.1%) | 1454 | 1453 | 1

everywhere: 1807 paid clips = $9.04 at list price if none were cached
gaps: 1228 paid clips = $6.14 at list price if none were cached
gaps_and_unsure: 1454 paid clips = $7.27 at list price if none were cached
(new requests this measurement: 0 — 1 window had no cached answer and was counted, not sent)
Network attempts blocked by the guard: 0.
```

(The pooled truth is 107, not 108: the scorer pools distinct truth works; the brief's Free = 49 is
reproduced exactly as "Free (stored)".)

### 1.2 Audio evidence only (what paid adds to a mix with no tracklist)

`--no-hints`: pooled Free (today) 50/107; everywhere 50/107 (1807 clips); **gaps 50/107 (1214
clips)**; gaps + unsure 50/107 (1674 clips); likely precision 16/16 for all. Again no policy names a
truth track today's Free does not.

### 1.3 Tracks gained and lost, by name, against Free (today)

* **Every policy, every mix: no truth track gained, none lost** (with the additive merge). What paid
  does add are *listed rows for tracks Free already names elsewhere* (extra plays, precision only):
  gaps adds "KETTAMA - Rok da House" and "Mall Grab - Temors" on Mall Grab (both correct), "EUROCLUB -
  FUNKY SHIT" and "DJ HEARTSTRING - What Music Felt Like In 2007" on Heartstring (both correct), and
  "Simula - Descent" on MPH (not in the truth). Pooled work precision: Free 67/98 → gaps 70/101.
* Against the **stored** Free (the brief's baseline), today's fusion alone gains: Mall Grab — Bear
  Witness, Effy - Get Down, Juice (MG Remix), Madman, 1ofthozedaze; MPH — Overrated (VIP), MJ Cole &
  MPH - Hold On, Jah War; and loses MPH - Nova. **Time-window check** (each gained track's listed row
  against the owner's timed truth, ±30 s): all IN — Bear Witness 2096–2106 s vs truth 1980–2190 s;
  Get Down 2478–2565 vs 2430–2640; Madman 4703–4713 vs 4670–4860; 1ofthozedaze 4881–4891 vs
  4860–5080; Overrated 4224–4266 vs 4200–4266; Hold On 4305–4383 vs 4266–4388; Jah War 4761–4773 vs
  4693–4807; Juice has one row IN (2610–2622 vs 2640–2850) and two later rows OUT (4251 s, 4323 s — a
  later play the truth does not list).

### 1.4 Where the "Deep gains" really came from

The earlier table credited Deep with five Mall Grab tracks. AudD's 50 cached matches on Mall Grab
name: Clouds - Plastyx (12), KETTAMA - Rok da House (11), Mall Grab - Tremors (7), Mall Grab -
Metaphysical (6), CRTB - It Never Ends (2), Flansie - No One Else Will (2), Mall Grab - Say Nothing (2),
Mall Grab - No One Else Will (2), and one each of Klubbheads, DJ Bozz, Mikel Alberdi, Yummy Tunes,
Mall Grab - Juice, Dise. **None of Bear Witness, Madman, 1ofthozedaze or Get Down.** Those rows are
`hint_only` / `hint_supported` rows that fusion:4 builds from the mix's own tracklist hints; the Deep
run showed them because it was fused under fusion:4 while the stored Free result was still fusion:1.

### 1.5 The chosen policy: (b) gaps

(a), (b) and (c) reach the **same recall** on every mix (56/107 with hints, 50/107 without), the same
likely precision, and near-identical work precision; (b) needs **1228** paid clips against 1807 for
(a) and 1454 for (c) (without hints: 1214 vs 1807 vs 1674) — about two thirds of paying everywhere.
The data agrees with the expected winner, so Deep's recipe carries `secondary_priority=("gaps",)`.
Stated plainly: on these three mixes even (b) buys no new correct track; it buys a little precision.
Whether it is worth $2/mix is the owner's call — which is exactly why the price is now asked first.

## 2. The fusion invariant — its fix and its proof

**The loss (plain one-pass fusion, `--plain-fusion`):** every policy loses "Mall Grab & C.R.T.B. -
Juice (MG Remix) (UR)" (pooled 55/107). Mechanism, traced: the free row is the same episode, with the
same evidence, in both fusions — but the paid answer "Mall Grab - Juice" (a later play, in a gap)
pulls the tracklist line "Mall Grab & C.R.T.B - Juice" into *its* work, so the free row's work loses
the name the tracklist gave it and falls back to the raw Shazam label "Mall Grab & CRTB - Juice
(Mixed)", which no longer matches the owner's truth.

**The fix — `id_detector.additive.additive_merge`:** the Deep result is built from two fusions, the free
evidence alone (F) and free + paid (C). A C row no paid observation touched is ignored (F already
decided it). A paid-touched C row that overlaps **no** F row of the same work is an **addition**. One
that overlaps F rows of the same work **confirms** (replaces) them only if it keeps everything the free
evidence gave each of them: listed stays listed, tier and badge never drop, every supporting tracklist
hint and `hint_supported`/`hint_only` flag is kept, and every tracklist/crowd line naming the free
row's work still names its own. Otherwise the F rows stay exactly as F made them and the paid row is
dropped. A free row that was already hidden on free evidence alone is not protected (a confirming paid
row may lift it). The identity record is F's, plus what the kept C rows need.

**Proof on the three mixes:** with the merge, every policy's "lost" list is empty (tables above), and
the item-4 comparison prints "Free-found tracks Deep lost … none" for all three. **In tests:**
`test_paid_evidence_never_strips_a_free_rows_tracklist_name_or_support` reproduces the measured Juice
mechanism on hand-built records (fails when the merge is reverted — R1 below);
`test_a_paid_row_only_confirms_a_free_row_it_keeps_everything_of`,
`test_a_free_row_hidden_on_free_evidence_alone_may_be_lifted_but_is_never_protected`,
`test_no_paid_evidence_leaves_the_free_result_untouched`, and end to end
`test_deep_reuses_the_stored_free_sweep_and_pays_only_for_its_gaps` (the free row A stays listed, the
paid-only row D is added).

## 3. The new flow (items 2 and 3)

**Recipe.** `DEEP_RECIPE`: `primary_engine="shazam"`, `secondary_engine="audd"`,
`secondary_priority=("gaps",)` (the paid policy), primary fraction 0.80 (the free sweep), paid-step
fraction 0.95, `algorithm_version="additive:1,fusion:4"`, `requires=("shazam_sweep","audd_additive")`.
The recipe id changes, so a stored paid-first (`targeting:1`) Deep result is never served as the new
recipe; the e591971 refusion rules are unchanged (a fusion-stale Deep result — and any pre-bundle Deep
result — is still refused and explained, never re-fused, never re-charged). The web "Max accuracy" and
`--recipe deep` both call `get_recipe("deep")`.

**Pipeline (`run_analysis`).** Free pass (reusing a stored Free sweep when one exists: a sealed Free
bundle as before, **and** now a pre-bundle or re-fused Free result whose recognition files are proven
against their recorded checksums) → fuse → plan the paid step (`plan_paid_step`: the gap targets, the
exact clips, how many already have a stored answer) → price gate → reserve (for the uncached clips
only) → paid sweep over the targets → re-fuse → additive merge → publish. A stored paid answer, a
no-match included, is never bought again unless `--refresh` is passed. What is sent to AudD is
unchanged — same clip bytes, windows, sample rate, endpoint, fields and `CLIP_CONFIG_VERSION` — only
fewer clips. Declined, not configured, or refused by the cap: the finished free pass is published as a
**Free** result (stamped as a Free request would stamp it) and nothing is reserved. The money authority
(SQLite, claim/lease/run/cancel fence, ambiguous-means-spent, exactly-once settlement) is untouched;
the gate runs before the reservation and never replaces it.

**Command line** (`idea analyse <mix> --recipe deep`), after the free pass:

```
Deep would check 28 gaps for about $2.01.
The free pass lists nothing for 1:00:09 of this 1:30:02 mix; Deep checks only those gaps: 402 paid clips at density 1, 0 already answered (free), 402 to send.
Exact price: 402 x $0.0050 = $2.01; reservation including 5% headroom: $2.12.
Recipe ceiling: $9.00; configured per-run cap: none; effective cap: $9.00.
Stored scan: free (complete).
Spend about $2.01 of AudD credit on these 402 clips? [y/N]:
```

`--yes` confirms; without a terminal: `cancelled: paid confirmation requires an interactive terminal;
use --yes for a scripted run.` Declining prints `cancelled: paid scan not confirmed; nothing reserved
or spent.` and then `Deep stopped after the free pass: you did not confirm the paid check. Nothing was
reserved or spent. Your Free result is saved: tracklist=…` (exit 130, journal status `cancelled`,
reason `paid_not_confirmed`, $0). When every clip already has an answer: `Nothing to spend: every clip
Deep checks already has a stored paid answer.`

**`idea cost`** — a Free-scanned mix gets the exact figure offline (the lines above, then `Exact for
today's stored free result; a fresh run re-reads its tracklist hints, which can move a gap. Nothing was
fetched or spent.`). The owner's three mixes in `work/` today: Mall Grab **28 gaps, 402 clips, $2.01**;
MPH **35 gaps, 386 clips, $1.93**; Heartstring **26 gaps, 440 clips, $2.20** (verified read-only: every
file's mtime and size in those three folders unchanged). A mix not yet scanned:

```
Deep runs the free pass first and then pays only for the stretches the free result leaves blank, so the real figure is known only after the free pass. The whole mix is the ceiling:
Ceiling at density 1: 600 paid clips, $3.00.
Ceiling at density 2: 300 paid clips, $1.50.
```

**Browser (local).** Choosing Max accuracy runs the free pass; the job then ends with the Free result
open and the offer beside it. Exact words on the job page: heading **"Free pass done — Deep is waiting
for your approval"**, text **"Deep would check 28 gaps for about $2.01. Nothing has been reserved or
spent. Approve to run the paid check, or Skip to keep the Free result. Closing this page spends
nothing."** (plus "N of its M clips already have a stored answer and cost nothing." when some do),
buttons **"Approve $2.01"**, **"Skip"**, **"Open the Free result"**. The home card reads "Free pass done
— Deep is waiting for your approval". Approve (POST `/jobs/<id>/approve`, synchroniser token required)
starts a new Max-accuracy job carrying that approval (price and clip count); it reuses the free pass
just finished and its paid step runs only while its own exact plan is within the approval — otherwise
it stops again with a fresh offer. Its reservation, dispatch fence and settlement are the queue's usual
money authority. Skip (POST `/jobs/<id>/skip`) records "Free result kept — You skipped the paid check.
Nothing was reserved or spent." Closing the tab does nothing and spends nothing. Hosted mode has no
such route and still refuses paid work outright.

Why a follow-up job rather than parking the same job: in local mode a "waiting" job is already
terminal (the queue maps it to `provider_unavailable`), so the existing states offer no resumable
pause; a new job keyed to the approval reuses them all, and a server restart cannot leave a half-
approved run holding the worker.

## 4. The comparison runner (item 4)

`scripts/compare_deep.py`:
* **Staging** copies into a temporary sibling, retries the rename with a bounded backoff (0.5, 1, 2, 4,
  8, 15 s) while the fresh copy is locked, and on persistent failure removes the partial copy and
  stops: `could not move the copied mix into … after 7 attempts (…); the partial copy was removed and
  nothing was queued, reserved or spent. Close anything holding the folder open (a virus scan of the
  fresh copy is the usual cause) and run the same command again.`
* **Selection refusal** kept, now explicit: `scan root belongs to a different mix selection or recipe:
  it was started with <mixes>; name those mixes too (--mix … --mix …) to resume or extend it[; N of its
  job(s) belong to a different recipe or run list; use a new --scan-root for this recipe]. It is
  refused so that no mix can be charged twice.`
* **New Deep vs Free:** `--paid-answers-from <earlier scan root>` copies that root's stored paid answers
  into each staged mix (the source is only read); `--offline` runs under the network guard with an
  empty token and the free engine off, and compares on audio evidence only (no connector can be
  reached). Columns: Free (stored), Free today, Deep, plus the invariant line.

**Headline run** — the owner's three mixes, scan root `%TEMP%\idea-additive` (a fresh copy staged from
`work/`), paid answers copied from `id-detector-deepscan`:

```
dj-heartstring-youtube-set: paid step 440 clip(s) over 26 gap(s); 439 answered from stored answers; 0 new request(s) sent; 1 without a stored answer counted as would-cost, not sent; free requests sent 0.
mall-grab-boiler-room-melbourne-22: paid step 388 clip(s) over 19 gap(s); 388 answered from stored answers; 0 new request(s) sent; 0 without a stored answer counted as would-cost, not sent; free requests sent 0.
mph-youtube-set: paid step 386 clip(s) over 35 gap(s); 386 answered from stored answers; 0 new request(s) sent; 0 without a stored answer counted as would-cost, not sent; free requests sent 0.
Development comparison; work-only matching, not certification.
Mix | Recipe | Work recall | Work precision | Likely precision
--- | --- | --- | --- | ---
dj-heartstring-youtube-set | Free (stored) | 47.6% | 66.7% | 100.0%
dj-heartstring-youtube-set | Free today | 42.9% | 60.0% | 100.0%
dj-heartstring-youtube-set | Deep | 42.9% | 62.5% | 100.0%
mall-grab-boiler-room-melbourne-22 | Free (stored) | 33.3% | 44.4% | 42.9%
mall-grab-boiler-room-melbourne-22 | Free today | 33.3% | 44.4% | 100.0%
mall-grab-boiler-room-melbourne-22 | Deep | 33.3% | 44.4% | 100.0%
mph-youtube-set | Free (stored) | 50.0% | 77.5% | 81.8%
mph-youtube-set | Free today | 53.2% | 82.5% | 100.0%
mph-youtube-set | Deep | 53.2% | 82.5% | 100.0%
Pooled | Free (stored) | 45.8% | 67.1% | 71.4%
Pooled | Free today | 46.7% | 68.5% | 100.0%
Pooled | Deep | 46.7% | 68.9% | 100.0%
Deep found that Free missed (truth-matched names):
dj-heartstring-youtube-set: none
mall-grab-boiler-room-melbourne-22: Effy - Get Down (UR)
mph-youtube-set: MPH - Overrated (VIP); MJ Cole & MPH - Hold On; MPH - My Mind (time approx.)
Free-found tracks Deep lost (the additive invariant; must be none), against today's Free:
dj-heartstring-youtube-set: none
mall-grab-boiler-room-melbourne-22: none
mph-youtube-set: none
Experiment spent so far (including ambiguous requests): $0.000000.
Network attempts refused by the offline guard: 0 (none: no request left this machine).
```

**New requests: 0 AudD, 0 Shazam, 0 network attempts.** (The "Deep found that Free missed" line is
against the stored fusion:1 result; against today's Free, Deep adds nothing — the same finding as §1.)
The 1214 paid clips it answered from the cache match the item-1 audio-only "gaps" count exactly.
Read-only proof: every file under `id-detector-deepscan` and under the three `work/` mix folders has
the same mtime and size before and after (a `find -printf '%p %T@ %s'` listing diffed: identical;
newest mtime in deepscan is still 2026-09-27 19:43, in those `work/` folders 2026-09-10, in
`data/corpus` 2026-09-14). `git status --short -- data work` in the main checkout is empty.

## 5. Two recommendations — written up, not built

**(i) AudD's enterprise endpoint (several results per 12 s chunk, `accurate_offsets`).** Not worth
pursuing for this corpus, and not usable truthfully. Evidence from the cached answers: on the same 16
kHz mono clips AudD matched **139 of 1806** (7.7 %) while Shazam matched **1122** (62 %); Shazam matched
990 clips where AudD said no-match, AudD matched only 7 that Shazam missed. The limit is AudD's
catalogue on this music, not the number of results per clip: a second result per chunk can only help in
the ~139 clips AudD recognises at all. It also needs the whole mix uploaded, which this project gates
behind the ownership attestation (`require_upload_permission`, 348b3c5) — the owner cannot truthfully
give it for other DJs' sets, and I did not touch that gate. Cost if it were ever used on the DJ's *own*
mixes: 1 request per 12 s, i.e. 450 requests ≈ $2.25 for a 90-minute mix at the listed $0.005 (the
enterprise price may differ). Recommendation: do not build.

**(ii) Is the 16 kHz mono clip costing recognition?** Probably not much, but it is unproven. The same
16 kHz bytes let Shazam identify 62 % of clips, so the format is clearly identifiable; AudD's 7.7 % is
consistent with its known underground blind spots (an earlier ACRCloud comparison found the same). A
fair test, without disturbing the cached answers: pick ~100–200 windows where Shazam and the owner's
truth agree on a catalogue track but AudD's 16 kHz answer was no-match; send the same windows cut at
the source rate (44.1 kHz stereo) under a **separate** experimental config version (so its cache keys
never collide with `audd-main-v1`), plus ~20 already-matched windows as a control. Cost ≈ $0.60–$1.10.
Only a large jump would justify changing the product's clip format — which would re-key every cached
answer and mean buying them again.

## 6. Other notes for the reviewer

* **Free recipe output unchanged:** `tests/test_golden_local_free.py` passes; the Free code path is the
  same statements in the same order (the only shared change is `running_free=True`, which the Free
  primary already passed).
* **Retired paid-first pieces:** the Shazam secondary no longer runs in the pipeline. Five end-to-end
  tests of it were removed from `tests/test_phase1b_targeting.py`
  (`test_overlap_allowed_probes_coincide_with_audd_windows[1,2]`, `test_secondary_under_80_percent_is_degraded`,
  `test_stopped_primary_leaves_a_blank_tail_the_secondary_probes`,
  `test_reserve_confirms_a_new_identity_with_two_clips_30_s_apart`,
  `test_reserve_exhausted_lists_the_third_discovery_uncorroborated`); the scheduler module and its unit
  tests stay (the pipeline still uses its `frozen_windows`). A resume of a checkpoint written by the
  paid-first recipe fails closed ("this run was started by the older paid-first Deep recipe and cannot
  be resumed; nothing further was sent") and settles what it spent.
* **Existing tests updated to the new semantics, intended never to weaken a money check (see Fix pass 1: two refusion assertions WERE loosened, `> 30` to `> 0`, and are now exact):** status moves from
  `partial/primary_not_achieved` to `degraded/secondary_not_achieved` when only the paid check falls
  short (the free pass stands); a refused paid run now shows 7 free-pass Shazam requests and a kept
  Free result; crash/cancel tests now strike during the paid step (the free pass is durable first);
  web tests that need the paid step pass the approval (`APPROVED_DEEP`) as the Approve button would.
* **Owner's real library:** the three mixes' paid answers live in `id-detector-deepscan`, not in
  `work/`. Running Deep on them in `work/` today would buy the 1228 gap clips again (~$6.14). Copying
  each mix's `recognise/invocations/live-audd-clip-v1/raw/*.json` from deepscan into the same folder
  under `work/` first makes that run cost $0 (`idea cost` will then say "402 already answered").
* **Known limit, measured as is:** gap targeting uses the free rows' `best_start/best_end`; a
  single-window row has crossed proof bounds and covers nothing, so the paid step also checks around
  such weak rows. The measured costs include this.
* **Pre-existing, not fixed:** the queue worker finds a finished bundle with `pathlib` globbing, which
  misses paths over 260 characters on Windows; a scan root under a deep temp folder therefore
  recorded no bundle id. The comparison was run from a short path.

## 7. Revert proofs

`revert_proofs.py` (kept outside the repo) reverts one fix at a time, runs the tests that guard it,
restores the file byte-for-byte:

```
R1 additive invariant: Deep = plain combined fusion: FAILS as required -> 3 failed
R2 gap targeting: paid step checks the whole mix: FAILS as required -> 2 failed
R3 free-first reuse: Deep never reuses a stored Free sweep: FAILS as required -> 2 failed
R4 proven pre-bundle evidence: only sealed Free bundles are reused: FAILS as required -> 1 failed
R5 stored paid no-match re-bought (paid sweep follows the free refresh states): FAILS as required -> 1 failed
R6 no credential: skip the whole sweep even when answers are stored: FAILS as required -> 1 failed
R7 browser price gate removed (Max accuracy dispatches unasked): FAILS as required -> 2 failed
R8 approval not bounded by the approved clip count: FAILS as required -> 1 failed
R9 CLI price gate not asked (the old pre-free-pass gate removed): FAILS as required -> 5 failed
R10 staging: one rename, no retry: FAILS as required -> 1 failed
R11 staging: the temporary copy is left behind on failure: FAILS as required -> 1 failed
R12 selection refusal back to the old wording: FAILS as required -> 1 failed
R13 reuse proof demands the hint file too (a rewritten tracklist forces a re-sweep): FAILS as required -> 1 failed
R15 a free row hidden on free evidence alone is protected anyway: FAILS as required -> 1 failed
R16 the plan ignores stored paid answers (every clip priced as a request): FAILS as required -> 1 failed
R17 Approve starts a job without the approval it granted: FAILS as required -> 1 failed
R18 no Approve / Skip route in the local app: FAILS as required -> 1 failed
R19 selection refusal function back to the old wording: FAILS as required -> 1 failed
R20 offline guard leaves the AudD token in place: FAILS as required -> 1 failed
R21 a job waiting for approval drops off the home page: FAILS as required -> 1 failed
```

Honesty note: R12 and R20 first came back "STILL PASSES" — the R12 test called the message helper
directly and the R20 test ran with an already-empty token. Both tests were strengthened (R12 now goes
through `execute()`, R20 sets a placeholder token first) and then fail as required, as shown.

## 8. Gates

Suite, in foreground shards covering every collected file (`--collect-only`: `2100/2197 tests
collected (97 deselected)`; the six files not listed below collect nothing by default — all their
tests are `slow`/`live`):

```
s1 tests/test_refusion.py                                   44 passed in 263.15s
s2 service_api, followup_money_resume                       42 passed in 371.90s
s3 phase0a_status, phase0b_audd, phase0b_attempts, phase0a_money, phase1b_breaker_scorer, phase1a_compat
                                                           203 passed in 160.28s
s4 phase1b_fusion, phase0b_config, phase0a_crash_cache, phase0a_security, phase3a_honesty, score_corpus,
   stage4d_profiles, cost_preview, additive_deep, phase1b_targeting
                                                           217 passed in 190.42s
s5 idea_web: coalescing, followup_queue_money, worker, refusion_money, followup_round2, followup_round4
                                                           107 passed in 183.61s
s6 idea_web: followup_round5, followup_round6, followup_round7, followup_review_fixes
                                                            45 passed in 83.03s
s7 idea_web accounts … test_engine_corroboration (20 files) 390 passed in 142.81s
s8 test_fixture_audit … test_stage1_shazam (21 files)       348 passed in 105.86s
s9 test_stage1_wheel … test_truth_review (37 files)         702 passed, 2 skipped, 5 deselected in 143.69s
total: 2098 passed + 2 skipped = 2100 collected
extra: -m "slow and not live" tests/test_stage2b_pipeline.py tests/test_stage4c_generations.py: 14 passed
```

```
$ uv run ruff check .
All checks passed!
$ uv run ruff format --check .
428 files already formatted
$ uv run python scripts/audit_fixtures.py
audited 546 files
fixture audit passed
$ uv run python scripts/check_page_js.py
page JavaScript check passed: 53 inline scripts across 22 page renders, plus the static app.js
```

```
$ git diff --stat
 scripts/compare_deep.py                      |  401 ++++++++--
 src/id_detector/cli.py                       |   67 +-
 src/id_detector/cost.py                      |   97 ++-
 src/id_detector/paid_clip.py                 |   63 +-
 src/id_detector/pipeline.py                  | 1040 +++++++++++---------------
 src/id_detector/present/server.py            |   34 +
 src/id_detector/recipes.py                   |   54 +-
 src/id_detector/service.py                   |    7 +-
 src/id_detector/webapp/jobs.py               |   80 ++
 src/id_detector/webapp/runner.py             |   59 ++
 src/idea_web/application.py                  |   53 +-
 src/idea_web/jobs/local.py                   |   65 ++
 src/idea_web/pages.py                        |    3 +
 tests/fakes/providers.py                     |    6 +
 tests/idea_web/test_followup_review_fixes.py |    4 +-
 tests/idea_web/test_followup_round2.py       |    8 +-
 tests/idea_web/test_followup_round4.py       |    8 +-
 tests/idea_web/test_followup_round5.py       |    5 +-
 tests/idea_web/test_followup_round6.py       |    8 +-
 tests/idea_web/test_followup_round7.py       |    3 +-
 tests/idea_web/test_refusion_money.py        |   10 +-
 tests/idea_web/test_worker.py                |   14 +-
 tests/test_cost_preview.py                   |   36 +-
 tests/test_followup_money_resume.py          |   16 +-
 tests/test_phase0a_crash_cache.py            |   36 +-
 tests/test_phase0a_money.py                  |   63 +-
 tests/test_phase0a_status.py                 |   79 +-
 tests/test_phase0b_attempts.py               |   20 +-
 tests/test_phase0b_audd.py                   |    7 +-
 tests/test_phase0b_config.py                 |   40 +-
 tests/test_phase1a_compat.py                 |   10 +-
 tests/test_phase1b_breaker_scorer.py         |   33 +-
 tests/test_phase1b_targeting.py              |  291 +------
 tests/test_refusion.py                       |    8 +-
 tests/test_service_api.py                    |   52 +-
 35 files changed, 1634 insertions(+), 1146 deletions(-)
$ git status --short
 M scripts/compare_deep.py
 M src/id_detector/cli.py
 M src/id_detector/cost.py
 M src/id_detector/paid_clip.py
 M src/id_detector/pipeline.py
 M src/id_detector/present/server.py
 M src/id_detector/recipes.py
 M src/id_detector/service.py
 M src/id_detector/webapp/jobs.py
 M src/id_detector/webapp/runner.py
 M src/idea_web/application.py
 M src/idea_web/jobs/local.py
 M src/idea_web/pages.py
 M tests/fakes/providers.py
 M tests/idea_web/test_followup_review_fixes.py
 M tests/idea_web/test_followup_round2.py
 M tests/idea_web/test_followup_round4.py
 M tests/idea_web/test_followup_round5.py
 M tests/idea_web/test_followup_round6.py
 M tests/idea_web/test_followup_round7.py
 M tests/idea_web/test_refusion_money.py
 M tests/idea_web/test_worker.py
 M tests/test_cost_preview.py
 M tests/test_followup_money_resume.py
 M tests/test_phase0a_crash_cache.py
 M tests/test_phase0a_money.py
 M tests/test_phase0a_status.py
 M tests/test_phase0b_attempts.py
 M tests/test_phase0b_audd.py
 M tests/test_phase0b_config.py
 M tests/test_phase1a_compat.py
 M tests/test_phase1b_breaker_scorer.py
 M tests/test_phase1b_targeting.py
 M tests/test_refusion.py
 M tests/test_service_api.py
?? docs/reviews/build-local-additive-deep.md
?? scripts/measure_additive.py
?? scripts/offline_guard.py
?? src/id_detector/additive.py
?? tests/test_additive_deep.py
$ git status --short -- data work
(empty)
```

The main checkout's own `git status --short -- data work` is empty as well.


## Fix pass 1 (review `review-additive-round1.md`, verdict FIX_FIRST)

Everything the review confirmed is kept: the Free path and golden are unchanged, the CLI gate
sits after the free plan and before the reservation, Approve/Skip keep POST + synchroniser token +
Origin/Host checks, hosted paid work is refused, the bb57b70 money authority, the AudD cache key and
request are unchanged, the offline guard blocks, and legacy `targeting:1` Deep results are refused.
**New provider requests in this pass: 0** (tests use fakes; the re-measurement read the cache only).

### P1 — browser approval is now one atomic transaction

`LocalJobs.approve_offer()` (`src/idea_web/jobs/local.py`) now runs inside ONE `BEGIN IMMEDIATE`
write: it reads the offer row, inserts the authorised follow-up job (`JobQueue.insert_job`, new, the
same INSERT `enqueue` uses, now callable inside the caller's transaction), and consumes the offer with
a conditional `UPDATE … WHERE offer_decision IS NULL`; if that update does not hit exactly one row the
whole transaction rolls back. The follow-up's job id and run id are derived from the offer's job id
(`approval_job_id`, `approval_run_id` — the durable approval identity), so one offer can authorise
at most one paid run, ever: a retry after a crash, a cancel or an ambiguous first run finds the offer
consumed and creates nothing; even a second insert would hit the primary key. Skip was already a
single conditional UPDATE and now races correctly against it.

Tests (real concurrency: threads, each with its OWN `LocalJobs`/`Database` connection to the same
SQLite file, released together by a barrier):
* `test_concurrent_approvals_of_one_offer_create_one_paid_job_and_one_paid_run` — 6 contenders × 6
  offers: exactly one winner per offer, one follow-up job per offer, and running them yields 7 AudD
  calls and 1 reservation for the offer that was run.
* `test_a_concurrent_approve_and_skip_never_leave_a_paid_job_behind_a_skip` — 12 offers, Approve
  racing Skip: exactly one answer wins each time; a skipped offer has no paid job at all, not even a
  cancelled one.
* `test_a_crash_after_the_paid_job_is_written_leaves_nothing_and_a_retry_authorises_once` — a SQLite
  trigger aborts right after the follow-up is written (before the offer is consumed): nothing
  survives; the retry authorises once; a further retry authorises nothing; the run makes 7 AudD calls
  and 1 reservation — no re-purchase.

### P1 — paid fusion can no longer rename a Free-listed row

`_preserves()` (`src/id_detector/additive.py`) now requires, for a listed free row, that the
confirming paid row publishes exactly the same presentation — the page/export artist and title
(`_candidate_label`) and the work label the exports and scorer read (`_label_for_candidate`),
compared exactly (new `_Graph.presented`). If Shazam and AudD agree it is the same recording but
disagree about its metadata, the free row is kept exactly as free fusion made it, under its free
label, and the paid row is dropped (`kept_free`). The invariant now reads: paid evidence may confirm
or add a row, never remove, demote or rename one Free lists.

Test: `test_a_paid_engine_naming_the_same_recording_differently_never_renames_a_free_row`, three cases
where one candidate holds both provider nodes but AudD names it differently (artist and title;
artist only; title only): before the fix the merge reported `confirmed=1` and published the paid name
(the reviewer's "Zulu — Free" → "Alpha — Paid"); now the free row and its "Zulu — Free" label are what
the tracklist shows, with `kept_free=1`.

**Re-measurement on the owner's three mixes** (cache only, offline guard, `measure_additive.py`,
same command as §1): the with-hints table is identical to §1.1 line for line (pooled Free today
56/107; everywhere 56/107 with 1807 clips; gaps 56/107 with 1228; gaps+unsure 56/107 with 1454;
likely precision 16/17 throughout), audio-only is identical in every figure (50/107; 1807 / 1214 /
1674 clips), **no Free track lost** under any policy on any mix (`LOST` lines: 0 and 0), guard
count 0. The only change is internal: one audio-only Heartstring confirmation whose AudD label
differed from the free label is now kept as the free row (`gaps: confirmed 3→2, kept_free 0→1`).
Deepscan and the three `work/` folders: every file's mtime and size unchanged (diffed listings).

### Follow-ups

* **Weakened assertions restored exactly** — `tests/test_refusion.py` now asserts
  `first_audd.calls == 5` and `(paid_targets, paid_target_spans, paid_cache_hits, paid_requests) ==
  (5, 2, 0, 5)` for the fixture's gap-only first purchase. Correction to my first report: its wording
  said tests were never weakened; these two were (`> 30` became `> 0`), and they are exact now.
* **Golden compared as bytes** — `test_local_free_run_reproduces_the_golden_bytes` takes only the run
  identity and timestamps from the committed golden and requires every other byte to match exactly
  (it catches value-type changes, e.g. `False` → `0`, that the semantic comparison cannot see).
* **No reservation for clips that cannot be sent** — with no credential and some stored answers, the
  paid step now reserves nothing at all and only reads the cache (a resumed run keeps the reservation
  it made). Test: `test_without_a_credential_a_partly_cached_deep_run_reserves_nothing`.
* **Offline guard counts per block** — `no_network()` now yields that block's own list (the
  process-wide `BLOCKED` is still kept); `measure_additive.py` and `compare_deep.py` report the
  block's own count. Test: `test_each_offline_block_counts_only_its_own_refused_attempts`.

### Revert proofs (fix pass 1)

```
F1 approval back to read / enqueue / then consume (three separate steps): FAILS as required -> 3 failed
    FAILED tests/test_additive_deep.py::test_concurrent_approvals_of_one_offer_create_one_paid_job_and_one_paid_run
    FAILED tests/test_additive_deep.py::test_a_concurrent_approve_and_skip_never_leave_a_paid_job_behind_a_skip
    FAILED tests/test_additive_deep.py::test_a_crash_after_the_paid_job_is_written_leaves_nothing_and_a_retry_authorises_once
F2 a confirming paid row may publish its own label: FAILS as required -> 3 failed (all three metadata cases)
F3 an internal reservation made for clips that cannot be sent: FAILS as required -> 1 failed
    FAILED tests/test_additive_deep.py::test_without_a_credential_a_partly_cached_deep_run_reserves_nothing
F4 offline guard yields the process-wide list again: FAILS as required -> 1 failed
    FAILED tests/test_additive_deep.py::test_each_offline_block_counts_only_its_own_refused_attempts
F5 exact refusion counts catch a paid step that is no longer gap-only: FAILS as required -> 7 failed
    FAILED tests/test_refusion.py::test_a_stale_deep_result_never_becomes_a_new_paid_analysis[missing|mismatched|intact]
    FAILED tests/test_refusion.py::test_a_legacy_stale_deep_result_is_refused_before_any_paid_action[4 cases]
F6 golden bytes catch a type-only change the semantic check cannot see: FAILS as required -> 1 failed, 4 passed
    FAILED tests/test_golden_local_free.py::test_local_free_run_reproduces_the_golden_bytes
```

(F6 reverts nothing in the product: it injects `engine_corroborated` as `int` instead of `bool` into
the Free export; the semantic golden test still passes, the new byte test fails. A first attempt at
F6 with a float `duration_ms` failed both tests and was replaced by this sharper probe.)

### Gates (fix pass 1)

`--collect-only`: `2109/2206 tests collected (97 deselected)`; the shard file lists cover every
collected file (diffed: identical).

```
s1 tests/test_refusion.py                                  44 passed in 242.25s
s2 service_api, followup_money_resume                      42 passed in 371.79s
s3 phase0a_status … phase1a_compat                        203 passed in 156.13s
s4 phase1b_fusion … additive_deep, phase1b_targeting      225 passed in 194.35s
s5 idea_web coalescing … followup_round4                  107 passed in 160.11s
s6 idea_web followup_round5 … followup_review_fixes        45 passed in 71.92s
s7 idea_web accounts … test_engine_corroboration          390 passed in 125.75s
s8 test_fixture_audit … test_stage1_shazam                349 passed in 99.11s
s9 test_stage1_wheel … test_truth_review                  702 passed, 2 skipped, 5 deselected in 129.60s
total: 2107 passed + 2 skipped = 2109 collected
$ uv run ruff check .
All checks passed!
$ uv run ruff format --check .
429 files already formatted
$ uv run python scripts/audit_fixtures.py
audited 547 files
fixture audit passed
$ uv run python scripts/check_page_js.py
page JavaScript check passed: 53 inline scripts across 22 page renders, plus the static app.js
```

```
 scripts/compare_deep.py                      |  400 ++++++++--
 src/id_detector/cli.py                       |   67 +-
 src/id_detector/cost.py                      |   97 ++-
 src/id_detector/paid_clip.py                 |   63 +-
 src/id_detector/pipeline.py                  | 1043 +++++++++++---------------
 src/id_detector/present/server.py            |   34 +
 src/id_detector/recipes.py                   |   54 +-
 src/id_detector/service.py                   |    7 +-
 src/id_detector/webapp/jobs.py               |   80 ++
 src/id_detector/webapp/runner.py             |   59 ++
 src/idea_web/application.py                  |   53 +-
 src/idea_web/jobs/local.py                   |  126 ++++
 src/idea_web/jobs/worker.py                  |   67 +-
 src/idea_web/pages.py                        |    3 +
 tests/fakes/providers.py                     |    6 +
 tests/idea_web/test_followup_review_fixes.py |    4 +-
 tests/idea_web/test_followup_round2.py       |    8 +-
 tests/idea_web/test_followup_round4.py       |    8 +-
 tests/idea_web/test_followup_round5.py       |    5 +-
 tests/idea_web/test_followup_round6.py       |    8 +-
 tests/idea_web/test_followup_round7.py       |    3 +-
 tests/idea_web/test_refusion_money.py        |   10 +-
 tests/idea_web/test_worker.py                |   14 +-
 tests/test_cost_preview.py                   |   36 +-
 tests/test_followup_money_resume.py          |   16 +-
 tests/test_golden_local_free.py              |   31 +
 tests/test_phase0a_crash_cache.py            |   36 +-
 tests/test_phase0a_money.py                  |   63 +-
 tests/test_phase0a_status.py                 |   79 +-
 tests/test_phase0b_attempts.py               |   20 +-
 tests/test_phase0b_audd.py                   |    7 +-
 tests/test_phase0b_config.py                 |   40 +-
 tests/test_phase1a_compat.py                 |   10 +-
 tests/test_phase1b_breaker_scorer.py         |   33 +-
 tests/test_phase1b_targeting.py              |  291 +------
 tests/test_refusion.py                       |   30 +-
 tests/test_service_api.py                    |   52 +-
 37 files changed, 1798 insertions(+), 1165 deletions(-)
$ git status --short
 M scripts/compare_deep.py
 M src/id_detector/cli.py
 M src/id_detector/cost.py
 M src/id_detector/paid_clip.py
 M src/id_detector/pipeline.py
 M src/id_detector/present/server.py
 M src/id_detector/recipes.py
 M src/id_detector/service.py
 M src/id_detector/webapp/jobs.py
 M src/id_detector/webapp/runner.py
 M src/idea_web/application.py
 M src/idea_web/jobs/local.py
 M src/idea_web/jobs/worker.py
 M src/idea_web/pages.py
 M tests/fakes/providers.py
 M tests/idea_web/test_followup_review_fixes.py
 M tests/idea_web/test_followup_round2.py
 M tests/idea_web/test_followup_round4.py
 M tests/idea_web/test_followup_round5.py
 M tests/idea_web/test_followup_round6.py
 M tests/idea_web/test_followup_round7.py
 M tests/idea_web/test_refusion_money.py
 M tests/idea_web/test_worker.py
 M tests/test_cost_preview.py
 M tests/test_followup_money_resume.py
 M tests/test_golden_local_free.py
 M tests/test_phase0a_crash_cache.py
 M tests/test_phase0a_money.py
 M tests/test_phase0a_status.py
 M tests/test_phase0b_attempts.py
 M tests/test_phase0b_audd.py
 M tests/test_phase0b_config.py
 M tests/test_phase1a_compat.py
 M tests/test_phase1b_breaker_scorer.py
 M tests/test_phase1b_targeting.py
 M tests/test_refusion.py
 M tests/test_service_api.py
?? docs/reviews/build-local-additive-deep.md
?? scripts/measure_additive.py
?? scripts/offline_guard.py
?? src/id_detector/additive.py
?? tests/test_additive_deep.py
$ git status --short -- data work
(empty)
```
