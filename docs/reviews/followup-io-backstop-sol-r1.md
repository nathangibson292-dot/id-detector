## A coverage

No unguarded caller-facing file-write helper found.

- `atomic_write_bytes` resolves and guards at `src/id_detector/io.py:121-123`, before directory/temp-file creation at `:124-127`.
- `durable_replace` guards at `:227-228`, before source fsync or rename at `:229-233`.
- `create_file_durably` guards at `:246-248`, before directory or temporary-file creation at `:249-252`.
- `atomic_write_json` delegates to guarded `atomic_write_bytes` at `:278-279`; `write_completion_sidecar` reaches it at `:310`.
- `_move_write_through` does not itself guard (`:198-212`), but is private and its only calls follow the guards at `:231` and `:264`. No current bypass exists.
- `fsync_*` only flush existing objects; `ensure_directory_durable` creates directories, not files.

Cost remains two ancestors × three fixed names: six `lexists` probes at `io.py:105-107`, with no directory enumeration.

Only the truth gateway claims the exemption in production: `src/id_detector/truth.py:449-457`. The AST ownership test scans `src/` and `scripts/` and pins that result at `tests/test_io_backstop_followup.py:208-227`.

## B regressions

No realistic false refusal found on the owner’s normal `idea serve`, `idea.cmd`, or `idea analyse` path.

- `idea.cmd` first changes to the repository root (`idea.cmd:4-5`), and the default work root is `work` (`src/id_detector/cli.py:97`).
- Media directories are `<work>/<source>/<media>` (`src/id_detector/ingest.py:279-284`).
- Direct attempt journals are `<media>/recognise/attempts.jsonl` (`src/id_detector/attempts.py:35-47`); queue journals are `<work>/.attempts/<run>.jsonl` (`src/idea_web/jobs/worker.py:2547-2554`). Neither probes a corpus-bearing ancestor.
- Reservation sidecars are under the journal’s `reservations/` directory (`src/id_detector/run_ledger.py:457-460`); their existing atomic writes remain guarded at `:478-490`.
- Checkpoints are `<work>/.checkpoints/<run>.json` (`src/id_detector/service.py:331-372`).
- The local database is `<work>/.idea/app.db` (`src/idea_web/jobs/local.py:162-171`); SQLite itself does not call this backstop.
- Manual tracklists use `<work>/.tracklists/<hash>.txt` (`src/id_detector/service.py:695-701`).
- Bundles are below `<media>/present/bundles/` (`src/id_detector/present/bundles.py:265-272`).
- Enrichment and hint caches are below `data/local/enrich` and `data/local/hints` (`src/id_detector/cli.py:814-820`, `src/id_detector/hints/pipeline.py:417`).
- Playlists default to `data/local/playlists.json` (`src/id_detector/playlists/store.py:20-25,70-72`).
- Settings creation uses its own validated direct write (`src/id_detector/cli.py:367-383`) and is unaffected.
- Gateway writes skip the backstop at `truth.py:457`; pinned owner-truth writes remain independent at `src/id_detector/truth_paths.py:245-281`.

A custom work root deliberately placed inside a corpus will now be refused; that is intended protection, not a legitimate-path regression.

## C tests

The new file collects 14 tests successfully, but I could not execute them: `uv.exe` is unavailable in this sandbox, and direct pytest cannot obtain a writable temporary directory.

Static coverage is sound:

- Five corpus destination shapes are defined at `tests/test_io_backstop_followup.py:28-40`.
- Both new guards are exercised over all five at `:77-101`, with before/after tree checks.
- The journal integration refusal is covered at `:104-110`.
- Positive work-tree writes, bounded probes, and no `scandir`/`listdir` are checked at `:118-180`.
- Gateway behavior and exclusive production ownership are checked at `:188-227`.
- Removing either guard makes its five direct refusal cases fail; removing `create_file_durably`’s guard also breaks the journal case.
- No existing test file changed. Focused Ruff and formatting checks passed for `io.py` and the new test.

## D scope

`git status` contains only:

- Modified: `src/id_detector/io.py`
- New: `tests/test_io_backstop_followup.py`
- New: `docs/reviews/build-followup-io-backstop.md`

None of `src/id_detector/playlists/**`, `tests/test_playlists.py`, `README.md`, `idea.cmd`, `src/id_detector/present/theme.py`, `data/`, or `work/` is touched.

## Required fixes

### Realistic

- P2 — Reconcile the gate totals in `docs/reviews/build-followup-io-backstop.md:131-132`: the baseline reports 1,694 passed/1 skipped, while the follow-up reports 1,707 passed/2 skipped after 14 new passing cases. Explain the additional skip or correct the comparison.

### Adversarial

None within the stated threat model.

VERDICT: OK_TO_COMMIT