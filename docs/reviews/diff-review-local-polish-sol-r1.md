## A — Progress bar

`LocalWorker.run_once()` genuinely calls `_restarted()` for attempts after the first ([local.py:670](src/idea_web/jobs/local.py:670)). The restart folds prior carry, completed phases, and the open phase up to the last persisted evaluation into one value ([progress.py:109](src/idea_web/progress.py:109)), while preserving window counts and both progress high-water marks ([local.py:700](src/idea_web/jobs/local.py:700)).

The existing progress calculation remains monotonic, clamps running jobs to 99%, and reaches 100 only on success ([jobs.py:380](src/id_detector/webapp/jobs.py:380)). Because carry ends at the previous document’s `progress_at` and the new phase clock starts on restart, overlapping attempt time is not double-counted. A worsened ETA can temporarily hold the high-water floor, but fresh elapsed time continues accumulating.

The regression test’s `carried_seconds == 207` assertion ([test_local_queue.py:126](tests/idea_web/test_local_queue.py:126)) becomes zero under the reverted implementation, so it does fail for the intended reason.

## B — Untimed hints

The identity guard is correctly shared with positioned hints: both use `hint_label_corroborates()` ([identity.py:502](src/id_detector/fuse/identity.py:502)), and the mix-wide recognised-work roots are counted before attachment; more than one is vetoed ([identity.py:650](src/id_detector/fuse/identity.py:650)).

Other guards are structurally present:

- Only audio-created plays in `plays_by_work` receive an untimed hint ([episodes.py:787](src/id_detector/fuse/episodes.py:787)); no synthetic episode is constructed.
- Crowd-only construction still requires a real position range ([episodes.py:450](src/id_detector/fuse/episodes.py:450)).
- Untimed hints are excluded from the calibration boundary model ([episodes.py:963](src/id_detector/fuse/episodes.py:963)), so they do not alter support or timing.
- They remain indirect and therefore cannot override `scatter` or `contradicted` ([episodes.py:1132](src/id_detector/fuse/episodes.py:1132)).

However, the implementation gives an untimed hint to **every occurrence** of the matched work ([episodes.py:793](src/id_detector/fuse/episodes.py:793)). The generic `hint_supported` exemption then makes every short occurrence publishable ([exports.py:207](src/id_detector/present/exports.py:207)).

A read-only replay confirms 18/18 hints attach, 10 episodes gain support, and rows move 40→42. But one added row is wrong: the second `MPH — One Sixty` fragment has evidence at 47:42–47:54, where the committed timed tracklist says `Champion & MPH — Badderman Refix`; `One Sixty` occurs only at the opening ([mph-youtube-set.txt:6](data/corpus/release-1/tracklists/mph-youtube-set.txt:6), [mph-youtube-set.txt:34](data/corpus/release-1/tracklists/mph-youtube-set.txt:34)). Its exported start/end are also reversed, 47:54–47:42. `Jah War` is the valid new row.

The report missed this because its cited work matcher explicitly ignores time and permits multiple predictions to map to the same truth work ([score_corpus.py:528](scripts/score_corpus.py:528)). Thus an unchanged unmatched-work list does not establish that no wrong occurrence appeared.

## C — Refusal wording

Exact owner-visible wording:

- Free: “BENWAL: The result was left as it is because its stored records disagree with each other: the final generation reference disagrees with its generation sidecar. Re-running this one is free: it costs nothing, but it will take time.”
- Deep: “Speed Garage & Bass Mix - Holly Olivia (March 26): The result was left as it is because it is a paid (Deep) result, and the windows its second opinion checked were chosen by the older fusion rules, so it cannot simply be rebuilt. Warning: re-running this one will spend real AudD credit.”
- Unknown: “Unknown recipe mix: The result was left as it is because the legacy invocation journal is malformed or truncated; without trustworthy legacy metadata it is not safe to assume the stored result was Free. Warning: its recipe could not be determined, so ID'er must treat it as paid; re-running this one may spend real AudD credit.”

Recipe conflicts and omissions resolve to `unknown`, while any proved Deep record remains protected ([refusion.py:127](src/id_detector/refusion.py:127)). Startup logging and the later report both call the same formatter ([server.py:52](src/idea_web/server.py:52), [server.py:149](src/idea_web/server.py:149)); the browser status renders those same lines. The wording is conservative without presenting the deliberate refusal as a failure.

## D — CLI commands

The three additions are thin adapters into `idea_web.backup.main()` ([cli.py:142](src/id_detector/cli.py:142)); no snapshot logic was duplicated.

Existing safeguards remain authoritative:

- Backup destinations are checked bidirectionally against the work tree and database directory and against corpus ancestry ([backup.py:704](src/idea_web/backup.py:704)).
- Restore requires its database inside the locked work root, takes the restore and supervisor locks, and refuses a running tool ([backup.py:1514](src/idea_web/backup.py:1514)).
- Verification returns status 1 when problems exist, and the wrapper preserves that exit status ([backup.py:1704](src/idea_web/backup.py:1704)).
- No wrapper argument bypasses module validation.

The rendered help accurately says backup excludes audio/corpus writes, restore excludes audio/truth corpus, and verification changes neither work, data nor snapshot.

## E — Scope and publication safety

`HEAD` is exactly `e591971`; only the reported 17 files and the new review report are present. `git diff --check` is clean. `data/corpus/`, `work/`, `profiles/`, the Local Free golden, and every named second-session file have no diff. Timestamp inspection found no owner-cache or corpus file newer than the builder’s recorded start.

The pass does not modify reservation, paid dispatch, settlement authority, or certification controls. Deep re-fusion remains refused, and the added CLI commands cannot analyse or dispatch. Changed assertions are exact fusion-version updates or stronger new checks; none was weakened. No token, key, private URL, absolute personal path, or other unsuitable publication content was added.

## Required fixes

### Realistic

- **P1 — Prevent an untimed work hint from publishing every occurrence of that work.** The current loop surfaces a known-wrong, malformed second `One Sixty` row. Add an occurrence-ambiguity policy or otherwise prevent an untimed hint from granting the short-row exemption indiscriminately. Add a regression with one work recognised in two separated occurrences, only one of which is present in the timed truth, and correct the report’s “no wrong row” claim.

### Adversarial

- None.

## Follow-ups, not blockers

- Extend the progress regression through a persisted queue-row round trip and advance the replacement attempt’s clock; the implementation is sound, but the current test directly exercises `_restarted()` only.
- The report says the restart carries the original start timestamp, but `JobManager._execute()` overwrites `started_at` ([jobs.py:823](src/id_detector/webapp/jobs.py:823)). This does not break elapsed carry, but the report should not claim otherwise.

VERDICT: FIX_FIRST
