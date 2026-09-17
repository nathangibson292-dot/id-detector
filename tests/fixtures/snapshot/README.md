# Restore-drill snapshot fixture

The committed inputs of the plan §4.6 restore drill (cycle 4b-iii; `tests/idea_web/test_backup.py`).

* `artefacts/` — one media's snapshot: a sealed result bundle, its frozen fuse run,
  the `recognise/` and `hints/` artefacts with their completion sidecars, and
  `ingest/source.json`. Synthetic: no rendered page, no platform URLs, no handles.
* `app.sql` — the queue database as SQL, so every committed byte is readable. The
  drill materialises `app.db` from it and seals `snapshot.json` with the production
  `idea_web.backup.describe`/`seal`, then restores and verifies.
* `result_bundles.path` holds an ABSOLUTE path under the placeholder work root
  `/idea-fixture-work`, exactly as production stores it. The drill records that
  root as `work_root` when it seals, and restore re-roots the paths for wherever the
  snapshot actually lands.

Regenerate with `scripts`-free tooling: see the generator named in
`docs/reviews/build-4b-ii-iii.md`.
