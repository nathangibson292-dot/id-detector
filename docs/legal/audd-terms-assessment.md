# AudD API Terms of Service — assessment for IDea (launch gate L1)

*Assessed 2026-09-10 against the AudD® API Terms of Service, text last changed 2026-07-07, read by the
owner in a browser at <https://audd.io/terms/> and pasted verbatim into
[audd-terms-2026-07-07.md](audd-terms-2026-07-07.md). Machine-readable version endpoint:
`GET https://api.audd.io/terms/version` → `{"version": "2026-07-07", …}`. Not legal advice — the
owner's solicitor reviews under L2.*

## What the plan asked (PLAN-v2 §6.3 L1) and what the Terms say

| Question | Answer from the Terms | Consequence |
|---|---|---|
| Hosted / consumer use — may we show Results to our users in a paid product? | **Yes.** "AudD grants the customer a … license … to use the results returned by the Service ('Results') within the customer's own products and services, including displaying Results to the customer's users." | Hosted Deep scans are licensed. |
| Caching / retention of Results | **Yes.** "The customer may cache and store Results for use within its own products and services." | The content-addressed response cache and the cross-user reuse of clip responses are permitted. |
| Attribution | **Optional for paying customers** ("the customer might display a 'Powered by AudD' link or image"); mandatory only for free/educational/non-profit access under separate branding rules. | The Test License's logo requirement ends once we pay. Keep the attribution slot as an optional config value. |
| Clip vs whole-file recognition | No distinction; nothing forbids DJ-mix analysis. | The clip path is fine. |
| Concurrency / rate limits | Not stated numerically; customers must not "use automated systems in a manner inconsistent with AudD's documentation, rate limits, or written instructions" nor "bypass … rate limits". | Our AIMD limiter backs off on 429/503 (0b-i). **Ask AudD for the documented ceiling** and set `[deep] audd_requests_per_minute` from it. |
| Per-clip subscription rate; whether throttled/refused requests bill; reconciliation of ambiguous requests | **Not in the Terms** (pricing is "in accordance with our current pricing policy"). | Remaining L1 items are commercial only — one email to api@audd.io. They set the Pro allowance (`pricing.toml`), not whether we may launch. |

## Restrictions we must honour

1. **No standalone data product:** Results may not be resold or redistributed "as a standalone data product".
   A tracklist page/export inside IDea is use "within the customer's own products". The future Pro Report
   tier must be a report about a mix, not a Results feed. *(Plan D6 / M2+.)*
2. **No competing recognition service:** Results may not be used "to build, train, or improve a
   music-recognition or audio-identification service, database, or model that competes with the Service",
   and the Service may not be used to "build, train, benchmark, or improve a competing service, model,
   database, or product". IDea's fusion trains nothing and the Panako index is built from the DJ's own
   uploads, not from Results. The **release corpus (L3) scores AudD alongside Shazam** — internal evaluation
   of our product, not a competing service — but it sits near the word "benchmark": mention it in the email
   and keep the accuracy report about IDea's tiers, not a provider comparison.
3. **Flow-down to users:** "The customer will ensure that its users' access to the Service is subject to
   terms at least as protective of AudD as the Terms." Our ToS must carry the same use restrictions and the
   users' warranty that they hold the rights to the audio they submit; we indemnify AudD for Customer
   Content. *(L2 — our ToS draft in 6b.)*
4. **Rights in Customer Content:** we warrant we have the rights/permissions to submit the audio. Same
   posture as every competitor ("the user asserts the rights; audio is not re-hosted; deleted within 7 days").
   Our privacy sentence already says only short clips go to the engines. *(L2.)*
5. **Termination without notice** is at AudD's discretion (prepaid unused fees refunded when without cause).
   Keeps R10 alive: engine abstraction stays; never a single point of failure for paying users.
6. **Terms change detection:** AudD recommends automated checks of `GET https://api.audd.io/terms/version`
   with a human alert on change. Queued for the ops runbook (6b) — a daily check that logs and alerts.
7. **Contracting entity:** AudD, LLC (Delaware law) or High Expected Value LTD (England & Wales law) per
   the invoice — relevant to L2/L4 (UK owner).

## L1 status

**Licensing: cleared.** Hosted third-party Deep scans, caching and display are permitted for a paying
customer; no attribution obligation. **Commercial questions remain** (rate for the per-clip endpoint,
documented rate limit/concurrency, billing of 429s, reconciliation of ambiguous requests) — they set the
Pro allowance in `pricing.toml` and do not block the private beta.
