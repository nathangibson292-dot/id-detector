## A — Audit of reproduction verdicts

| Verdict in fixer record | Audit |
|---|---|
| Manifest omission: **YES** | Credible. The tests explicitly assert the sidecar and raw file exceed 260 characters, exercising the old `rglob`/`is_file` path. |
| Sidecar not sealed: **YES** | Credible for the same reason: the affected `.done.json` is explicitly over 260 characters. |
| Trash purge fails: **YES** | Credible. Although the dated-directory argument itself is short, `rmtree` must recursively traverse descendants explicitly seeded beyond 260 characters. |
| Dry-run/apply divergence: **YES** | Credible: the old apply path reaches that same unsafe recursive purge. |
| Media directory treated absent: **YES** | Credible. The test explicitly asserts `len(str(media)) > 260` before calling GC. |
| Bundle/original not treated missing: **NO** | Not disproved by the test. With the documented default temp layout, the media directory is about 222–223 characters, `present/bundles` about 238–239, and `ingest/original.wav` about 242–243. The deep `index.html` exceeds 260, but old production calls `iterdir()` on the short `bundles` parent. The original check is safe by code inspection (`path_is_file`), not because this test exercises it beyond 260. |
| Move to trash fails: **NO** | Not adequately disproved. The whole-media source and destination in the purge test are below 260; the tests only guarantee that descendants are long. The one test guaranteeing a media directory over 260 fails the old implementation at its dry-run assertion, before its apply/move call. Also, “short relative targets” says nothing about the full absolute destination passed to `exists` or `replace`. |

## B — Correctness and safety bugs

1. **P0 — Rename subjects and destinations are resolved before mutation.**  
   [`retention.py:376`](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a215a8533ef9de243/src/id_detector/retention.py:376)–381 still passes both `os.replace` operands through `native_path`, which resolves symlinks and junctions. `_targets` only rejects targets resolving outside the media directory; an in-media file symlink or junction is accepted and its target is moved instead of refusing the link. More seriously, a dangling symlink at the calculated trash destination is considered absent by `_exists`, then `native_path(destination)` resolves to its possibly external target and moves the owner’s file there. `os.makedirs` also runs before the destination-parent containment check, so an existing reparse-point ancestor can cause directories to be created outside trash before GC raises.

2. **P0 — Sidecar and manifest writes can follow file symlinks outside the work root.**  
   [`retention.py:140`](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a215a8533ef9de243/src/id_detector/retention.py:140)–147 uses `entry.is_file()` with its default `follow_symlinks=True`. A `*.done.json` symlink is consequently passed to the rewrite at [`retention.py:311`](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a215a8533ef9de243/src/id_detector/retention.py:311)–347, where `atomic_write_json` resolves it and can overwrite an external file. Likewise, [`retention.py:434`](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a215a8533ef9de243/src/id_detector/retention.py:434)–449 does not reject a symlinked `retention-manifest.json`. These mutation targets receive no containment or reparse-point validation.

3. **P0 — Purge containment is not revalidated at mutation time.**  
   Purges are planned at [`retention.py:452`](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a215a8533ef9de243/src/id_detector/retention.py:452)–465, potentially long before execution at lines 559–565. The final guard checks only whether the dated path itself is a link. If `.trash` is replaced by a junction after planning, the dated child can appear as an ordinary directory: `_is_dir` follows the intermediate junction, `_is_link` returns false for the final child, and `rmtree` deletes the external dated tree.

4. **P1 — An unreadable subtree is silently omitted.**  
   [`retention.py:147`](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a215a8533ef9de243/src/id_detector/retention.py:147)–149 skips permission-denied directories. That can omit sidecars before their upstream moves and under-report the supposedly complete manifest—the same unsafe “silent omission” pattern this follow-up addresses. GC should fail closed for that media, not continue its mutations.

The ordinary-path invariants otherwise remain intact: `_TRASH_GRACE` makes the youngest purged entry provably seven days old; source-level dot directories still exclude `.idea` and `.trash`; reference `OSError`s fail closed; and `_resolved` strips the extended prefix before all `relative_to`, `is_relative_to`, and equality comparisons.

## C — Remaining long-path-unsafe calls

- Retention’s existence, stat, listing, traversal, move, directory creation, and recursive-delete calls now receive extended paths. The recursive `rmtree(_extended(...))` form is consistent with the fixer’s Windows probe, though I could not independently execute it.
- GC constructs `ProcessLock` at [`retention.py:534`](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a215a8533ef9de243/src/id_detector/retention.py:534), whose constructor still performs plain `Path.resolve()` twice at [`jobs.py:48`](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a215a8533ef9de243/src/id_detector/jobs.py:48)–53. On a long `.media.lock` path this can stop canonicalizing at MAX_PATH. The long-media test proves only that an uncontended mutex can be acquired; it does not prove that GC and an active pipeline resolve an aliased/junctioned long path to the same mutex key.
- `native_path` makes `os.replace` length-safe but is semantically unsafe for rename subjects, as described in B1.

## D — Test quality

- No `--basetemp`, fixture shortening, or pytest configuration change appears in the diff. The existing `pyproject.toml` `addopts` is unchanged and does not shorten paths.
- Seeding and assertions were strengthened: atomic writes and extended-path existence checks remove previously vacuous assertions. No existing retention assertion was weakened.
- Five of the six new long-path tests statically pin a reverted production failure, matching the recorded old-code result. `test_long_paths_published_bundle_blocks_collection` intentionally passes against old production and therefore does not pin this fix.
- The bundle test needs the actual `present/bundles` directory—not merely its child `index.html`—asserted over 260 characters. There are also no regressions for linked move sources, dangling/link-containing trash destinations, linked sidecars/manifests, mutation-time trash-root replacement, or long-path lock contention.
- I could not run `uv` or pytest in this read-only sandbox. Suite-pass claims are therefore the fixer’s recorded evidence, not independently reproduced here.

## E — Scope

- HEAD is `ec32f9726146ed4602e7c44b25442a6ffdf18ab1`.
- Tracked changes are only `src/id_detector/retention.py` and `tests/test_phase2b_retention.py`; the sole untracked file is `docs/reviews/followup-longpath-retention.md`.
- `src/id_detector/io.py` is untouched. There is no dependency/config change, nothing under `data/` or `work/`, and none of the listed playlist/UI-session files changed.
- `git diff --check` reported no whitespace errors.

## Required fixes

1. **P0:** Make `_move_to_trash` reject linked/reparse-point sources and every existing destination component, use unresolved extended operands for `os.replace`, use a lexical-existence check for collisions, and validate containment before any `makedirs`; add source-link and dangling external-destination regressions.
2. **P0:** Exclude file symlinks/reparse points from `_files` and validate every sidecar and manifest output immediately before writing; add tests proving external link targets remain byte-identical.
3. **P0:** Immediately before each purge, revalidate both `.trash` and the dated path as unresolved, non-reparse directories contained in the expected trash root; add an intermediate-junction replacement regression.
4. **P1:** Make traversal errors fail closed for that media instead of silently omitting the subtree, and test that no target moves and no incomplete manifest is written.
5. **P1:** Replace `ProcessLock`’s plain long-path `Path.resolve()` canonicalization or otherwise prove equivalent canonical mutex keys; add a greater-than-260-character contention test using two path spellings.
6. **P1:** Strengthen the two “did not reproduce” cases so the exact plain call or move operand is explicitly over 260 characters, rerun them against old production, and correct the review record to distinguish code inspection from demonstrated reproduction.

VERDICT: FIX_FIRST