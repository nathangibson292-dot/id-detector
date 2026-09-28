## A — Shazam retry

The normal sweep behavior is correct: each dispatched provider error is retried once, in window order, through the same adapter, limiter, breaker, budget, and journal. Credential/quota refusals and unresolved attempts are excluded. The five changed assertions are exact: `+1`, `+2`, `+7`, `+2`, and `+1`; no adjacent assertion was weakened.

A large `connect_error`/`timeout_pre` set can consume the remaining daily allowance because those outcomes do not count toward the breaker’s failure-rate sample. It still cannot exceed either hard budget.

## B — MixesDB

Correct. Real-shaped timestamped wikitext covers italic/bold markup, stray closing markup, internal apostrophes, and quoted placeholders. `ID`, `?`, and `??` titles become identity-less placeholders rather than tracks.

## C — No/refused token

The ordinary no-token CLI, cost, and browser paths are clear and avoid a dead Approve button. Approval and ledger authority files are unchanged.

Two money blockers remain:

- The cited [AudD API documentation](https://docs.audd.io/#common-errors) establishes meanings only for 900/901. The [official Java SDK](https://github.com/AudDMusic/audd-java#errors) and [Go SDK](https://github.com/AudDMusic/audd-go#errors) classify 902–905, 19, 31337, and 611, but do not guarantee that requests returning them are unbilled. AudD describes pricing per request, so SDK exception categories alone do not justify refunding a post-billed request.
- Deep runs four AudD requests concurrently. A refusal can stop future dispatch while an in-flight sibling resolves `timeout_post`, `http_5xx`, or `malformed` and is correctly charged. Nevertheless [paid_clip.py](src/id_detector/paid_clip.py:228), [pipeline.py](src/id_detector/pipeline.py:1508), and the CLI/browser renderers can still say “nothing was spent.” The frozen settlement already contains the true spend; the presentation ignores it.

Code 19 is also broader than “account blocked”: AudD’s [Python SDK documentation](https://docs.audd.io/sdks/python/) describes request blocking, abuse, or test-scope cases.

## D — Progress bar and `--runner`

The screenshots credibly show 10% → 39% → 39% after reload → completion. They contain no token, account detail, personal path, address bar, or PNG metadata. The elapsed panel is collapsed, so elapsed continuity is supported by the report timeline and focused test, not visibly by the screenshots.

Preserving `started_at` while resetting only the phase clock is correct and tested.

The CLI and worker both refuse `--runner` outside test mode; normal serve passes no spec, and the web app exposes no route to it. However [local.py](src/idea_web/jobs/local.py:1251) imports and executes any supplied `module:factory` once `IDEA_TEST_MODE=1`. Such a runner bypasses the provider-override guard entirely.

## E — Hardenings and scope

The golden test compares the produced raw bytes against an independently rendered expectation. The golden remains blob `316b6171c42f0772a9bce3066cf716491f22d181`.

The approval test kills a genuine child process inside the approval transaction, verifies rollback, retries twice, and proves exactly seven AudD calls and one reservation.

Protected files and directories are absent from diff/status, including fusion, recipes, playlists, profiles, corpus, and second-session paths. The live job page changed, not saved result-page rendering, so no `PAGE_VERSION` bump is needed. The reported shard arithmetic reconciles exactly to 2,130 passed plus two skipped. No public-repository secret or personal detail was found.

## Required fixes

### Realistic

- **P1:** Obtain explicit AudD billing evidence for every newly refunded code, including 611, or stop terminally while conservatively counting the dispatched attempt as spent and never retrying it.
- **P1:** Base CLI/browser refusal wording on the frozen settled spend. Add a mixed concurrent test with one refusal and one billable/ambiguous in-flight outcome; never display “nothing spent” when `usd_e6_spent > 0`.

### Adversarial

- **P1:** Replace arbitrary `module:factory` loading with a built-in or strict allowlisted offline runner that enforces an empty token and fake providers.

## Follow-ups, not blockers

- **P2:** No-token Deep with some cached paid answers bypasses the new notice; `idea cost` with unknown duration also exits before printing it.
- **P2:** Commit the raw browser sample trace or screenshots with Timing details expanded; current images do not visibly prove elapsed continuity.
- **P2:** Consider a retry-pass cap or breaker treatment for connection/pre-response failures so one bad mix cannot consume the remaining daily allowance.

VERDICT: FIX_FIRST
