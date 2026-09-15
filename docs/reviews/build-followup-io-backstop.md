# Build: the io corpus backstop under the durable file helpers

Follow-up to the truth fix (`28d8fe1`). Its record
([followup-truth-corpus.md](followup-truth-corpus.md), round 10, "Follow-ups recorded, not built")
noted that the money fix (`bb57b70`) added `io.durable_replace` and `io.create_file_durably`, and
that neither called `io.refuse_corpus_destination`, the backstop that `atomic_write_bytes` runs
first. Built by Claude (Fable) on 2026-09-15 in an isolated worktree on `28d8fe1`. Nothing was
committed, branched, merged or pushed.

**Worktree note.** The worktree was parked at `9bfacf8` (an old commit). Its branch was clean, so
it was moved to `28d8fe1` with `git reset --hard` before any work; no branch was created.

## What changed, file by file

### `src/id_detector/io.py` (+19 / -6)

Every shared helper in `io.py` that creates, replaces or renames a file at a caller-chosen
destination now calls `refuse_corpus_destination` first, with the same exemption semantics as
`atomic_write_bytes`:

- `durable_replace(source, destination, *, through_corpus_gateway=False)`: before the source is
  fsynced and before any move, `refuse_corpus_destination(Path(destination).resolve())` runs
  unless `through_corpus_gateway=True`. The move itself is unchanged.
- `create_file_durably(path, *, through_corpus_gateway=False)`: after the path is resolved and
  **before** `ensure_directory_durable(path.parent)`, `refuse_corpus_destination(path)` runs
  unless `through_corpus_gateway=True`. Ordering matters: a refused destination below a set now
  leaves no new directory behind (the tests assert the tree is byte-identical and directory-identical).
- `refuse_corpus_destination`'s docstring now names the three helpers it sits under.

No other helper in `io.py` creates, replaces or renames a file at a caller-chosen destination:
`atomic_write_bytes` (already guarded; `atomic_write_json` and `write_completion_sidecar` go
through it), `_move_write_through` (private, reached only from the two helpers above),
`fsync_file` / `fsync_directory` (flush only), `ensure_directory_durable` (directories only; its
other callers, `run_ledger.write_reservation` / `project_reservation`, write their file through
`atomic_write_bytes`, which is guarded).

**Cost.** Nothing new beyond the backstop's own bounded checks: one name comparison and at most
six `lstat` probes (`os.path.lexists`), no directory listing. The work-tree test counts the probes
per call and patches `os.scandir` and `os.listdir` to prove no listing happens.

### Callers of the newly guarded helpers (checked; none refused, none needs the exemption)

- `durable_replace`: `src/id_detector/decode.py:142` moves the decoded PCM into the work tree's
  media directory. Work-tree destination; unaffected.
- `create_file_durably`: `src/id_detector/journal.py:142` (`append_line`) creates the money
  journal / attempt journal / `invocations.jsonl` in the work tree. Unaffected.
- The truth gateway (`truth.py`) and `truth_paths.py` do **not** use either helper: owner truth is
  written by `truth_paths.PinnedDirectory.write_bytes`, which has its own exclusive-create and
  entry-replacing rename, and the gateway's only `io` write is
  `truth.write_corpus_file_through_gateway`, which already passes `through_corpus_gateway=True` to
  `atomic_write_bytes`. So no production caller passes the exemption to the two new keywords; a test
  (`test_only_the_gateway_helper_passes_the_exemption`) pins that: the only file under `src/` or
  `scripts/` whose call sites pass `through_corpus_gateway=True` is `src/id_detector/truth.py`.
- `tests/test_truth_gateway_guard.py` (corpus-name literals outside the gateway modules) is
  unaffected: `io.py` gained no new literal; its three names were already allowlisted.

### `tests/test_io_backstop_followup.py` (new, 14 tests)

| Test | What it proves |
|---|---|
| `test_durable_replace_refuses_a_corpus_destination[set-truth, manifest-name, inside-set, below-set, inside-manifest-corpus]` | `durable_replace` refuses a set's `ground_truth.json`, a `corpus-version.json` by name anywhere, a file inside a set, a file below a set, and a file inside a manifest-only corpus; the source stays staged and the whole temporary tree (files and directories) is unchanged |
| `test_create_file_durably_refuses_a_corpus_destination[same five]` | `create_file_durably` refuses the same five destinations and creates no directory, no temporary and no file |
| `test_the_money_journal_cannot_be_started_inside_a_set` | End to end through a real caller: `journal.append_line` into a set's `.attempts/audd.jsonl` is refused and nothing appears |
| `test_work_tree_writes_are_unaffected_bounded_and_never_list` | A `.attempts/*.jsonl` (existing and new), `invocations.jsonl` via `append_line`, an `app.db` sibling and a `present/bundles/<hash>/tracklist.json` replace all still work; each counted call makes at most six `lexists` probes and no `scandir` / `listdir` |
| `test_the_gateway_exemption_still_works_for_every_guarded_helper` | `durable_replace(..., through_corpus_gateway=True)` replaces a set's truth, `create_file_durably(..., through_corpus_gateway=True)` creates in a set (and returns `False` the second time), and `truth.write_corpus_file_through_gateway` still writes a new set's truth |
| `test_only_the_gateway_helper_passes_the_exemption` | AST scan of `src/` and `scripts/`: the only file whose call sites pass `through_corpus_gateway=True` is `truth.py` (docstrings do not count) |

The file touches only `tmp_path`; nothing under `data/corpus/` or `work/` is read or written.

### `docs/reviews/build-followup-io-backstop.md`

This record.

## Revert proof

Each new guard was removed in place (the two-line `if not through_corpus_gateway: refuse_...`
block, matched together with its following line so the identical block in `atomic_write_bytes`
was untouched), the new test file was run, the file was restored from the saved bytes and its
SHA-256 compared. Script: a scratchpad `revert_proof.py`, run in the foreground.

```text
io.py sha256 before: 3480eb2f441173376c196941ea92c0984c93aef0e726d1dde96c0b61422a25fb

== W1 durable_replace guard removed ==
FAILED tests/test_io_backstop_followup.py::test_durable_replace_refuses_a_corpus_destination[below-set]
FAILED tests/test_io_backstop_followup.py::test_durable_replace_refuses_a_corpus_destination[inside-manifest-corpus]
FAILED tests/test_io_backstop_followup.py::test_durable_replace_refuses_a_corpus_destination[inside-set]
FAILED tests/test_io_backstop_followup.py::test_durable_replace_refuses_a_corpus_destination[manifest-name]
FAILED tests/test_io_backstop_followup.py::test_durable_replace_refuses_a_corpus_destination[set-truth]
5 failed, 9 passed in 7.95s
restored byte-identical: True (3480eb2f441173376c196941ea92c0984c93aef0e726d1dde96c0b61422a25fb)

== W2 create_file_durably guard removed ==
FAILED tests/test_io_backstop_followup.py::test_create_file_durably_refuses_a_corpus_destination[below-set]
FAILED tests/test_io_backstop_followup.py::test_create_file_durably_refuses_a_corpus_destination[inside-manifest-corpus]
FAILED tests/test_io_backstop_followup.py::test_create_file_durably_refuses_a_corpus_destination[inside-set]
FAILED tests/test_io_backstop_followup.py::test_create_file_durably_refuses_a_corpus_destination[manifest-name]
FAILED tests/test_io_backstop_followup.py::test_create_file_durably_refuses_a_corpus_destination[set-truth]
FAILED tests/test_io_backstop_followup.py::test_the_money_journal_cannot_be_started_inside_a_set
6 failed, 8 passed in 1.11s
restored byte-identical: True (3480eb2f441173376c196941ea92c0984c93aef0e726d1dde96c0b61422a25fb)
```

Exactly the refusal tests for the removed guard fail (plus the `append_line` end-to-end test for
W2, which reaches the helper through its caller); the work-tree, exemption and scan tests pass on
both versions, as they should. With both guards in place: `14 passed in 11.48s`.

## Gate outputs

### Full suite, in the foreground, in six shards

One `uv run pytest -q` exceeds the 10-minute foreground limit (the orchestrator's own "shard A"
took 758 s), so the default collection was run in six shards that together name every collected
test file (`--collect-only`: 1,709 collected, 97 deselected by the default `not slow and not live`
markers; every collected file is under `tests/idea_web/` or is one of the `tests/test_*.py` files
named below). Each shard was run with `-q -p no:cacheprovider` and a clean environment (this
worktree has no `.env`; `cli.PROJECT_ROOT` resolves to the worktree, `dotenv present: False`).

```text
S1 tests/idea_web:                                          208 passed, 1 warning in 190.97s (0:03:10)
S2 tests/test_[a-o]*.py (13 files, explicit) + test_io_backstop_followup.py + test_paid_clip.py:
                                                            154 passed, 1 warning in 68.69s (0:01:08)
S3 tests/test_phase*.py (15 files, explicit):               418 passed, 1 warning in 311.24s (0:05:11)
S4 test_playlists, test_projection, test_scan, test_scan_targeting, test_score_corpus,
   test_semantics, test_service_api:                        157 passed, 1 warning in 136.43s (0:02:16)
S5 tests/test_stage*.py (37 files, explicit):               467 passed, 1 skipped, 93 deselected, 1 warning in 107.51s (0:01:47)
S6 test_truth_corpus_followup*.py (10 files), test_truth_gateway_guard.py, test_truth_review.py:
                                                            303 passed, 1 skipped, 4 deselected, 1 warning in 168.35s (0:02:48)
```

**Total: 1,707 passed, 2 skipped (both pre-existing), 0 failed.** (Main after the truth fix was
1,694 passed + 1 skipped; this adds the 14 new tests.) The warning in every shard is pydub's
`audioop` deprecation.

*Honesty note on a discarded run.* A first attempt at S1 and S3 was run with
`IDEA_TEST_MODE=1 AUDD_API_TOKEN= IDEA_ENGINE_SHAZAM=off` exported, following the no-live-calls
rule literally. That environment leaked into the pipeline tests (`shazam_manual_off` replaced the
expected `secondary_not_achieved` reasons, hosted-Shazam worker tests saw the engine off) and
produced 10 + 66 failures that have nothing to do with this change; one was rerun alone to confirm
the cause. The suite carries its own fakes and this worktree holds no token, so the shards above
were rerun with no overrides and are the results that count. No live provider was ever called; no
CLI or pipeline was invoked outside the test suite.

### `uv run ruff check .` / `uv run ruff format --check .`

```text
All checks passed!
349 files already formatted
```

### `uv run python scripts/audit_fixtures.py` (real committed corpus included, read-only)

```text
audited 484 files
fixture audit passed
```

### `git status --short` / `git diff --stat`

```text
 M src/id_detector/io.py
?? tests/test_io_backstop_followup.py
```

```text
 src/id_detector/io.py | 25 +++++++++++++++++++------
 1 file changed, 19 insertions(+), 6 deletions(-)
```

(Before this record was written; it adds `?? docs/reviews/build-followup-io-backstop.md`.)
`git status --short -- work data` is empty. No server was started on ports 8791/8792; the
PowerShell gates were left to the orchestrator.

## Out of scope, noted

- `ensure_directory_durable` creates directories, not files, and was left alone; its two external
  callers (`run_ledger`) write through the guarded `atomic_write_bytes`, but a refused reservation
  write would already have made the directory durable first. Same shape as before this change.
- The in-place `os.replace` calls in `decode.py`, `ingest.py`, `windows.py`,
  `providers/panako_setup.py`, `retention.py` and the controlled renderer write fixed media names in
  the work tree or a validated staging folder and are not shared helpers, as the truth record
  already noted.

## Orchestrator gate runs on the final code

The worktree guard in the fixer's session refused PowerShell, so the orchestrator ran both PowerShell gates from the fixer's worktree against the final code, after checking the loopback ports were free:

```text
=== io backstop follow-up applied to main (base 28d8fe1) — orchestrator gate runs ===
ruff check .            : All checks passed!
ruff format --check .   : 350 files already formatted
scripts/audit_fixtures.py: fixture audit passed
scripts/check_page_js.py : page JavaScript check passed: 53 inline scripts across 22 page renders
note: the 4a-iii Fable builder was running tests in its own worktree during these runs
pytest shard A (tests/idea_web, tests/test_[a-o]*.py, tests/test_phase*.py, incl. tests/test_io_backstop_followup.py): 773 passed, 1 warning in 889.87s (0 failed)
pytest shard B (all remaining tests/test_*.py): 935 passed, 1 skipped, 97 deselected (default slow/live markers), 1 warning in 481.00s (0 failed)
full suite on main: 1,708 passed, 1 skipped, 0 failed (= the 28d8fe1 baseline of 1,694 passed + the 14 new backstop tests; resolves the review's P2 about the builder's differently sharded totals)
smoke_serve.ps1 (23:59:27-23:59:31, 4a-iii fixer pytest running in its own worktree; ports free, cpu 3%): smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'; exit 0 (first run)
gate_local_mode.ps1 (23:59:41-23:59:55): prepared offline cached mix; local gate passed: idea.cmd; cached mix open; audio metadata + seek; probe logged audio-ok; exit 0 (first run, no re-run needed)
```

Second-model reviews (Codex gpt-5.6-sol at xhigh, model verified from each log header), filed in round order:

- `docs/reviews/followup-io-backstop-sol-r1.md`

