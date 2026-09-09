# Build 0a-ii — Recipes, pricing, reservation, admission, settlement

The cycle was started by a Codex run that was interrupted, then finished in this pass. Every entry
below is marked **[codex]** (already present in the working tree when this pass began) or **[added]**
(written or changed in this pass). Nothing from the interrupted run was discarded.

## File-by-file changes

- `pricing.toml` — **[codex]** the single pricing authority at the repository root, carrying every
  §3.3 field: `pricing_version`, `audd_usd_e6_per_request = 5000`, `bill_on_throttle = false`, the
  free/pro/pack tables (including `pro.deep_minutes_per_month = 90` and
  `pack.small = {gbp = 6, minutes = 55}`), `usd_cap_account_month_e2`, `usd_cap_global_day_e2`,
  `cache_hits_per_day`, `hints_max_age_days`, `alias_revalidate_days`,
  `serve_free_from_deep = false`, `compat_version = 1`.
- `src/id_detector/recipes.py` — **[codex]** frozen `free` and `deep` recipes with every §2.3.1
  field, immutable mappings, `recipe_id = sha256(canonical JSON of every field)`,
  `algorithm_version = "targeting:0,fusion:1"` for Deep and `"fusion:1"` for Free,
  `max_usd_e2` 0 / 900, `requires`, and `get_recipe(name, primary_density=1|2)` deriving the
  density-2 Deep variant (a distinct `recipe_id`).
- `src/id_detector/pricing.py` — **[codex]** strict typed TOML loader that rejects unknown fields and
  wrong types and exposes the optional `max_usd_e2`. **[added]** `DEFAULT_PRICING_PATH`, resolved
  from the package rather than the process working directory, and `load_pricing(path=None)` using it;
  before this, `AppConfig.load()` looked for `./pricing.toml`, so `idea config init` and
  `present/refresh.py` would have failed or silently lost the price when run from any other
  directory.
- `src/id_detector/money.py` — **[codex]** exact-integer `reserve_usd` (`planned x price_e6 x 1.05`,
  `ceil` to e6, `usd_e2_reserved = ceil(usd_e6_reserved / 10^4)`), `BudgetExhausted` above the
  effective cap, the thread-safe `UsdAdmitter` (admit one unit before a dispatch, `ReservationExhausted`
  when a whole unit does not fit, zero-cost outcomes refund their unit), the billable/zero-cost outcome
  sets of §2.3.2 (`auth_error` and `quota_error` cost 0), and settlement. **[added]** settlement now
  charges a dispatched-but-unresolved unit as spent (§2.3.3 treats it as ambiguous) instead of raising,
  and is idempotent — previously a run that crashed with a unit in flight would have raised inside its
  own failure journal and lost both the original exception and the money record.
- `src/id_detector/providers/base.py` — **[codex]** `AppConfig` gained `pricing_version`,
  `audd_usd_e6_per_request`, `bill_on_throttle`, `max_usd_e2` and `deep_primary_density`, the last
  parsed and validated from a new `[deep]` table. **[added]** `pricing_path=None` now means "the
  repository authority" instead of "./pricing.toml", and the `if pricing else <default>` ladders are
  gone: money fields always come from the authority.
- `src/id_detector/providers/audd.py` — **[codex]** split clip transport failures so a pre-receipt
  connect/pool failure is `ProviderUnavailable` (zero cost) while a lost response after dispatch stays
  `AmbiguousProviderOutcome` (billable).
- `src/id_detector/paid_clip.py` — **[codex]** Deep primary now sweeps the frozen generation-0 window
  set at the recipe density, admits one unit immediately before each adapter dispatch, resolves every
  outcome (including error bodies classified into `auth_error`/`quota_error`/`http_429`/`http_503`/
  `http_5xx`/`malformed`), stops the sweep on `ReservationExhausted` and reports
  `reservation_exhausted`. **[added]** `paid_failures` counts only windows actually attempted, so a
  stopped sweep no longer reports its un-dispatched windows as failures.
- `src/id_detector/cli.py` — **[codex]** `--recipe free|deep` (defaulting to Deep only for the legacy
  `max_accuracy` profile), the pre-I/O reservation from the frozen window count with
  `budget_exhausted` / exit 4, the `partial` + `reason=reservation_exhausted` terminal path, the money
  and recipe journal fields on every terminal status, and the retirement of `--max-paid-clips`.
  **[added]** the Free recipe now strips `PAID_CLIP_ENGINES` as well as `PAID_FILE_SCANNERS` (the
  zero-dollar cap no longer depends on `audd` happening to appear in both lists); `_settle_money()` /
  `_money_journal_fields()` were split so that settlement happens once and `costs.usd_e2` is by
  construction the settled `usd_e2_spent`; the `budget_exhausted` entry goes through the same helper
  instead of repeating four keyword arguments; and `_load_app_config` uses the packaged pricing
  authority rather than `Path.cwd() / "pricing.toml"`.
- `src/id_detector/config_template.py`, `idea.example.toml` — **[codex]** documented and rendered the
  `[deep] primary_density` setting; frozen profiles untouched.
- `src/id_detector/contracts.py`, `src/id_detector/journal.py`,
  `docs/schemas/invocation_journal_entry.schema.json`,
  `tests/golden/invocation_journal_entry.json` — **[codex]** the invocation journal contract gained
  `reason`, `usd_e6_reserved`, `usd_e6_spent`, `usd_e2_reserved`, `usd_e2_spent`,
  `requested_recipe_id`, `algorithm_version`, `pricing_version`, `audd_usd_e6_per_request` and the
  `partial` / `budget_exhausted` statuses; the schema and golden were regenerated.
- `src/id_detector/webapp/runner.py` — **[codex]** carried the pricing and density fields across the
  profile-derived web configuration. **[added]** uses the packaged pricing authority.
- `tests/fakes/providers.py` — **[codex]** `FakeAudD` invokes `on_attempt` before it records a call
  (so a refused admission is not a provider attempt in the fake either) and exposes the dispatched
  paths for density assertions.
- `tests/fakes/scripts/all-http-429.json`, `tests/fakes/scripts/money-refunds.json` — **[codex]** the
  throttle-storm and mixed-outcome scripts.

No dependency was added; `pyproject.toml` and `uv.lock` are unchanged. `docs/PLAN-v2.md`, `profiles/`
and `data/corpus/` were not touched.

## Tests added

`tests/test_phase0a_money.py` now holds 18 tests. Fourteen came from the interrupted run: recipe
fields and canonical-JSON hashing (including density-2 producing a different `recipe_id` and the
mappings being immutable); the pricing authority and `AppConfig` loader; the seven-window reservation
(36,750 e6 / 4 cents) and its refusal above a cap; the outcome-cost sets plus a concurrent 429 storm
in which the eighth dispatch is denied before any provider call and the seven resolved units are all
refunded; a Deep success journalling price, recipe id, reservation and settlement; density 2
dispatching the even frozen windows; the Free recipe making zero paid calls and journalling zero
money; reservation over the effective cap (`max_usd_e2 = 0` and `= 3`) exiting 4 with zero AudD and
zero Shazam attempts and no page written; mixed zero-cost/billable outcomes settling at 10,000 e6;
AudD transport classification; admission stopping a sweep into `partial/reservation_exhausted`; and
`--recipe deep` / the retired `--max-paid-clips`.

Four were added in this pass:

- `test_pricing_authority_is_found_from_any_working_directory` — `chdir` away from the repository and
  assert `load_pricing()` and `AppConfig.load(None)` still return the authority's price.
- `test_pricing_loader_refuses_unknown_or_malformed_fields` — an unknown top-level key, a quoted
  `audd_usd_e6_per_request`, and a missing `pro.deep_minutes_per_month` each raise.
- `test_throttled_run_refunds_every_unit_and_bills_nothing` — the 429 storm end to end through
  `_analyse`: seven admitted dispatches, every unit refunded, `usd_e6_reserved = 36750`,
  `usd_e6_spent = 0`, `costs.usd_e2 = 0`, no cached body, no silent downgrade to Shazam, exit 3.
- `test_settlement_charges_dispatched_units_that_never_resolved_and_is_idempotent` — the ambiguous
  in-flight unit settles as spent and a second `settle()` returns the same figures.

Existing tests were extended with `--recipe free` CLI selection and with `paid_requests` /
`paid_failures` assertions on the stopped sweep.

## Required command outputs

### 1. `uv run pytest -q`

```text
........................................................................ [ 11%]
........................................................................ [ 23%]
........................................................................ [ 35%]
........................................................................ [ 47%]
........................................................................ [ 58%]
........................................................................ [ 70%]
........................................................................ [ 82%]
........................................................................ [ 94%]
....................................                                     [100%]
============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
612 passed, 93 deselected, 1 warning in 91.85s (0:01:31)
```

### 2. `uv run ruff check .`

```text
All checks passed!
```

### 3. `uv run ruff format --check .`

```text
216 files already formatted
```

(`uv run ruff format .` was run first; it reformatted only files this pass had edited.)

### 4. `uv run python scripts/audit_fixtures.py`

```text
audited 371 files
fixture audit passed
```

### 5. Phase gate — `uv run pytest tests/test_phase0a_money.py -q`

```text
..................                                                       [100%]
============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
18 passed, 1 warning in 15.20s
```

### 6. `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_serve.ps1`

```text
smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'
```

### 7. `git status --short`

```text
 M docs/schemas/invocation_journal_entry.schema.json
 M idea.example.toml
 M src/id_detector/cli.py
 M src/id_detector/config_template.py
 M src/id_detector/contracts.py
 M src/id_detector/journal.py
 M src/id_detector/paid_clip.py
 M src/id_detector/providers/audd.py
 M src/id_detector/providers/base.py
 M src/id_detector/webapp/runner.py
 M tests/fakes/providers.py
 M tests/golden/invocation_journal_entry.json
?? docs/reviews/build-0a-ii.md
?? pricing.toml
?? src/id_detector/money.py
?? src/id_detector/pricing.py
?? src/id_detector/recipes.py
?? tests/fakes/scripts/all-http-429.json
?? tests/fakes/scripts/money-refunds.json
?? tests/test_phase0a_money.py
```

### 8. `git diff --stat`

```text
warning: in the working copy of 'idea.example.toml', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'src/id_detector/contracts.py', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'src/id_detector/providers/audd.py', CRLF will be replaced by LF the next time Git touches it
 docs/schemas/invocation_journal_entry.schema.json |   2 +-
 idea.example.toml                                 |   5 +
 src/id_detector/cli.py                            | 149 ++++++++++++++++++----
 src/id_detector/config_template.py                |   8 ++
 src/id_detector/contracts.py                      |  19 ++-
 src/id_detector/journal.py                        |  19 +++
 src/id_detector/paid_clip.py                      | 127 +++++++++++++++---
 src/id_detector/providers/audd.py                 |   6 +-
 src/id_detector/providers/base.py                 |  37 +++++-
 src/id_detector/webapp/runner.py                  |   7 +-
 tests/fakes/providers.py                          |   6 +-
 tests/golden/invocation_journal_entry.json        |   2 +-
 12 files changed, 333 insertions(+), 54 deletions(-)
```

### 9. Local-mode end-to-end check (not a required gate)

The 0a-iv gate's money assertions already hold through the real CLI:

```text
uv run idea analyse tests/fixtures/audio/tone-60s.wav --recipe deep --fake-providers audd,shazam --work-root <tmp>
uv run python scripts/assert_journal.py --work-root <tmp> --expect status=succeeded algorithm_version=targeting:0,fusion:1 usd_e6_reserved=36750 usd_e6_spent=35000 usd_e2_spent=4 usd_e2_reserved=4
journal assertion passed at ...\invocations.jsonl:1
```

`--recipe free` on the same fixture journals `algorithm_version=fusion:1` and zero money. (`status`
is still `succeeded`; the `complete|degraded|partial` matrix is 0a-iv's deliverable.)

## Plan ambiguities resolved

- **`pricing_version` value.** The plan requires the field but names no value; the first authority is
  `v1`.
- **Fractional reservations.** `planned x price x 1.05` is rounded **up** to the next micro-dollar,
  the conservative reading. The required seven-window case is exact either way (36,750 e6 / 4 cents).
- **Where `pricing.toml` lives at runtime.** §3.3 says "repo root". Resolution is now
  package-relative, so a run started from any directory prices identically; an explicitly passed path
  must exist and raises if it does not.
- **The gate's "429-storm -> `partial/reservation_exhausted`".** Refunds are immediate, so a
  *sequential* stream of `http_429` returns each unit before the next admission and can never exhaust
  the reservation — with the concurrency and retries of 0b-i it will. The gate is therefore covered in
  three parts: the concurrent storm proves the admitter denies the dispatch that exceeds the
  reservation before any provider call and refunds all seven zero-cost units; an end-to-end storm
  proves such a run bills nothing; and an end-to-end run with an undersized reservation proves the
  sweep stops and journals `partial` / `reason=reservation_exhausted`.
- **What `budget_exhausted` journals.** Nothing was reserved (the reservation was refused before any
  I/O), so the money fields are zero and `reason=reservation_exceeds_cap` carries the cause.
- **A dispatched attempt with no outcome.** §2.3.3 calls it ambiguous, so settlement charges it
  rather than refunding it.

## Not done, and why

- **Retries re-admitting.** The retry policy is recipe data in this cycle; the AudD retry loop,
  concurrency 4 and the token bucket are 0b-i. `UsdAdmitter` already admits per dispatch, so a retry
  will re-admit for free when that loop lands.
- **The rest of the status matrix.** `complete`, `degraded`, `achieved`, `--allow-degrade`,
  `provider_unavailable_midrun` and the provisional `targeting:0` Shazam secondary are 0a-iv; this
  cycle adds only the `budget_exhausted` and `partial/reservation_exhausted` surfaces its gate needs,
  and terminal success is still journalled as `succeeded`.
- **Hosted ceilings.** `account_month_remaining` and `global_day_remaining` are hosted-only (4d-iv);
  the local effective cap is `min(recipe.max_usd_e2, AppConfig.max_usd_e2 or infinity)`.
- **Durable attempt events.** `prepared/dispatched/resolved` in `recognise/attempts.jsonl` is 0b-iii;
  admission is in-process, as the cycle specifies ("an in-process `usd_admitter`").
- **No commit, branch or push**, per the build instructions.

## Review + fix pass

Adversarial review of the uncommitted 0a-ii tree against `docs/PLAN-v2.md` §2.3.1–§2.3.3, §2.3.5,
§3.3, §3.4, §3.5, §6.1 and the cycle's own scope/gate. Every gate below was re-run from scratch,
not trusted from the build report.

### Live-spend disclosure

`cli.py:131` loads the repository `.env` into the environment, so `AUDD_API_TOKEN` is live for every
CLI run on this machine. While reproducing the Deep path end to end I ran
`uv run idea analyse tests/fixtures/audio/tone-60s.wav --recipe deep` (no fakes). It made **7 real
AudD requests, 4 of them billable — `usd_e6_spent = 20000`, about $0.02**. That was the review's only
paid call; every other check ran on the fakes or with a blocked token. It also demonstrated P0-1
below with real money.

### Findings

**P0-1 — a legacy invocation silently started spending.** `cli.py:1219` (pre-fix) mapped
`--profile max_accuracy` to the Deep recipe whenever no `--recipe` was given, and `cli.py:436-441`
forces `audd` into `enabled_engines` for Deep. Before this cycle, `paid_first` additionally required
`audd` in `enabled_engines`, i.e. an explicit `--engine audd`; a bare `--profile max_accuracy` (or
`default_profile = "max_accuracy"` in `idea.toml`) spent nothing. After the cycle the same command
reserves 36 750 e6 and bills AudD for every frozen window — on this machine, with a real token in
`.env`, silently. The plan asks for `--recipe free|deep` as the selector; it does not ask a profile
to start billing.
*Fixed:* `cli.py` now derives the default recipe from the pre-cycle predicate — Deep only when the
profile is `max_accuracy` **and** a paid clip engine is already enabled. `--recipe deep` remains the
explicit, always-honoured opt-in, and the web runner is untouched (it passes `primary_engine="audd"`
for `max_accuracy`, so it still resolves to Deep).
*Test:* `test_legacy_max_accuracy_profile_never_starts_paid_work_without_an_explicit_recipe`.
*Verified live:* `--profile max_accuracy` with a blocked token now journals
`usd_e6_reserved=0 usd_e6_spent=0 algorithm_version=fusion:1`.

**P1-1 — the pricing authority is unreachable in an installed build.** `pricing.py:12` (pre-fix)
resolved `DEFAULT_PRICING_PATH` as `parents[2]/pricing.toml`, which exists only in a source
checkout, while `providers/base.py:139` made `AppConfig.load` load pricing *unconditionally*. The
wheel packages only `src/id_detector` (`pyproject.toml`), so a non-editable install would fail every
command with `invalid config idea.toml: [Errno 2]`. Before this cycle `AppConfig.load` needed no
such file.
*Fixed:* `_resolve_pricing_path()` resolves checkout-first, then `id_detector/resources/pricing.toml`,
mirroring how `profiles.py` resolves frozen profiles; `pyproject.toml` force-includes the repository
file into the wheel at build time, so there is no second committed copy that could drift.
*Tests:* `test_pricing_authority_falls_back_to_the_packaged_copy_without_a_checkout`, plus an
`id_detector/resources/pricing.toml` assertion in `tests/test_stage1_wheel.py`.

**P1-2 — the Free recipe still entered the whole-file scanner call site.** `cli.py:640` gated the
paid whole-file scan on `if enabled_engines and ...`, but the Free recipe strips only *paid* names
and leaves `("shazam",)`, so the branch was taken: it reported "cross-checking with paid engines"
and timed a `scan_ms` stage on a run whose cap forbids paid calls. Only `run_paid_scanners`' own
`PAID_FILE_SCANNERS` filter kept it a no-op, so `max_usd_e2 = 0` was safe by luck of an inner filter
rather than by the outer gate.
*Fixed:* the gate is now `set(enabled_engines) & set(PAID_FILE_SCANNERS) and not paid_first`.
*Test:* `test_free_recipe_reaches_no_paid_call_path_even_with_consent_and_engines_open` sentinels
**both** `run_paid_scanners` and `run_paid_clip_recognition` to raise, with upload consent open,
`cli_confirmation=True` and `enabled_engines=("shazam","audd","acrcloud")`, and asserts the Free run
still completes with zero AudD calls and zero money.

**P1-3 — no regression test for E-S3**, one of the two register items this cycle claims. Nothing
pinned "the frozen profiles' `budget.max_usd_e2 = 0` is never read by the clip path", so a future
`profile_app_config` change mapping it would silently turn every Deep run into `budget_exhausted`.
*Fixed (test only):* `test_frozen_profile_usd_budget_never_caps_a_deep_run` asserts
`frozen.budget.max_usd_e2 == 0`, `profile_app_config(frozen).max_usd_e2 is None`, and that a Deep run
on that config settles at 35 000 e6 rather than exiting 4.

**P1-4 — an explicit `--engine audd` is silently discarded by the Free recipe** (`cli.py:436-440`).
The user asked for a paid engine and got a free scan with no signal at all; before the cycle the same
flag ran the supplemental clip pass.
*Fixed:* a one-line stderr notice ("--engine is ignored by the free recipe (max_usd_e2 = 0); pass
--recipe deep to run a paid scan"). Behaviour is unchanged — the cap still wins.
*Test:* the third leg of
`test_legacy_max_accuracy_profile_never_starts_paid_work_without_an_explicit_recipe`.

**P2-1 (fixed) — `_protocol_outcome` classified money by substring.** `"503" in message` and
`any(str(code) in message for code in range(500, 600))` would refund a *billable* unit for any
protocol message that merely contains those digits (a byte count, a clip offset, a future code).
Only "AudD HTTP nnn" reaches it today, so no live mis-billing, but it is money classification by
accident. It now parses the code with `_HTTP_STATUS`;
`test_protocol_outcome_parses_the_status_code_instead_of_substring_matching` pins the old failure
("AudD clip 5031-byte body was truncated" must stay `malformed`).

**P2-2 (fixed) — `timeout_pre` was unreachable.** `paid_clip.py` chose it with
`"timeout" in str(exc)`, but the adapter's message was "AudD clip connection failed before provider
receipt" and the fake's is "AudD timed out before receiving a response" — neither matches, so every
pre-receipt failure was recorded as `connect_error`. Cost-neutral today (both are zero-cost), but
0b-i's retry policy and 0b-iii's attempt journal must tell them apart. `audd.py` now raises a
distinct "AudD clip timeout before provider receipt" for `ConnectTimeout`/`PoolTimeout` and
`paid_clip.py` matches both spellings; the transport test is parametrised over `ConnectError` /
`ConnectTimeout` / `ReadTimeout` and asserts the wording.

**P2-3 (not fixed — 0b-i) — `reservation_exhausted` is unreachable through the real CLI.** The
reservation is 105 % of `planned`, and `_windows_in_targets` can only ever return a subset of the
frozen windows the reservation was computed from, so `floor(reserved / unit) >= dispatches` always
holds; only retries or concurrency (0b-i) can exhaust it. The builder documented this and covers the
gate in three parts (concurrent admission storm, end-to-end 429 storm, monkeypatched undersized
reservation). Agreed — forcing reachability now would mean faking the arithmetic.

**P2-4 (not fixed — 0a-iv) — `auth_error`/`quota_error` do not stop the primary.** §2.3.3 calls them
terminal-provider outcomes that stop the sweep immediately; `run_paid_clip_recognition` continues
over the remaining windows (`money-refunds.json` proves it keeps going after both). No money leaks
(both cost zero units), and the `partial` / `provider_unavailable_midrun` surface is 0a-iv's
deliverable.

**P2-5 (not fixed — 0a-iv) — `provider_unavailable` can journal a non-zero `usd_e2_spent`.**
`cli.py:600` takes that branch whenever the sweep resolved no observation, which includes an
all-`http_5xx` sweep — billable. §2.3.5 says "nothing spent" for that row. The journal is honest
about it (`costs.usd_e2` is the settled figure, not a hard-coded zero); the status matrix is 0a-iv.

**P2-6 (not fixed) — the pricing authority is mirrored as dataclass defaults** in
`providers/base.py:108-109` and `journal.py:76-77` (`audd_usd_e6_per_request = 5_000`,
`pricing_version = "v1"`, `requested_recipe_id = FREE_RECIPE.recipe_id`). No production path reads
them — every `AppConfig` is built through `load()`/`replace()` and all five `timer.entry()` call sites
pass loaded values — but they would silently go stale against `pricing.toml`. Removing the defaults
would touch `tests/test_stage1_privacy.py`, so it is left as a note.

**P2-7 (not fixed) — the reservation can over-count.** `_windows_in_targets` de-duplicates windows by
`wav_sha256` while `cli.py`'s `planned` counts every frozen generation-zero window, so a mix with
repeated clip content reserves more than it can dispatch. Safe direction (never under-reserves), and
`planned = ceil(windows / density)` is the plan's literal formula.

**P2-8 (not fixed) — the supplemental clip lever is now dead.** `want_clips` (`cli.py:745`) requires
`not paid_first`, Deep always sets `paid_first`, and Free strips the paid engines, so the
`run_paid_clip_recognition` call at `cli.py:856` — the one path that would dispatch **without** an
admitter — is unreachable. Good for money safety; its removal is scheduled (0a-iv / M2 7f). Same for
`DEFAULT_MAX_CLIPS`, now only a legacy selection ceiling. The stopped-sweep log line
(`f"{len(selected) - requests} cached"`) also miscounts un-dispatched windows as cached; cosmetic.

**P2-9 (not fixed) — `bill_on_throttle` is loaded, threaded onto `AppConfig` and never read.** It is
a §3.3-required field recording the L1 assumption, so it stays.

### Confirmed correct (checked, no finding)

- **Admission before dispatch.** `AudDAdapter.recognize_clip` awaits `on_attempt()` before it
  constructs the client and posts; `FakeAudD` was corrected to mirror that, and the concurrent storm
  asserts `fake.calls == 0` while seven units are admitted and the eighth is denied.
- **Reservation arithmetic.** `(planned * unit * 105 + 99) // 100` then `ceil_e2`: 7 x 5 000 ->
  36 750 e6 / 4 cents (exact, plan gate); density 2 -> 21 000 e6; the cap comparison is on the e2
  figure against `min(recipe, config)`.
- **Refunds for zero-cost outcomes.** `money-refunds.json` (429, 503, 401 body, 402 body,
  timeout_pre, timeout_post, no_match) settles at exactly 10 000 e6 = 2 billable units, and the
  all-429 run settles at 0 with nothing written to the response cache.
- **Settlement idempotency and ambiguity.** A dispatched-but-unresolved unit settles as spent
  (§2.3.3), `settle()` returns the same value on re-entry, and `released == reserved - spent` holds
  after the outstanding charge.
- **Exit codes.** 4 + `budget_exhausted` + `reason=reservation_exceeds_cap` with zero AudD and zero
  Shazam attempts and no page written; 3 + `provider_unavailable` with no silent degrade to Shazam;
  0 + `partial` + `reason=reservation_exhausted`.
- **`max_usd_e2 = 0` forbids every paid call path.** The clip path (engine not in
  `enabled_engines`), the whole-file scanner path (P1-2 gate plus the inner `PAID_FILE_SCANNERS`
  filter) and the supplemental lever (`want_clips` false) are all closed, now with a sentinel test
  over both call sites.
- **E-S3.** `profile_app_config` never maps `budget.max_usd_e2`; the effective cap comes from the
  recipe and `pricing.toml` only.

### Files changed in this pass

`src/id_detector/cli.py` (legacy recipe default, `--engine` notice, paid-scanner gate),
`src/id_detector/pricing.py` (`_resolve_pricing_path`), `src/id_detector/paid_clip.py`
(`_HTTP_STATUS` parsing, timeout classification), `src/id_detector/providers/audd.py` (distinct
pre-receipt timeout message), `pyproject.toml` (wheel force-include), `tests/test_phase0a_money.py`
(+6 tests, transport test parametrised over three transports), `tests/test_stage1_wheel.py`
(packaged pricing assertion). `docs/PLAN-v2.md`, `profiles/` and `data/corpus/` untouched; no
dependency added.

### Final gate outputs

1. `uv run pytest -q`

```text
620 passed, 93 deselected, 1 warning in 109.82s (0:01:49)
```

2. `uv run ruff check .`

```text
All checks passed!
```

3. `uv run ruff format --check .`

```text
217 files already formatted
```

4. `uv run python scripts/audit_fixtures.py`

```text
audited 371 files
fixture audit passed
```

5. Phase gate — `uv run pytest tests/test_phase0a_money.py -q`

```text
24 passed, 1 warning in 25.26s
```

6. `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_serve.ps1`

```text
smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'
```

7. Local-mode end-to-end (fakes)

```text
uv run idea analyse tests/fixtures/audio/tone-60s.wav --recipe deep --fake-providers audd,shazam --work-root <tmp>
uv run python scripts/assert_journal.py --work-root <tmp> --expect algorithm_version=targeting:0,fusion:1 usd_e6_reserved=36750 usd_e6_spent=35000 usd_e2_spent=4 usd_e2_reserved=4
journal assertion passed
```

8. Local-mode end-to-end (P0-1 regression, blocked token)

```text
uv run idea analyse tests/fixtures/audio/tone-60s.wav --profile max_accuracy --work-root <tmp>
uv run python scripts/assert_journal.py --work-root <tmp> --expect status=succeeded exit_code=0 usd_e6_reserved=0 usd_e6_spent=0 algorithm_version=fusion:1
journal assertion passed
```
