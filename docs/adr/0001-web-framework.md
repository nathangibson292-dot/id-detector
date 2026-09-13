# ADR 0001: Web framework

Status: Proposed — awaiting owner decision (plan spike S4)

## Decision

Use FastAPI on Starlette, Jinja2 templates, and uvicorn for both hosted and local HTTP service.

## Context

IDea needs a small server-rendered web application, not a SPA. It must provide pages and a narrow JSON status endpoint; use server-side sessions plus synchroniser-token CSRF and Origin checks on both loopback and the hosted origin; and run the analysis pipeline only in a separate worker process. The web process enqueues and reads state; the N=1 worker leases and runs jobs.

Both processes share `app.db` in SQLite WAL mode. Job pages poll status every 2.5 seconds. Result routes authorise access, reject traversal and symlink escapes, and serve immutable `present/bundles/<bundle_id>/` content; `/media/<media_key>/audio` also needs correct `HEAD` and single-range `206`/`416` behaviour for seeking. `idea serve` must run this same application in loopback/local mode, not a second implementation.

Hosted deployment is one small VPS (about €16/month): Caddy terminates TLS, applies the 250 MB request cap and security headers, and proxies to one uvicorn web process; one separate worker consumes jobs. Framework overhead matters, but pipeline CPU, RAM, and disk dominate.

The current implementation is a loopback-only `ThreadingHTTPServer` in `src/id_detector/present/server.py`. It hand-rolls routing, HTML, form/JSON parsing, CSRF/Origin checks, byte ranges, and safe path resolution. `src/id_detector/webapp/jobs.py` keeps jobs in memory and executes them on a thread; `runner.py` already adapts jobs to the `id_detector.service` seam. This is the behaviour 4a-ii must preserve before retiring the old server.

## Options considered

### FastAPI + Jinja2 + uvicorn — recommended

Migration is a route-for-route rewrite rather than reuse of `BaseHTTPRequestHandler`, but the existing handler functions expose a clear inventory. ASGI fits the existing async/httpx ecosystem and later webhook or email clients, while the pipeline remains outside the web event loop in the worker. Jinja2 replaces assembled HTML without introducing a front-end build. FastAPI/Starlette does not supply our required database-backed sessions, synchroniser CSRF, or Origin policy, so those remain small explicit modules. `StaticFiles` can serve public assets; authorised bundles require our own resolver and `FileResponse`/range tests rather than a broad static mount. Starlette's httpx-based test client maps well to the existing pytest/httpx assertions. One uvicorn process is light enough for the VPS.

### Flask + Jinja2 + Gunicorn (or uvicorn through a WSGI adapter)

The routing migration is similarly sized and Jinja2 is built in. Flask's default signed client cookie is not the revocable server-side session required here; database sessions and synchroniser CSRF need our code or extensions. `send_file(conditional=True)` helps with ranges, but authorised bundle lookup, traversal checks, and exact media tests remain ours. Flask's test client is good, although current httpx tests would need a different adapter/style. The pipeline is correctly isolated in either design, but Flask's WSGI core brings no benefit to the repo's async httpx code; async views add per-request event-loop machinery rather than an end-to-end ASGI model. Gunicorn is fine on the hosted Linux box but not the local Windows path, while uvicorn requires an adapter, creating two serving shapes for little saving. Resource cost is modest but slightly more operationally awkward than one uvicorn command everywhere.

### Django

Django would replace rather than merely port the web layer: settings, ORM models, migration ownership, URL configuration, and template conventions would reshape the planned hand-written SQLite schema and numbered SQL migrations. Its templates, auth, server-side sessions, CSRF, password flows, admin, and test client would remove substantial later work. Its ASGI support can host async views, but synchronous ORM boundaries still need care; the pipeline must still be a separate worker. Production static delivery belongs at Caddy (or adds WhiteNoise), and protected bundle/range delivery still needs a custom authorised response because a normal static mount is unsafe and Django's file response does not implement our whole range contract for us. It would run on this VPS at N=1, but has the largest dependency, configuration, startup, and migration footprint. That cost is unjustified for one narrow site and a service/data model already specified outside an ORM.

### Keep the stdlib `http.server`

This preserves the most handler code, the current manual range implementation, and almost all existing integration tests. It has no template engine, multipart parser, session store, CSRF facility, middleware/dependency model, or production server contract; hosted auth, uploads, limits, proxy trust, and error handling would all become security-sensitive local infrastructure. Async pipeline/httpx calls require thread/event-loop bridges, although worker separation reduces that pressure. Static and bundle serving work only because this repo already wrote them by hand. It is operationally tiny, but thread-per-request behaviour and continued bespoke security code make it false economy for an internet-facing origin.

## Why the recommendation wins

FastAPI is the smallest change that establishes a production ASGI boundary without forcing IDea's content-addressed artefacts and explicit SQL model into a framework ORM. The current routes already divide naturally into page, job, playlist, media, and result routers; the pipeline adapter already targets `id_detector.service`; and the suite already drives real HTTP with httpx. Jinja2 can initially render the same markup and CSS, while Starlette supplies request parsing, middleware, responses, and a testable ASGI application. Uvicorn then gives `idea serve` and hosted deployment the same server path.

This recommendation is conditional on maintaining the process boundary: no FastAPI route, background task, lifespan hook, or thread may run a pipeline. The app only validates, authorises, enqueues, and reports; the worker owns pipeline execution.

## What it costs us

We add FastAPI, Starlette, uvicorn, Jinja2/MarkupSafe, and multipart parsing (plus their transitive dependencies). We must build and test server-side session storage, synchroniser CSRF and Origin validation, password/account flows, rate limits, trusted-proxy handling, SQL migration tooling, and a minimal admin surface. Django would have supplied most of auth, CSRF, admin, and migrations; Flask would have supplied Jinja integration and a simpler synchronous extension ecosystem. FastAPI's dependency injection can also hide control flow if overused, so dependencies should be limited to settings, database transactions, current user, and authorisation.

We also retain custom security-critical code for bundle authorisation, canonical path/symlink containment, immutable-manifest verification, and HTTP ranges. Caddy's 250 MB cap is a perimeter guard, not a substitute for application-side content-length and streamed-body limits.

## Consequences for 4a-ii

Parity means the same visible local home/library, analyse form, job/progress page and 2.5-second polling, health check, redirects, form and JSON responses, cancellation/dismissal, recoverable library removal, verified local audio playback, and result/export behaviour. Preserve status codes, `HEAD`, byte ranges, MIME types, escaping, target validation, CSRF/Origin/Host protections, and traversal/symlink rejection covered by the current pytest suite.

Port the route and presentation behaviour from `present/server.py` into `src/idea_web` routers, Jinja templates, static assets, and explicit authorised file responses. Keep `present/page.py`, `present/theme.py`, `present/exports.py`, and immutable bundles as the presentation source during parity; do not re-render analytical results in request handlers. Put job dispatch behind the queue/worker interface so the web app never imports or invokes the pipeline runner. `idea serve` selects local settings and loopback policy around the same app factory used behind Caddy.

The playlist feature's thin second-session dispatch shim added in feature commit `6ef821f` (`present/server.py` → `id_detector.playlists.handle_get/handle_post`) must be re-pointed to FastAPI routes in this cycle, retaining the same CSRF and Origin guards. After parity tests and the local-mode gate pass, retire `present/server.py` and its in-process `webapp/jobs.py` ownership; remove compatibility code only when no caller or test depends on it.

## Reversibility

Moderate and intentionally bounded. The 4a-i `id_detector.service` request/result seam, framework-neutral worker queue, explicit SQL, Jinja templates, and immutable bundle routes keep domain and pipeline code outside FastAPI. Changing to Flask would mostly replace HTTP adapters; changing to Django would additionally migrate sessions, SQL ownership, and auth. Reversal becomes expensive only if FastAPI request objects or dependency injection leak into service, queue, or artefact modules, which this decision forbids.

## Owner decision

**Owner: approve FastAPI + Jinja2 + uvicorn as recommended, or name the alternative: ____________________.**
