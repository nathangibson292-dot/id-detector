## A — Audit of the reproduction verdicts

| Item | Evidence and judgment | Would regression be caught? |
|---|---|---|
| Invalid `.trash` parity | `_trash_root` rejects dangling entries, files, symlinks, junctions, and other reparse points at [retention.py:451](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a215a8533ef9de243/src/id_detector/retention.py:451). `collect` records the trash refusal and skips every media before dry-run/apply diverge at [retention.py:683](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a215a8533ef9de243/src/id_detector/retention.py:683) and [retention.py:688](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a215a8533ef9de243/src/id_detector/retention.py:688). The parameterized test covers live junction, dangling junction, and regular file, compares complete ordered action tuples, requires exactly one trash skip, and checks byte-for-byte immutability at [test_phase2b_retention.py:507](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a215a8533ef9de243/tests/test_phase2b_retention.py:507). | Yes. Reverting the guard to the apply-only branch makes dry-run add media moves while apply returns only the trash skip, failing lines 531–532 for every parameter. |
| Valid `.trash` parity | The valid long-path case creates ordinary dated trash, requires both `move` and `purge`, and compares the exact dry-run/apply action tuples at [test_phase2b_retention.py:670](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a215a8533ef9de243/tests/test_phase2b_retention.py:670). | Yes. This prevents the new early guard from under-reporting normal moves or purges. |
| Manifest preflight parity | Dry-run invokes the shared `_manifest_refusal` after planning moves at [retention.py:714](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a215a8533ef9de243/src/id_detector/retention.py:714); apply invokes the same check immediately before writing at [retention.py:572](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a215a8533ef9de243/src/id_detector/retention.py:572). The linked-manifest test compares complete ordered results at [test_phase2b_retention.py:924](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a215a8533ef9de243/tests/test_phase2b_retention.py:924). | Yes. Without dry-run preflight, apply contains the manifest skip and dry-run does not, so line 939 fails. |
| Literal `\\?\` lock spelling | The test constructs an explicit extended spelling, asserts its prefix and realistic lengths, verifies all canonical keys are equal, and tests every held/contending spelling pair in separate processes at [test_phase2b_retention.py:999](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a215a8533ef9de243/tests/test_phase2b_retention.py:999). `ProcessLock` still strips the prefix before forming its key at [jobs.py:47](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a215a8533ef9de243/src/id_detector/jobs.py:47). | Reverting the protected canonicalization would fail key equality and contention. There was intentionally no production lock fix: reverting only the test expansion would remove coverage rather than cause a runtime failure. |

## B — Correctness and safety bugs

No realistic correctness or safety bug found.

The six round-1 results remain intact:

- Move candidates and destinations undergo lexical containment, reparse-point refusal, resolved containment, and mutation-time revalidation before unresolved extended-path `os.replace` at [retention.py:479](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a215a8533ef9de243/src/id_detector/retention.py:479) and [retention.py:492](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a215a8533ef9de243/src/id_detector/retention.py:492).
- Sidecars and manifests are link-refused and revalidated immediately before atomic writes at [retention.py:445](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a215a8533ef9de243/src/id_detector/retention.py:445) and [retention.py:583](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a215a8533ef9de243/src/id_detector/retention.py:583).
- Purges are restricted to a real, unlinked `work/.trash/<date>` and revalidated immediately before extended-path `rmtree` at [retention.py:614](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a215a8533ef9de243/src/id_detector/retention.py:614) and [retention.py:770](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a215a8533ef9de243/src/id_detector/retention.py:770).
- `_TRASH_GRACE` remains eight calendar days, making the youngest entry provably at least seven days old before purge, at [retention.py:41](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a215a8533ef9de243/src/id_detector/retention.py:41).
- Traversal errors propagate and cause media-level refusal before sidecar changes or moves at [retention.py:700](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a215a8533ef9de243/src/id_detector/retention.py:700) and [retention.py:736](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a215a8533ef9de243/src/id_detector/retention.py:736).
- Dot source directories remain skipped at [retention.py:338](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a215a8533ef9de243/src/id_detector/retention.py:338), protecting `work/.idea/app.db`. Unreadable bundle/current/index state remains a reference at [retention.py:512](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a215a8533ef9de243/src/id_detector/retention.py:512) and [retention.py:537](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a215a8533ef9de243/src/id_detector/retention.py:537).

All permanent deletion remains confined to expired dated trash. Active artefacts are moved with `os.replace`; none is unlinked in place.

### Adversarial residual risks

- `[adversarial]` A hostile same-user process with write access could ignore the media lock and replace a checked destination ancestor, sidecar, or manifest with a link between the final checks and `os.replace`/`atomic_write_json`. The windows remain adjacent at [retention.py:501](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a215a8533ef9de243/src/id_detector/retention.py:501), [retention.py:445](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a215a8533ef9de243/src/id_detector/retention.py:445), and [retention.py:583](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a215a8533ef9de243/src/id_detector/retention.py:583). Round 3 added no work inside those windows.
- `[adversarial]` A hostile process could replace `.trash` with a junction after `_purgeable` succeeds and before `rmtree` at [retention.py:770](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a215a8533ef9de243/src/id_detector/retention.py:770). This still requires deliberately racing the immediate syscall boundary.

Both are still explicitly documented at [followup-longpath-retention.md:423](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a215a8533ef9de243/docs/reviews/followup-longpath-retention.md:423) and have not grown.

## C — Remaining long-path-unsafe calls

None found in production retention.

Existence, directory checks, collision checks, listing, traversal, and stat use `native_path` or unresolved extended paths. `makedirs`, `os.replace`, and recursive deletion receive unresolved `\\?\` paths. The recursive-delete form matches the recorded Windows probe.

All resolved comparisons pass through `_resolved`, which strips `\\?\`/`\\?\UNC\` before returning a `Path`, at [retention.py:116](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a215a8533ef9de243/src/id_detector/retention.py:116). Remaining `relative_to`/`is_relative_to` operations are lexical comparisons between ordinary, unprefixed paths.

## D — Test quality

The three narrow regressions are strong and would detect the relevant production reversions as described in section A. The lock test correctly pins existing `jobs.py` behavior rather than claiming a production fix.

No pytest configuration, `--basetemp`, `addopts`, or fixture shortening changed. Realistic-path tests explicitly assert operands or directories exceed 260 characters on Windows.

The appended record reports 52 retention tests and the complete suite passing under the default temp root with no retention skips. I could not run `uv` or pytest in this read-only sandbox, so I did not independently verify those execution results.

## E — Scope

`git status --short` shows only:

- `src/id_detector/retention.py`
- `tests/test_phase2b_retention.py`
- untracked `docs/reviews/followup-longpath-retention.md`

`io.py`, `jobs.py`, dependencies, configuration, `data/`, `work/`, and every named second-session file are untouched. `git diff --check` is clean.

## Required fixes

### Realistic

None.

### Adversarial

None required for this cycle; the two accepted, documented TOCTOU risks above would require handle-relative Win32 operations to eliminate.

VERDICT: OK_TO_COMMIT