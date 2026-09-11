# AudD — answers to the four commercial questions (2026-09-11)

The owner asked `api@audd.io` the four questions left open by the Terms assessment
([audd-terms-assessment.md](audd-terms-assessment.md)), describing the product honestly: DJ-mix track
identification from SoundCloud/Mixcloud links, timestamped tracklists shown to our own users, ~12-second clips
on the standard `api.audd.io` recognise endpoint, plus a note that our internal accuracy checks are product
evaluation of our own confidence tiers, not a competing recognition service.

## What they answered

| # | Question | AudD's answer |
|---|---|---|
| 1 | Is the "$2 per 1,000 requests" subscription rate available on the standard per-clip endpoint, and at what monthly commitment? | "No, this is the enterprise pricing for clients sending hundreds of millions of requests per month on negotiated terms." |
| 2 | Documented rate limit and concurrency for that endpoint? | "You shouldn't experience any rate limits or concurrency limits on api.audd.io." |
| 3 | Are HTTP 429/503 responses billed? | "No; you should not experience those HTTP errors." |
| 4 | If a request times out after upload, is there a way to reconcile whether it was billed? | "You should not experience any requests timing out. If you do, please get in touch with us." |

They raised no objection to the described use, and none to the internal accuracy evaluation.

## What each answer means for the build

1. **The subscription rate is out of reach.** $2/1,000 needs hundreds of millions of requests a month; we will
   send thousands. **Every price and allowance therefore stands on the walk-up rate of $5 per 1,000 requests**
   — which is exactly what `pricing.toml` already encodes (`audd_usd_e6_per_request = 5000`) and what the
   provisional Pro allowance (90 Deep minutes) and pack (55 minutes) were computed from, the walk-up d=1 row of
   the plan's COGS table. **No pricing change is needed.** The two "subscription" rows in PLAN-v2 §3.3 and the
   subscription column in §2.3.6 are unreachable and are marked as such; nobody should plan against them.
   Revisit only if AudD ever offers mid-tier pricing.
2. **There is no documented rate limit or concurrency ceiling**, so there is also no contractual headroom to
   rely on. Keep our self-imposed limits exactly as built (concurrency 4 plus a token bucket in the AudD
   adapter): they protect the provider relationship and keep wall-clock predictable, and "you shouldn't
   experience any" is not a guarantee we can hold anyone to.
3. **429/503 are not billed**, which matches `bill_on_throttle = false` and the outcome cost table (429, 503,
   connect errors, pre-dispatch timeouts and auth/quota errors all cost zero units). Their added claim that we
   "should not experience those errors" is an expectation, not an SLA — the retry, breaker and
   `provider_unavailable` paths stay.
4. **There is no reconciliation mechanism.** A request that times out after upload therefore remains
   *ambiguous*, and our conservative rule stands: an ambiguous attempt is counted as spent and is never
   automatically retried, so we can never under-count what we owe or double-bill a user. If it ever happens in
   volume, the owner emails AudD with the attempt journal (`attempts.py` records every dispatch durably before
   network I/O, which is what makes that conversation possible).

## L1 status

Licensing was already cleared from the Terms text. With these answers the **commercial questions are closed**:
the rate is known ($5/1,000, no subscription tier available to us), there is no documented limit to design
against, throttled requests are free, and ambiguity is ours to absorb. No answer contradicts the Terms
assessment or changes what we may ship. The one thing we did **not** get is any written service commitment, so
the plan's defensive handling of provider failure is load-bearing and must not be simplified away later.
