# Local cost preview and paid measurement build

Base: `main` at `723f495`, detached worktree at
`C:/Users/natha/Documents/Music/id-detector-cost-preview`. No branch and no commit.

## 1. Read-only `idea cost`

`idea cost <source-url-or-media-key> --work-root <cached-work>` reads the stored
source and decode record. It neither rebuilds the index nor fetches, decodes,
reserves or calls a provider. Uncached or missing decode length returns exit 2
with `Length unknown. Supply --minutes to estimate; nothing was fetched or spent.`
`--minutes` is an explicit estimate; `--density 1|2`, `--config` and `--profile`
resolve the same schedule preferences as analyse. There is no implicit download option.

The request count uses the existing generation-zero scheduler, including its tail
window. Dollars use `AppConfig.load` and therefore `pricing.toml`; no new price is
hard-coded. It shows the recipe ceiling, configured per-run cap, effective cap,
105% reservation rounded up to cents, stored recipe/status, and the density-2
alternative. A stored scan is history, not a claim that its result is compatible
or that a new scan is free. Duplicate audio, cached responses and outcomes can
reduce actual spending. An over-cap estimate says it will be refused.

Actual command output for the cached Boomtown mix (81.05 minutes):

```text
Mix length: 1:21:03 (81.05 minutes).
Deep at density 1: approximately 541 paid clips, estimated $2.71.
Density 2: approximately 271 paid clips, estimated $1.35.
Recipe ceiling: $9.00; configured per-run cap: none; effective cap: $9.00.
Reservation including 5% headroom: $2.85.
Stored scan: free (complete).
Estimate only, not a promise: cached answers, duplicate clips and provider outcomes can change the spend.
```

## 2. CLI confirmation before reservation

Only `cli.analyse` installs `PipelineOptions.cli_paid_confirm`; the service passes
it only in local mode, and the pipeline calls it only for a fresh Deep reservation.
Free never installs or calls it. Default service/worker callers install nothing;
hosted mode ignores it. Existing hosted paid refusal and money authority code are
unchanged. `--yes` bypasses input, but still shows the estimate when paid work is
about to start. A compatible cache hit needs no paid prompt.

The callback runs after length/windows are known and before `reserve_usd`,
`record_reservation` and dispatch. Decline/EOF/noninteractive input returns
`cancelled` / `paid_not_confirmed`, exit 130, without an invocation settlement,
reservation or attempt. Noninteractive input explains that `--yes` is required.
Existing durable reservations resume under their original authorisation and price.
Recovered spend/attempts also bypass this fresh-run gate, so a broken reservation
cannot make a declined prompt hide money the existing recovery path must account for.
No SQLite claim, lease, cancellation fence, ambiguous-attempt rule or settlement
implementation was changed.

Exact prompt rendered using that real stored duration and an injected `n` answer
(no analysis or provider call was performed for this transcript):

```text
Mix length: 1:21:03 (81.05 minutes).
Deep at density 1: approximately 541 paid clips, estimated $2.71.
Density 2: approximately 271 paid clips, estimated $1.35.
Recipe ceiling: $9.00; configured per-run cap: none; effective cap: $9.00.
Reservation including 5% headroom: $2.85.
Stored scan: free (complete).
Estimate only, not a promise: cached answers, duplicate clips and provider outcomes can change the spend.
Spend AudD credit on this Deep scan? [y/N]: n
cancelled: paid scan not confirmed; nothing reserved or spent.
```

## 3. Budgeted Free/Deep comparison

`scripts/compare_deep.py` consumes a Free `score_corpus.py` run list, optionally
selecting repeatable `--mix` names. It requires `--budget` in dollars and a separate
`--scan-root` outside this checkout and the input work tree. The default is read-only
and prints each mix estimate plus the total. `--spend` is the deliberate spending
switch. A plan whose estimate **or reservation headroom** exceeds the hard budget
is refused before creating the scan root. Recipe/config caps are also checked. The existing scorer validates the whole Free
baseline before any scan begins, so broken or mismatched saved inputs cannot cause
spending followed by an unusable comparison.

Example (paths are examples, not commands run during this build):

```text
uv run python scripts/compare_deep.py --run-list C:/idea-measure/free-runs.json --work-root C:/Music/id-detector/work --scan-root C:/idea-deep-scan --budget 20
```

Add `--spend` only when ready to pay; `--density 2` lowers the primary density. For `idea analyse`, set
`primary_density = 2` in the config's `[deep]` table; `idea cost --density 2` alone
only previews that choice.
The run list has the existing scorer shape: `{"recipe":"free","runs":[{"mix_id":
"boomtown","truth":".../ground_truth.json","episodes":".../episodes.json",
"identities":".../presentation-identities.json","media_key":"<64 hex>"}]}`.
Its baseline remains in the read-only source tree. Each selected media directory
is copied through a temporary directory before atomic rename into the scan root.
Keep the scan root, selection and density to resume. Its `.idea/app.db` is the
existing SQLite queue, not a parallel spend ledger. Stable job identities prevent
completed, partial or cancelled mixes being submitted again. Interrupted jobs keep
their run ID, original reservation, attempt ledger and checkpoint recovery.

Each mix gets a fixed cap within the prechecked total; the original reservation
is included on resume even if pricing changes. The runner holds the worker
supervisor lock and rechecks durable reservations under it before running. Existing
worker claim/lease/run/cancel fences admit every paid request. It reports cumulative
spend including ambiguous requests and each terminal/waiting status. A waiting or
interrupted job stops the invocation; unpaired mixes are named. A cancelled or
failed terminal job is not automatically retried or re-paid. There is no automatic
budget refill. Selection/recipe changes in an existing scan root are refused.

Comparisons use the job's exact result bundle, including a labelled partial run,
not the current pointer (which may still be Free). Only paired usable results enter
pooled counts. `score_corpus.py` owns filtering, work matching, likely precision and
pooling. The named gains are matched truth rows present in Deep but absent in Free,
not merely different provider spellings. These are development, work-only scores;
unreleased tracks do not become proven matches just because another engine names them.

Worked synthetic example: the Free fixture omits Alpha and Theta from the existing
mini-corpus Deep output. The pooled percentages come from counts, not an average
of mix percentages:

```text
Development comparison; work-only matching, not certification.
Mix | Recipe | Work recall | Work precision | Likely precision
--- | --- | --- | --- | ---
mini-a | Free | 66.7% | 50.0% | 100.0%
mini-a | Deep | 100.0% | 60.0% | 100.0%
mini-b | Free | 0.0% | 0.0% | 0.0%
mini-b | Deep | 33.3% | 50.0% | 50.0%
Pooled | Free | 33.3% | 40.0% | 66.7%
Pooled | Deep | 66.7% | 57.1% | 80.0%
Deep found that Free missed (truth-matched names):
mini-a: Mini Artist - Alpha
mini-b: Mini Artist - Theta
```

## 4. Kept offline fusion measurement

`scripts/measure_refusion.py --work-root <cached-work> --run-list <score-run-list>`
loads one stored snapshot and its checksum-proven observations/windows/hints,
builds the current identity graph and episodes in memory, and scores before/after
through `score_corpus.py`. All derived files are in an OS temporary directory.
It prints eligible attached hints, all unique attached hint IDs, supported episodes,
listed rows, and pooled scored recall/precision/likely precision. Aliases are counted
once. Missing, inconsistent or calibrated inputs are reported as skipped; no
recognition, hint fetching, cache repair or publication occurs. Deep results measure
only their stored evidence, not a hypothetical different secondary selection.

Read-only measurement of the owner's current cache:

```text
Offline measurement; cached files are read-only.
Media | Eligible hints | All attached hints | Supported episodes | Listed rows
--- | --- | --- | --- | ---
84232b822c6a | 4 -> 11 | 4 -> 11 | 4 -> 11 | 22 -> 22
6c00a79d755e | 9 -> 10 | 9 -> 10 | 9 -> 10 | 22 -> 22
de0f005862e7 | 0 -> 0 | 0 -> 0 | 0 -> 0 | 32 -> 33
fcc323605143 | 1 -> 1 | 1 -> 1 | 1 -> 1 | 34 -> 34
6651118675bd | 23 -> 61 | 23 -> 61 | 13 -> 29 | 32 -> 29
ed4ca55359f9 | 1 -> 16 | 1 -> 16 | 1 -> 8 | 42 -> 41
91a0554a7d03 | 0 -> 0 | 0 -> 0 | 0 -> 0 | 18 -> 18
819e1ee83956: SKIPPED: its stored records disagree with each other: the final generation reference disagrees with its generation sidecar
99b8ceebb852 | 9 -> 12 | 9 -> 12 | 7 -> 10 | 26 -> 28
a53c5eed4b59 | 7 -> 7 | 7 -> 7 | 7 -> 7 | 30 -> 33
354fbd61f5bb | 0 -> 0 | 0 -> 0 | 0 -> 0 | 33 -> 33
d3252340303c: SKIPPED: d3252340303c: present does not describe 3 candidate(s) this run's episodes name (e.g. 5ae1376c9ebd02655690c2323f782134cafe6508); it is not the identity graph generation 0 was fused from - check the run directory or name the right file with "identities"
ec0a562ab791 | 0 -> 0 | 0 -> 0 | 0 -> 0 | 10 -> 10
c3121055e46f | 0 -> 0 | 0 -> 0 | 0 -> 0 | 16 -> 16
Total | 54 -> 118 | 54 -> 118 | 42 -> 76 | 317 -> 319
Before: 6 truth mixes, work matching; recall 60.7%; precision 72.5%; likely precision 88.5%.
After: 6 truth mixes, work matching; recall 65.7%; precision 72.1%; likely precision 97.9%.
Deep re-fusion measures its stored evidence only; it does not simulate a new secondary selection. Development scores, not certification.
```

These are **stored-before versus current-code** numbers, not a reproduction of the
isolated fusion-3/fusion-4 ablation in the polish report. Two unsafe inputs are
explicitly excluded; only six of seven truth mixes could be paired, as printed.
No seven-mix accuracy claim is made.

## Tests and negative controls

`tests/test_cost_preview.py`: 14 cases. Tests exercise price/schedule/density/caps;
URL/key cache lookup and timestamp preservation; unknown length and explicit
minutes; interactive yes/no, noninteractive refusal, `--yes`, no reservation/attempt/
settlement on refusal; CLI-only wiring and Free/hosted exclusion; total-budget
refusal and parser-default dry run; crash recovery after paid dispatch, cancel,
no duplicate jobs/settlements or re-payment; exact pooled fixture scores and named
gains; and read-only re-fusion with scorer output.

The standalone new-test run passed: `13 passed, 1 warning in 30.01s`.
An additional regression removes every reservation copy after a paid interruption;
the prompt must not hide the already dispatched spend. It passed in 3.19s, and
removing just that protection failed in 3.47s. The first fault injection retained
a redundant reservation copy and did not reach this branch; the corrected injection
removes the SQLite row, checkpoint reservation and JSON projection.
A preceding integration run including the service tests passed:
`32 passed, 1 warning in 82.60s` (before adding the cancel parameter).
Initial development failures caught a Windows long-path copy error, an incorrect
fixture artist expectation and selecting the current Free pointer for a partial
Deep result. All were fixed before final suite validation.

Every new case has a failing targeted negative control. Production files were
saved byte-for-byte, altered temporarily, tested in foreground with fake providers,
and restored in `finally`. These are surgical removals/mutations, not a claim that
all new modules can be deleted while their tests still import. Exact controls:

| Control | Removed or changed behavior | Result |
|---|---|---|
| preview | Request count off by one | 3 failed |
| confirmation | Disabled the pre-reservation confirmation gate | 4 failed |
| cli_only | Removed CLI callback wiring | 1 failed |
| budget | Removed pre-start aggregate budget refusal | 1 failed |
| baseline | Removed the Free baseline scorer preflight | 1 failed |
| dry_run | Removed the dry-run early return | 1 failed |
| resume | Re-enqueued an existing durable job | 2 failed |
| table | Perturbed the rendered scorer percentages | 1 failed |
| measurement | Removed before/after scoring | 1 failed |
| recovered_money | Removed the recovered-money exclusion from confirmation | 1 failed |

The budget/dry-run case was run three times, once for each independent guard; 16 expected
failures cover all 14 new cases. No live provider is used even by the deliberately
broken variants. Full outputs follow:

### budget

```text
F                                                                        [100%]
================================== FAILURES ===================================
_____________ test_comparison_budget_refusal_and_default_dry_run ______________
tests\test_cost_preview.py:209: in test_comparison_budget_refusal_and_default_dry_run
    assert not args.scan_root.exists()
E   AssertionError: assert not True
E    +  where True = exists()
E    +    where exists = WindowsPath('C:/Users/natha/AppData/Local/Temp/pytest-of-natha/pytest-1998/test_comparison_budget_refusal0/experiment').exists
E    +      where WindowsPath('C:/Users/natha/AppData/Local/Temp/pytest-of-natha/pytest-1998/test_comparison_budget_refusal0/experiment') = namespace(work_root=WindowsPath('C:/Users/natha/AppData/Local/Temp/pytest-of-natha/pytest-1998/test_comparison_budget_.../Temp/pytest-of-natha/pytest-1998/test_comparison_budget_refusal0/runs.json'), budget=1, density=1, mix=[], spend=True).scan_root
---------------------------- Captured stdout setup ----------------------------
complete; 7 matches; 0 failures; 7 physical attempts; 1 generations (stop=max_generations); 3 episodes; tracklist=\\?\C:\Users\natha\AppData\Local\Temp\pytest-of-natha\pytest-1998\test_comparison_budget_refusal0\original\32a64e2436bf48b679fe4ce4be9f82245faf26976ad9de7a34b0b3ccbacc478e\e2179d997e03bdd132bbd451535df92691a581bdeb9b0a6bb73358b54b08575d\present\bundles\cf23adc833781e8cbddedcb2fe7012a5dee9f401d189c0687f29ae1813f4aefa\tracklist.json
============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector-cost-preview\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ===========================
FAILED tests/test_cost_preview.py::test_comparison_budget_refusal_and_default_dry_run
1 failed, 1 warning in 3.84s
```

### cli_only

```text
F                                                                        [100%]
================================== FAILURES ===================================
________________ test_gate_is_cli_only_and_free_never_prompts _________________
tests\test_cost_preview.py:141: in test_gate_is_cli_only_and_free_never_prompts
    assert callable(seen[-1]["cli_paid_confirm"])
                    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
E   KeyError: 'cli_paid_confirm'
============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector-cost-preview\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ===========================
FAILED tests/test_cost_preview.py::test_gate_is_cli_only_and_free_never_prompts
1 failed, 1 warning in 2.13s
```

### confirmation

```text
FFFF                                                                     [100%]
================================== FAILURES ===================================
________ test_cli_confirmation_before_reservation[False--False-False] _________
tests\test_cost_preview.py:118: in test_cli_confirmation_before_reservation
    assert result.exit_code == (0 if paid else 130), (result.output, result.exception)
E   AssertionError: ('complete; 6 matches; 0 failures; 2 physical attempts; 1 generations (stop=max_generations); 3 episodes; tracklist=\\...54b08575d\\present\\bundles\\f171a2a49fad8de9631f2221abda1996b2361f8e70337c21c70f15fc65ff4bdf\\tracklist.json
E     ', None)
E   assert 0 == 130
E    +  where 0 = <Result okay>.exit_code
_______ test_cli_confirmation_before_reservation[True-n\n-False-False] ________
tests\test_cost_preview.py:118: in test_cli_confirmation_before_reservation
    assert result.exit_code == (0 if paid else 130), (result.output, result.exception)
E   AssertionError: ('complete; 6 matches; 0 failures; 2 physical attempts; 1 generations (stop=max_generations); 3 episodes; tracklist=\\...54b08575d\\present\\bundles\\1913311b6f87b57d4452f2189271afddc7213bc06c7b5f05e5d55736b8df7fb3\\tracklist.json
E     ', None)
E   assert 0 == 130
E    +  where 0 = <Result okay>.exit_code
________ test_cli_confirmation_before_reservation[True-y\n-False-True] ________
tests\test_cost_preview.py:119: in test_cli_confirmation_before_reservation
    assert "estimated $" in result.output
E   AssertionError: assert 'estimated $' in 'complete; 6 matches; 0 failures; 2 physical attempts; 1 generations (stop=max_generations); 3 episodes; tracklist=\\\...b73358b54b08575d\\present\\bundles\\c546c308c0e55481feaac8d48a3afad4308d4fcd7efe078efc3d668a294d75be\\tracklist.json\n'
E    +  where 'complete; 6 matches; 0 failures; 2 physical attempts; 1 generations (stop=max_generations); 3 episodes; tracklist=\\\...b73358b54b08575d\\present\\bundles\\c546c308c0e55481feaac8d48a3afad4308d4fcd7efe078efc3d668a294d75be\\tracklist.json\n' = <Result okay>.output
_________ test_cli_confirmation_before_reservation[False--True-True] __________
tests\test_cost_preview.py:119: in test_cli_confirmation_before_reservation
    assert "estimated $" in result.output
E   AssertionError: assert 'estimated $' in 'complete; 6 matches; 0 failures; 2 physical attempts; 1 generations (stop=max_generations); 3 episodes; tracklist=\\\...b73358b54b08575d\\present\\bundles\\36ea453b4c165e1307c799acf9b350d5076d1ecd36e3c7c93779d1e6e6d61cb0\\tracklist.json\n'
E    +  where 'complete; 6 matches; 0 failures; 2 physical attempts; 1 generations (stop=max_generations); 3 episodes; tracklist=\\\...b73358b54b08575d\\present\\bundles\\36ea453b4c165e1307c799acf9b350d5076d1ecd36e3c7c93779d1e6e6d61cb0\\tracklist.json\n' = <Result okay>.output
============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector-cost-preview\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ===========================
FAILED tests/test_cost_preview.py::test_cli_confirmation_before_reservation[False--False-False]
FAILED tests/test_cost_preview.py::test_cli_confirmation_before_reservation[True-n\n-False-False]
FAILED tests/test_cost_preview.py::test_cli_confirmation_before_reservation[True-y\n-False-True]
FAILED tests/test_cost_preview.py::test_cli_confirmation_before_reservation[False--True-True]
4 failed, 1 warning in 16.71s
```

### dry_run

```text
F                                                                        [100%]
================================== FAILURES ===================================
_____________ test_comparison_budget_refusal_and_default_dry_run ______________
tests\test_cost_preview.py:229: in test_comparison_budget_refusal_and_default_dry_run
    assert "DRY RUN" in capsys.readouterr().out
E   AssertionError: assert 'DRY RUN' in 'tone: 7 clips, estimate $0.04; reservation $0.04; not started\nTotal estimate: $0.04; conservative budget required: $...oled | Deep | 0.0% | 0.0% | 0.0%\nDeep found that Free missed (truth-matched names):\ntone: none\nNot compared: none\n'
E    +  where 'tone: 7 clips, estimate $0.04; reservation $0.04; not started\nTotal estimate: $0.04; conservative budget required: $...oled | Deep | 0.0% | 0.0% | 0.0%\nDeep found that Free missed (truth-matched names):\ntone: none\nNot compared: none\n' = CaptureResult(out='tone: 7 clips, estimate $0.04; reservation $0.04; not started\nTotal estimate: $0.04; conservative ...ep | 0.0% | 0.0% | 0.0%\nDeep found that Free missed (truth-matched names):\ntone: none\nNot compared: none\n', err='').out
E    +    where CaptureResult(out='tone: 7 clips, estimate $0.04; reservation $0.04; not started\nTotal estimate: $0.04; conservative ...ep | 0.0% | 0.0% | 0.0%\nDeep found that Free missed (truth-matched names):\ntone: none\nNot compared: none\n', err='') = readouterr()
E    +      where readouterr = <_pytest.capture.CaptureFixture object at 0x0000023EE8115B20>.readouterr
---------------------------- Captured stdout setup ----------------------------
complete; 7 matches; 0 failures; 7 physical attempts; 1 generations (stop=max_generations); 3 episodes; tracklist=\\?\C:\Users\natha\AppData\Local\Temp\pytest-of-natha\pytest-1999\test_comparison_budget_refusal0\original\32a64e2436bf48b679fe4ce4be9f82245faf26976ad9de7a34b0b3ccbacc478e\e2179d997e03bdd132bbd451535df92691a581bdeb9b0a6bb73358b54b08575d\present\bundles\cf23adc833781e8cbddedcb2fe7012a5dee9f401d189c0687f29ae1813f4aefa\tracklist.json
============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector-cost-preview\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ===========================
FAILED tests/test_cost_preview.py::test_comparison_budget_refusal_and_default_dry_run
1 failed, 1 warning in 8.15s
```

### measurement

```text
F                                                                        [100%]
================================== FAILURES ===================================
_____________________ test_refusion_measurement_read_only _____________________
tests\test_cost_preview.py:315: in test_refusion_measurement_read_only
    assert "Before: 1 truth mixes" in output and "After: 1 truth mixes" in output
E   AssertionError: assert ('Before: 1 truth mixes' in 'Offline measurement; cached files are read-only.\nMedia | Eligible hints | All attached hints | Supported episodes | ...sures its stored evidence only; it does not simulate a new secondary selection. Development scores, not certification.')
---------------------------- Captured stdout setup ----------------------------
complete; 7 matches; 0 failures; 7 physical attempts; 1 generations (stop=max_generations); 3 episodes; tracklist=\\?\C:\Users\natha\AppData\Local\Temp\pytest-of-natha\pytest-2002\test_refusion_measurement_read0\original\32a64e2436bf48b679fe4ce4be9f82245faf26976ad9de7a34b0b3ccbacc478e\e2179d997e03bdd132bbd451535df92691a581bdeb9b0a6bb73358b54b08575d\present\bundles\cf23adc833781e8cbddedcb2fe7012a5dee9f401d189c0687f29ae1813f4aefa\tracklist.json
============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector-cost-preview\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ===========================
FAILED tests/test_cost_preview.py::test_refusion_measurement_read_only - Asse...
1 failed, 1 warning in 3.97s
```

### preview

```text
FFF                                                                      [100%]
================================== FAILURES ===================================
_____________ test_preview_uses_pricing_and_schedule_and_density ______________
tests\test_cost_preview.py:55: in test_preview_uses_pricing_and_schedule_and_density
    assert preview.clips == 400
E   assert 401 == 400
E    +  where 401 = CostEstimate(duration_ms=3600000, windows=401, density=1, unit_usd_e6=5000, ceiling_e2=900, configured_cap_e2=None).clips
___________________ test_cost_cached_key_and_url_read_only ____________________
tests\test_cost_preview.py:69: in test_cost_cached_key_and_url_read_only
    assert "0:01:00" in result.output and "7 paid clips" in result.output
E   AssertionError: assert ('0:01:00' in 'Mix length: 0:01:00 (1.00 minutes).\nDeep at density 1: approximately 8 paid clips, estimated $0.04.\nDensity 2: appr...omplete).\nEstimate only, not a promise: cached answers, duplicate clips and provider outcomes can change the spend.\n' and '7 paid clips' in 'Mix length: 0:01:00 (1.00 minutes).\nDeep at density 1: approximately 8 paid clips, estimated $0.04.\nDensity 2: appr...omplete).\nEstimate only, not a promise: cached answers, duplicate clips and provider outcomes can change the spend.\n')
E    +  where 'Mix length: 0:01:00 (1.00 minutes).\nDeep at density 1: approximately 8 paid clips, estimated $0.04.\nDensity 2: appr...omplete).\nEstimate only, not a promise: cached answers, duplicate clips and provider outcomes can change the spend.\n' = <Result okay>.output
E    +  and   'Mix length: 0:01:00 (1.00 minutes).\nDeep at density 1: approximately 8 paid clips, estimated $0.04.\nDensity 2: appr...omplete).\nEstimate only, not a promise: cached answers, duplicate clips and provider outcomes can change the spend.\n' = <Result okay>.output
---------------------------- Captured stdout setup ----------------------------
complete; 7 matches; 0 failures; 7 physical attempts; 1 generations (stop=max_generations); 3 episodes; tracklist=\\?\C:\Users\natha\AppData\Local\Temp\pytest-of-natha\pytest-1995\test_cost_cached_key_and_url_r0\original\32a64e2436bf48b679fe4ce4be9f82245faf26976ad9de7a34b0b3ccbacc478e\e2179d997e03bdd132bbd451535df92691a581bdeb9b0a6bb73358b54b08575d\present\bundles\cf23adc833781e8cbddedcb2fe7012a5dee9f401d189c0687f29ae1813f4aefa\tracklist.json
_________ test_unknown_length_requires_minutes_without_creating_work __________
tests\test_cost_preview.py:81: in test_unknown_length_requires_minutes_without_creating_work
    assert supplied.exit_code == 0 and "400 paid clips" in supplied.output
E   AssertionError: assert (0 == 0 and '400 paid clips' in 'Mix length: 1:00:00 (60.00 minutes).\nDeep at density 1: approximately 401 paid clips, estimated $2.00.\nDensity 2: a...ix found.\nEstimate only, not a promise: cached answers, duplicate clips and provider outcomes can change the spend.\n')
E    +  where 0 = <Result okay>.exit_code
E    +  and   'Mix length: 1:00:00 (60.00 minutes).\nDeep at density 1: approximately 401 paid clips, estimated $2.00.\nDensity 2: a...ix found.\nEstimate only, not a promise: cached answers, duplicate clips and provider outcomes can change the spend.\n' = <Result okay>.output
============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector-cost-preview\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ===========================
FAILED tests/test_cost_preview.py::test_preview_uses_pricing_and_schedule_and_density
FAILED tests/test_cost_preview.py::test_cost_cached_key_and_url_read_only - A...
FAILED tests/test_cost_preview.py::test_unknown_length_requires_minutes_without_creating_work
3 failed, 1 warning in 3.78s
```

### resume

```text
FF                                                                       [100%]
================================== FAILURES ===================================
____________ test_comparison_resume_after_paid_interruption[False] ____________
tests\test_cost_preview.py:273: in test_comparison_resume_after_paid_interruption
    compare_deep.execute(args, options=options(resumed))
scripts\compare_deep.py:213: in execute
    queue.enqueue(target, recipe, job_id=identifier)
src\idea_web\jobs\worker.py:1633: in enqueue
    connection.execute(
E   sqlite3.IntegrityError: UNIQUE constraint failed: jobs.id
---------------------------- Captured stdout setup ----------------------------
complete; 7 matches; 0 failures; 7 physical attempts; 1 generations (stop=max_generations); 3 episodes; tracklist=\\?\C:\Users\natha\AppData\Local\Temp\pytest-of-natha\pytest-2000\test_comparison_resume_after_p0\original\32a64e2436bf48b679fe4ce4be9f82245faf26976ad9de7a34b0b3ccbacc478e\e2179d997e03bdd132bbd451535df92691a581bdeb9b0a6bb73358b54b08575d\present\bundles\cf23adc833781e8cbddedcb2fe7012a5dee9f401d189c0687f29ae1813f4aefa\tracklist.json
---------------------------- Captured stdout call -----------------------------
tone: 7 clips, estimate $0.04; reservation $0.04; not started
Total estimate: $0.04; conservative budget required: $0.04; hard budget: $0.10.
tone: analysis
Experiment spent so far (including ambiguous requests): $0.020000.
Interrupted or waiting: resume with the same scan root and selection; no new job will be created.
Not compared: tone
tone: 7 clips, estimate $0.04; reservation $0.04; analysis
Total estimate: $0.04; conservative budget required: $0.04; hard budget: $0.00.
REFUSED: estimate or reservation headroom exceeds the total budget. Nothing started.
tone: 7 clips, estimate $0.04; reservation $0.04; analysis
Total estimate: $0.04; conservative budget required: $0.04; hard budget: $0.10.
____________ test_comparison_resume_after_paid_interruption[True] _____________
tests\test_cost_preview.py:273: in test_comparison_resume_after_paid_interruption
    compare_deep.execute(args, options=options(resumed))
scripts\compare_deep.py:213: in execute
    queue.enqueue(target, recipe, job_id=identifier)
src\idea_web\jobs\worker.py:1633: in enqueue
    connection.execute(
E   sqlite3.IntegrityError: UNIQUE constraint failed: jobs.id
---------------------------- Captured stdout setup ----------------------------
complete; 7 matches; 0 failures; 7 physical attempts; 1 generations (stop=max_generations); 3 episodes; tracklist=\\?\C:\Users\natha\AppData\Local\Temp\pytest-of-natha\pytest-2000\test_comparison_resume_after_p1\original\32a64e2436bf48b679fe4ce4be9f82245faf26976ad9de7a34b0b3ccbacc478e\e2179d997e03bdd132bbd451535df92691a581bdeb9b0a6bb73358b54b08575d\present\bundles\cf23adc833781e8cbddedcb2fe7012a5dee9f401d189c0687f29ae1813f4aefa\tracklist.json
---------------------------- Captured stdout call -----------------------------
tone: 7 clips, estimate $0.04; reservation $0.04; not started
Total estimate: $0.04; conservative budget required: $0.04; hard budget: $0.10.
tone: analysis
Experiment spent so far (including ambiguous requests): $0.020000.
Interrupted or waiting: resume with the same scan root and selection; no new job will be created.
Not compared: tone
tone: 7 clips, estimate $0.04; reservation $0.04; analysis
Total estimate: $0.04; conservative budget required: $0.04; hard budget: $0.00.
REFUSED: estimate or reservation headroom exceeds the total budget. Nothing started.
tone: 7 clips, estimate $0.04; reservation $0.04; analysis
Total estimate: $0.04; conservative budget required: $0.04; hard budget: $0.10.
============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector-cost-preview\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ===========================
FAILED tests/test_cost_preview.py::test_comparison_resume_after_paid_interruption[False]
FAILED tests/test_cost_preview.py::test_comparison_resume_after_paid_interruption[True]
2 failed, 1 warning in 8.33s
```

### table

```text
F                                                                        [100%]
================================== FAILURES ===================================
____________________ test_comparison_table_and_named_gains ____________________
tests\test_cost_preview.py:301: in test_comparison_table_and_named_gains
    assert "Pooled | Deep | 66.7% | 57.1% | 80.0%" in output
E   AssertionError: assert 'Pooled | Deep | 66.7% | 57.1% | 80.0%' in 'Development comparison; work-only matching, not certification.\nMix | Recipe | Work recall | Work precision | Likely ... | 81.0%\nDeep found that Free missed (truth-matched names):\nmini-a: Mini Artist - Alpha\nmini-b: Mini Artist - Theta'
============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector-cost-preview\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ===========================
FAILED tests/test_cost_preview.py::test_comparison_table_and_named_gains - As...
1 failed, 1 warning in 1.85s
```

### recovered_money

```text
F                                                                        [100%]
================================== FAILURES ===================================
________________ test_confirmation_cannot_hide_recovered_money ________________
tests\test_cost_preview.py:169: in test_confirmation_cannot_hide_recovered_money
    assert prompts == []
E   AssertionError: assert ['asked'] == []
E
E     Left contains one more item: 'asked'
E     Use -v to get more diff
============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector-cost-preview\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ===========================
FAILED tests/test_cost_preview.py::test_confirmation_cannot_hide_recovered_money
1 failed, 1 warning in 3.47s
```

### baseline

```text
F                                                                        [100%]
================================== FAILURES ===================================
_____________ test_comparison_budget_refusal_and_default_dry_run ______________
tests\test_cost_preview.py:242: in test_comparison_budget_refusal_and_default_dry_run
    assert not args.scan_root.exists()
E   AssertionError: assert not True
E    +  where True = exists()
E    +    where exists = WindowsPath('C:/Users/natha/AppData/Local/Temp/pytest-of-natha/pytest-2014/test_comparison_budget_refusal0/experiment').exists
E    +      where WindowsPath('C:/Users/natha/AppData/Local/Temp/pytest-of-natha/pytest-2014/test_comparison_budget_refusal0/experiment') = namespace(work_root=WindowsPath('C:/Users/natha/AppData/Local/Temp/pytest-of-natha/pytest-2014/test_comparison_budget_.../pytest-of-natha/pytest-2014/test_comparison_budget_refusal0/runs.json'), budget=100000, density=1, mix=[], spend=True).scan_root
---------------------------- Captured stdout setup ----------------------------
complete; 7 matches; 0 failures; 7 physical attempts; 1 generations (stop=max_generations); 3 episodes; tracklist=\\?\C:\Users\natha\AppData\Local\Temp\pytest-of-natha\pytest-2014\test_comparison_budget_refusal0\original\32a64e2436bf48b679fe4ce4be9f82245faf26976ad9de7a34b0b3ccbacc478e\e2179d997e03bdd132bbd451535df92691a581bdeb9b0a6bb73358b54b08575d\present\bundles\cf23adc833781e8cbddedcb2fe7012a5dee9f401d189c0687f29ae1813f4aefa\tracklist.json
---------------------------- Captured stdout call -----------------------------
tone: 7 clips, estimate $0.04; reservation $0.04; not started
Total estimate: $0.04; conservative budget required: $0.04; hard budget: $0.10.
complete; 6 matches; 0 failures; 0 physical attempts; 1 generations (stop=max_generations); 3 episodes; tracklist=\\?\C:\Users\natha\AppData\Local\Temp\pytest-of-natha\pytest-2014\test_comparison_budget_refusal0\experiment\32a64e2436bf48b679fe4ce4be9f82245faf26976ad9de7a34b0b3ccbacc478e\e2179d997e03bdd132bbd451535df92691a581bdeb9b0a6bb73358b54b08575d\present\bundles\a7355451747ca83898148112e55359e78c5facbf71c5740cbfa527e0734ce7b9\tracklist.json
tone: complete
Experiment spent so far (including ambiguous requests): $0.035000.
============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector-cost-preview\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ===========================
FAILED tests/test_cost_preview.py::test_comparison_budget_refusal_and_default_dry_run
1 failed, 1 warning in 13.46s
```

## Final validation

All test processes use `IDEA_TEST_MODE=1`. Every new analysis uses injected AudD and
Shazam fakes (CLI runs use `--fake-providers audd,shazam`). Real-cache measurement
processes alone set an empty AudD token and `IDEA_ENGINE_SHAZAM=off`; those switches
are not wrapped around the suite. No real source URL was analysed, no provider was
called live, no server or PowerShell gate was run. The Local Free golden, profiles,
pricing authority, playlists and all prohibited files remain unchanged.

Collection: `uv run pytest --collect-only -q -m "not live"`:
`2180/2183 tests collected (3 deselected) in 2.99s`.
Four foreground shards partition all 110 collected offline files, without overlap.
Each command is `uv run pytest -q -m "not live"` followed by the listed files.

Final totals: **2,177 passed, 3 skipped**, covering all 2,180 selected offline tests.
The 3 live tests were excluded. Every shard finished before the next started.
Shard timings were consistent with their process/audio workloads; none exhibited
a pathological stall requiring a shard rerun.

### Shard 1: 28 files

```text
tests/idea_web/test_accounts.py
tests/idea_web/test_followup_queue_money.py
tests/idea_web/test_followup_round5.py
tests/idea_web/test_followup_round9.py
tests/idea_web/test_ops.py
tests/idea_web/test_worker.py
tests/test_certification_followup.py
tests/test_crowd_id_merge.py
tests/test_followup_money_resume.py
tests/test_io_backstop_followup.py
tests/test_phase0a_money.py
tests/test_phase0b_audd.py
tests/test_phase1a_compat.py
tests/test_phase2b_retention.py
tests/test_projection.py
tests/test_score_corpus.py
tests/test_stage1_jobs.py
tests/test_stage1_wheel.py
tests/test_stage2a_truth.py
tests/test_stage2b_pipeline.py
tests/test_stage4a_parser.py
tests/test_stage4c_ablations.py
tests/test_stage4c_scanners.py
tests/test_stage7_page.py
tests/test_stage9_config.py
tests/test_truth_corpus_followup_r2.py
tests/test_truth_corpus_followup_r7.py
tests/test_truth_gateway_guard.py
```

```text
$ uv run pytest -q -m "not live" <files above>
........................................................................ [ 10%]
........................................................................ [ 21%]
........................................................................ [ 32%]
........................................................................ [ 43%]
........................................................................ [ 54%]
........................................................................ [ 65%]
........................................................................ [ 76%]
........................................................................ [ 87%]
........................................................................ [ 98%]
.............                                                            [100%]
============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector-cost-preview\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
661 passed, 1 warning in 451.28s (0:07:31)
```

### Shard 2: 28 files

```text
tests/idea_web/test_auth.py
tests/idea_web/test_followup_review_fixes.py
tests/idea_web/test_followup_round6.py
tests/idea_web/test_headers_forms.py
tests/idea_web/test_parity.py
tests/test_accuracy_fixes.py
tests/test_collapse.py
tests/test_doctor.py
tests/test_golden_local_free.py
tests/test_local_index.py
tests/test_phase0a_security.py
tests/test_phase0b_config.py
tests/test_phase1b_breaker_scorer.py
tests/test_phase3a_honesty.py
tests/test_refusion.py
tests/test_semantics.py
tests/test_stage1_privacy.py
tests/test_stage1_windows.py
tests/test_stage2b_alignment.py
tests/test_stage3_providers.py
tests/test_stage4a_pipeline.py
tests/test_stage4c_events.py
tests/test_stage4d_profiles.py
tests/test_stage7_server.py
tests/test_stage9_exports.py
tests/test_truth_corpus_followup_r3.py
tests/test_truth_corpus_followup_r8.py
tests/test_truth_review.py
```

```text
$ uv run pytest -q -m "not live" --durations=5 <files above>
........................................................................ [ 13%]
........................................................................ [ 27%]
........................................................................ [ 41%]
........................................................................ [ 55%]
........................................................................ [ 68%]
........................................................................ [ 82%]
........................................................................ [ 96%]
..................                                                       [100%]
============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector-cost-preview\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
============================= slowest 5 durations =============================
17.92s call     tests/test_refusion.py::test_spawned_process_refusion_crash_and_alias_lock_harness
15.42s call     tests/test_refusion.py::test_owner_visible_upkeep_skips
13.23s call     tests/test_refusion.py::test_refresh_is_the_explicit_action_that_analyses_again
11.55s call     tests/test_truth_corpus_followup_r8.py::test_audio_out_is_revalidated_immediately_before_publication
10.43s call     tests/test_refusion.py::test_analyse_re_fuses_a_fusion_2_result_without_a_single_provider_request
522 passed, 1 warning in 519.69s (0:08:39)
```

### Shard 3: 27 files

```text
tests/idea_web/test_backup.py
tests/idea_web/test_followup_round2.py
tests/idea_web/test_followup_round7.py
tests/idea_web/test_legacy_contract.py
tests/idea_web/test_refusion_money.py
tests/test_acrcloud_clip.py
tests/test_contracts.py
tests/test_engine_corroboration.py
tests/test_hint_corroboration.py
tests/test_paid_clip.py
tests/test_phase0a_status.py
tests/test_phase1a_bundles.py
tests/test_phase1b_fusion.py
tests/test_playlists.py
tests/test_scan.py
tests/test_service_api.py
tests/test_stage1_process.py
tests/test_stage2a_controlled.py
tests/test_stage2b_corpus.py
tests/test_stage3_shortlist.py
tests/test_stage4a_relations_fusion.py
tests/test_stage4c_generations.py
tests/test_stage5_calibration.py
tests/test_stage8_candidates.py
tests/test_truth_corpus_followup.py
tests/test_truth_corpus_followup_r4.py
tests/test_truth_corpus_followup_r9.py
```

```text
$ uv run pytest -q -m "not live" --durations=5 <files above>
........................................................................ [ 12%]
........................................................................ [ 25%]
........................................................................ [ 38%]
........................................................................ [ 51%]
........................................................................ [ 63%]
........................................................................ [ 76%]
........................................................................ [ 89%]
............................................................             [100%]
============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector-cost-preview\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
============================= slowest 5 durations =============================
34.80s call     tests/idea_web/test_followup_round2.py::test_a_paid_job_survives_a_worker_kill_through_the_real_supervisor
33.24s setup    tests/test_stage2a_controlled.py::test_default_render_covers_all_cases_and_valid_truth
10.98s call     tests/test_stage2a_controlled.py::test_failed_render_keeps_previously_published_corpus
8.17s call     tests/test_stage4c_generations.py::test_failure_injection_never_double_submits_across_generations[transport_error_before_acknowledgement]
6.68s call     tests/test_service_api.py::test_resume_from_primary_issues_zero_audd_requests
564 passed, 1 warning in 394.34s (0:06:34)
```

### Shard 4: 27 files

```text
tests/idea_web/test_coalescing.py
tests/idea_web/test_followup_round4.py
tests/idea_web/test_followup_round8.py
tests/idea_web/test_local_queue.py
tests/idea_web/test_refusion_upkeep.py
tests/test_audd_clip.py
tests/test_cost_preview.py
tests/test_fixture_audit.py
tests/test_identity_fuzzy.py
tests/test_phase0a_crash_cache.py
tests/test_phase0b_attempts.py
tests/test_phase1a_cached_open.py
tests/test_phase1b_targeting.py
tests/test_progress_wallclock.py
tests/test_scan_targeting.py
tests/test_stage10_webapp.py
tests/test_stage1_shazam.py
tests/test_stage2a_scorer.py
tests/test_stage2b_fuser.py
tests/test_stage4a_connectors.py
tests/test_stage4b_transforms_schedule.py
tests/test_stage4c_rescans.py
tests/test_stage6_enrich.py
tests/test_stage8_panako.py
tests/test_truth_corpus_followup_r10.py
tests/test_truth_corpus_followup_r6.py
tests/test_truth_corpus_followup_sol.py
```

```text
$ uv run pytest -q -m "not live" --durations=5 <files above>
........................................................................ [ 16%]
........................................................................ [ 33%]
........................................................................ [ 49%]
........................................................................ [ 66%]
........................................................................ [ 83%]
................ss.............................................s........ [ 99%]
.                                                                        [100%]
============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector-cost-preview\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
============================= slowest 5 durations =============================
21.34s call     tests/test_truth_corpus_followup_r6.py::test_controlled_render_serialises_against_a_concurrent_freeze
14.82s call     tests/idea_web/test_refusion_upkeep.py::test_the_server_answers_healthz_long_before_a_slow_upkeep_pass_is_done
13.57s call     tests/test_truth_corpus_followup_sol.py::test_generic_scorer_certifies_independent_truth_but_never_exposed_truth
11.81s call     tests/test_phase1b_targeting.py::test_reserve_confirms_a_new_identity_with_two_clips_30_s_apart
10.51s call     tests/test_truth_corpus_followup_r6.py::test_controlled_render_refuses_a_frozen_target
430 passed, 3 skipped, 1 warning in 269.80s (0:04:29)
```

The three skipped cases were re-invoked only to record their explicit reasons:

```text
sss                                                                      [100%]
============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector-cost-preview\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ===========================
SKIPPED [1] tests\test_stage8_panako.py:366: pinned Panako jar not present; run `id-detector panako-setup`
SKIPPED [1] tests\test_stage8_panako.py:416: pinned Panako jar not present
SKIPPED [1] tests\test_truth_corpus_followup_sol.py:479: resolving an unresolved disagreement is the forward transition
3 skipped, 1 warning in 11.27s
```

```text
$ uv run ruff check .
All checks passed!
```

```text
$ uv run ruff format --check .
423 files already formatted
```

```text
$ uv run python scripts/audit_fixtures.py
audited 545 files
fixture audit passed
```

```text
$ uv run python scripts/check_page_js.py
page JavaScript check passed: 53 inline scripts across 22 page renders, plus the static app.js
```

<!-- git-outputs:start -->

```text
$ git diff --stat
 docs/reviews/build-local-cost-preview.md | 855 +++++++++++++++++++++++++++++++
 scripts/compare_deep.py                  | 336 ++++++++++++
 scripts/measure_refusion.py              | 171 +++++++
 src/id_detector/cli.py                   |  73 +++
 src/id_detector/cost.py                  | 133 +++++
 src/id_detector/pipeline.py              |  12 +
 src/id_detector/service.py               |   3 +
 tests/test_cost_preview.py               | 349 +++++++++++++
 8 files changed, 1932 insertions(+)
```

```text
$ git status --short
 A docs/reviews/build-local-cost-preview.md
 A scripts/compare_deep.py
 A scripts/measure_refusion.py
 M src/id_detector/cli.py
 A src/id_detector/cost.py
 M src/id_detector/pipeline.py
 M src/id_detector/service.py
 A tests/test_cost_preview.py
```

```text
$ git status --short -- data work
```

<!-- git-outputs:end -->

Timestamp evidence compares full file inventory, byte length and UTC last-write
ticks for the original checkout's `work/` and `data/corpus/`, plus this detached
worktree's `data/corpus/`. This checks ignored files independently of Git.

```text
Before      : 39046
After       : 39046
Differences : 0
```

New files have intent-to-add entries solely so `git diff` includes them for review;
no content was committed and no branch was created.

## Orchestrator follow-ups, applied after the review (Opus)

The sol-xhigh read-only review returned `VERDICT: OK_TO_COMMIT` with no required fix at any
severity, and three P2 follow-ups. All three were closed on the reviewed tree before merging,
because two of them concern what the owner is told a scan will cost:

1. **A part-cent price is never shown lower than it is.** The two price lines formatted microdollars
   through a float (`estimate_e6 / 1000000:.2f`), which can understate an exact half-cent total such
   as $1.355 by half a cent. They now round cents up through the repo's own `money.ceil_e2`, the
   same helper the reservation line already used. A preview may overstate a half-cent; it must never
   understate what the owner would pay. Covered by
   `test_a_part_cent_price_is_never_shown_lower_than_it_is`, which fails against the float
   formatting (proved: `1 failed in 1.60s`) and passes with the fix.
2. **An explicit budget stop in the comparison runner.** The sum of per-mix durable reservations is
   already bounded by the hard budget, so recorded spend cannot exceed it; the runner now also
   checks recorded spend against the budget after each mix and stops before starting another if
   that invariant is ever broken. It is a backstop, not a replacement for the reservation bound.
3. **One U+FFFD replacement character** in this report, from a mangled quotation mark in a captured
   refusal message, replaced with a hyphen.

`tests/test_cost_preview.py` passes at 15 tests after all three, and `ruff check` and
`ruff format --check` are clean on the three changed files. The orchestrator ran the full suite and
both PowerShell gates on main after merging.
