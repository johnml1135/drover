# Slice 3b: `process`, remaining callers

`SLICE_BASE`: 6a95dba. Commits: Task 17's, then Tasks 18 to 20.

## Task 17: other drover raw sites

Section 5.1's "Other drover" sites now run through `process`: task_context:131, wizard_detect:17, resources:145, :244, decide:176, stats:268, decisions:22 and pane_files:61. Each stale catch tuple went with its site.

- **R1.** The rows for `task_context`, `wizard_detect`, `decide`, `stats` and `decisions` are gone: none of them imports `subprocess` now. `resources` and `pane_files` keep theirs until Task 18, for their injected `run=subprocess.run` defaults (`read_wsl_ram_gb`, `write_private`); both rows now give that reason. `decisions._git` keeps its R3 row and its shape (a string, or `DecisionError`) until Task 26. `pane_files.repo_trusted`'s R3 row is unchanged.
- **Decoding** (Task 14's call list):
  - resources:145 (`nvidia-smi`) decodes `Result.out` by `placement_exec.locale_text`, as `text=True` without an encoding did.
  - resources:244, decide:176, stats:268 and decisions:22 decode UTF-8 with `replace` (`Result.stdout`/`.stderr`).
  - task_context:131 and pane_files:61 keep strict UTF-8. They decode `Result.out` themselves, so undecodable output still fails: task_context with a `UnicodeDecodeError`, and in `repo_trusted` it means untrusted.
  - wizard_detect keeps bytes, read by `placement.decode_output` as before.
- **`locale_text` stays in `placement_exec`** (carry from Task 15). Section 5.1 says locale sites "read `Result.out` themselves" and fixes `process`'s interface, so `process` gains no decoder. `resources` imports `locale_text` from `placement_exec`, where placement_host, placement_process and placement_worker already get it.
- **Outcomes kept, site by site:**
  - `task_context.repository_rules`: a non-zero exit raises `OSError` with stderr, as before. A run that did not end now raises `OSError(detail)`, where `TimeoutExpired` escaped before. Its one caller, dispatcher_finish:296, catches neither, and the docstring says it fails closed on IO errors.
  - `wizard_detect._run` returns the `Result` when the run ended and `None` otherwise, as it returned `None` for `OSError` and `SubprocessError`. Its callers read `.code` and `.out` where they read `.returncode` and `.stdout` (bytes either way).
  - `resources.read_vram`: a run that did not end, or could not start, moves on to the next executable, as before. An ended run is parsed whatever its exit status, as before.
  - `resources.read_wsl`: a read that did not end is `None`. `WslOffline`, raised at build under the compaction hold, is still caught as an `OSError`. The Windows and Linux branches now share one `try`, which takes `resources` from 492 to 488 lines.
  - decide:176 and stats:268 now share `decide._wsl_gateway(route=None)`: the gateway from an ended `ip route`, or `None`. `base_url`'s `route=` test seam is kept, but no longer runs inside a catch.
  - `decisions._git`: a run that did not end raises `DecisionError('git: <detail>')`, where it gave `git: <exception text>` before. A non-zero exit gives stderr, then stdout, then 'git failed', as before.
  - `pane_files._wsl_read`: a run that is not `ok` raises `OSError(detail or exit status)`, where `check=True` raised `CalledProcessError`. `repo_trusted`'s catch tuple is now `(OSError, ValueError)`.
- **Gains, by design.** All eight commands now get DEVNULL stdin unless they send a payload (task_context sends its scopes). decide:176 and stats:268 also get the console flag, which they lacked before; that is empty on POSIX, where they run.
- **Tests:**
  - New and restated fakes at `process`, all `FakeCommands.patch(process)` with `process_options`. A real `subprocess.run` is refused in each of them.
  - `test_resources.Commands`: `test_resources_145_keeps_locale_decoding` (Task 14's list) and `test_resources_244_reads_wsl_memory_through_process`.
  - `test_decisions.Git`; `test_pane_launch_files.Trusted.test_the_wsl_reads_run_through_process_and_any_failure_is_untrusted`; `test_decide_devices.test_the_wsl_gateway_is_read_through_process`.
  - Restated: test_stats' `HubClient` (three tests, which patched the global `subprocess.run`), test_wizard_detection's two tool-probe tests (which patched `wizard_detect._run`), and test_worker_continuation's `test_repository_rules_read_on_owning_placement` (which patched `task_context.subprocess`).
  - No module left `tests.IMPURE`. test_decide_devices is pure on Windows, but on WSL `TestEnsureCPU.test_cli_reports_create_failure` still spawns.
- **Trace.** `git diff --exit-code 6a95dba -- tests/disk_decision_trace.json` prints nothing.

## Task 18: test-only run parameters

The injected `run=`/`runner=` parameters are gone from compaction, compaction_vhd, herdr_client, launch, pane_files, resources, usage, wsl_cache_reclaim, seat_paths and disk_schedule. Every one of those spawns now runs through `process.run(process.Command(...))`, and tests fake them at `process.subprocess`. `disk_schedule_steps` quotes with `process.list2cmdline`. `disk_schedule.install` had no timeout before; it now takes `LONG = 600.0`.

- **R1.** Eleven rows are gone: compaction, compaction_vhd, disk_schedule, disk_schedule_steps, herdr_client, launch, pane_files, resources, seat_paths, usage and wsl_cache_reclaim. None of them imports `subprocess` now.
- **Decoding:**
  - Strict UTF-8 is kept where the site named an encoding: usage, seat_paths and the `pane_files` WSL write. Each decode stays inside the handling it had before (usage's failed-reading catch, seat_paths' `validate` catch, which is now `(OSError, ValueError)`).
  - Sites that decoded by locale keep `placement_exec.locale_text`: compaction_vhd stderr and disk_schedule stderr. They now use `replace`, following Task 14's table.
  - herdr_client decodes UTF-8 with `replace` and turns CRLF into LF, as text mode did.
  - launch reads `Result.stdout`.
- **Outcomes, site by site:**
  - compaction `_wsl`: a run that did not end raises `OSError(detail)`, where `TimeoutExpired` or `FileNotFoundError` was raised before. `holder_dead` and `_attempt` catch `OSError`, so they behave as before.
  - compaction_vhd `_pwsh` gives `label: detail` for a run that did not end. Otherwise it gives the last 300 characters of stderr.
  - disk_schedule.install: `unavailable` raises `OSError(detail)`. A timeout or a non-zero exit raises `RuntimeError('schedule installation failed: ...')`.
  - herdr_client: a timeout while starting is `start_unconfirmed`, and otherwise `down (timed out)`. `unavailable` is `down (... did not start: <strerror>)`.
  - Missing executable (fix round 1):
    - `process.run` catches `FileNotFoundError` before any other `OSError`, and gives it the fixed detail `<argv0>: No such file or directory` whatever the OS language. `process.MISSING` holds that phrase.
    - Other `OSError`s keep their strerror.
    - launch and seat_paths share one check, `seat_paths.missing_executable`. It also matches 'cannot find', for the runner's own spawn-failed reason.
    - A German strerror still pauses dispatch and marks the backend unavailable (test_dispatcher_launch).
  - launch detach: a missing executable is still `LaunchError(missing=True)`. Any other run that did not end is `runner start failed: <detail>`. `_wsl_runners` raises `ScanError` for a run that did not end, and `json.loads` stays inside its `ValueError` catch.
  - pane_files.write_private: a run that is not `ok`, or an empty WSL path, raises `OSError`.
  - resources.read_wsl_ram_gb parses only an ended run. `WslOffline` and other `OSError` are still `None`.
  - usage: `_ended` raises `OSError` for a run that did not end. The probe's catch no longer names `SubprocessError`.
  - wsl_cache_reclaim: `_ended` raises `OSError`, which `reclaim` catches as before.
  - seat_paths: `unavailable` that `missing_executable` matches raises `FileNotFoundError`, so dispatch still pauses for a missing WSL. Anything else that did not end raises `OSError`.
- **Tests:**
  - New: `test_compaction.Offline.test_compaction_shutdown_literal_runs_in_the_offline_phase` and `test_disk_schedule.test_schedule_install_times_out_at_600_seconds_and_reports_failed`.
  - New guards for runs that did not end, in test_resources, test_usage and test_first_dispatch_homes.
  - Each restated test is a `FakeCommands.patch(process)` test that refuses real spawns, in herdr_client, usage, wsl_cache_reclaim, launch_runner_handle, pane_launch_files, resources and first_dispatch_homes.
  - Everything that passed `run=` now uses `tests.fakeci.answer_spawns`/`answering`, which adapts an old-style fake to `process.subprocess`. `FakeHerdr.serve(case)` does the same for the herdr fake.
  - The ceilings are unchanged, and `tests.IMPURE` is unchanged.
  - Three tests pinned the old short detail ('No such file', or the fake's own message), and now expect the fixed phrase: test_decisions, test_worker_continuation and test_guards' missing-git case. test_seat_paths_contract's EXPECTED gains launch's use of `missing_executable`.
- **Carry.** If the hub's job refuses breakaway, a detach timeout now ends a runner that used to survive (review Minor 3). This is already in the ledger's carries for the final review.
- **Trace.** `git diff --exit-code 6a95dba -- tests/disk_decision_trace.json` prints nothing.

## Task 19: hub callers

Section 5.1's Hub sites now run through `process`: hub/__main__:26, hub/auth:28, :85, hub/task_views:113 and hub/watchdog:17. hub/runtime:90 stays outside (ruling 15) and gains `timeout=600.0` on its existing line, so the file stays at 612 lines. hub/watchdog's keep-alive `Popen` stays outside by rule.

- **R1.** The rows for `hub/__main__`, `hub/auth` (both) and `hub/task_views` are gone. `hub/watchdog` keeps its row for the keep-alive. `hub/runtime`'s R1 and R3 reasons now state the frozen bootstrap and its own timeout. `hub/task_views`' R3 row (`probe.git`) stays until Task 27.
- **Decoding** (Task 14's call list):
  - hub/auth:28 (`icacls`) decodes `Result.out` by `placement_exec.locale_text`, strict, as `text=True` did.
  - hub/__main__:26, hub/auth:85 and hub/task_views:113 decode UTF-8 with `replace` (`Result.stdout`/`.stderr`). task_views still turns CRLF into LF, as text mode did.
  - hub/watchdog:17 keeps bytes, read by `decode_output` as before.
- **Outcomes kept, site by site:**
  - `discover_wsl_ip`: a run that did not end is `None`, as `OSError` and `TimeoutExpired` were. An ended run is read whatever its exit status, as before.
  - `auth._icacls`: a run that is not `ok` raises `OSError`, with the detail or the exit status and stderr. Before, `check=True` raised `CalledProcessError`, and a timeout raised `TimeoutExpired`. `acl_warnings` catches `Exception`. `restrict_acl` lets the error through to `load_token`, as before.
  - `auth.real_tailscale`: anything but an `ok` run tries the next executable, then gives `''`, as before.
  - `task_views.probe.git`: a run that did not end raises `OSError(detail)`, where `TimeoutExpired` or `FileNotFoundError` was raised before. A non-zero exit raises `OSError(stderr[-200:])`, as before. `RepoStates.get` catches `Exception`, so either one reads as an error snapshot.
  - `watchdog.real_wsl`:
    - `timed_out` gives `(-1, 'timeout')`.
    - `unavailable` gives `(-2, detail)`; a missing wsl.exe has the fixed `process.MISSING` detail.
    - `WslOffline`, raised at build, is still `(-2, str(e))`.
  - `hub/runtime._git`: a timeout raises `TimeoutExpired`, which is a `SubprocessError`. The handlers at runtime.py:360, :516 and :535 hold it.
- **Gains, by design.** All four migrated sites now get DEVNULL stdin and the elapsed check.
- **Tests:**
  - New: `test_hub_runtime.GitTimeout.test_hub_git_read_takes_600_seconds`, faked at `hub.runtime`'s own `subprocess`.
  - New: `test_hub_watchdog.RealWsl.test_watchdog_17_runs_through_process`.
  - New: `test_hub_api_listener.ShortCommandTests`, three tests for :28, :85 and `__main__`:26. The :28 test pins locale decoding of a non-ASCII path.
  - Restated: `test_web_perf.test_repo_state_probe_records_ahead_in_background_snapshot`. It patched `hub.task_views.subprocess.run`; it now uses `FakeCommands.patch(process)`.
  - The ceilings and `tests.IMPURE` are unchanged.
- **Trace.** `git diff --exit-code 6a95dba -- tests/disk_decision_trace.json` prints nothing.

## Task 20: `wsl_control` moves; `NO_WINDOW` has one home

`placement_exec.wsl_control` is now `process.wsl_argv(args) -> list[str]`, with the same checks at build and only there: `WslOffline` in the compaction hold's offline phase, and `CommandTooLong` (a `ValueError`, as before) past 8 KB. `process` imports `compaction_hold`, which imports only `durable`, so no cycle. R5's direction row for `process` already allowed it. `placement_exec`'s `compaction_hold` import is gone.

- **Callers.** Every caller calls `process.wsl_argv` by attribute, so there is one binding to fake: `WSL.command` (placement_exec), compaction `_wsl` (2), disk_runtime (2), pane_files (2, and its `from .placement_exec import wsl_control` is gone), placement_host (3), wsl_cache_reclaim and hub/watchdog `real_wsl`. `placement` no longer re-exports `wsl_control`, `NO_WINDOW` or `CREATE_NO_WINDOW`.
- **The console flag.** `placement_exec`'s `CREATE_NO_WINDOW` and `NO_WINDOW` are gone. `Windows.spawn` (under `spawn_worker`) and `detach` read `process.CREATE_NO_WINDOW`. hub/watchdog's keep-alive `Popen` reads `process.NO_WINDOW`. The flag is now defined only in `process` and in the frozen `hub/runtime` (ruling 15). `pane_status_hook` keeps its literal, because it is standalone. `placement_exec` keeps `CREATE_SUSPENDED`, `CREATE_NEW_PROCESS_GROUP` and `CREATE_BREAKAWAY_FROM_JOB` for its ADR-0002 `Popen`s.
- **Removed symbols.** `qualified_names` gains `drover.placement_exec.wsl_control`, as the brief asks, and also `drover.placement.wsl_control` and the `NO_WINDOW` and `CREATE_NO_WINDOW` names on `placement_exec` and `placement`. The extra rows guard the re-exports and the flag copies that went in the same commit.
- **Tests.**
  - New: `test_process.WslArgv`, four tests. `test_wsl_argv_refuses_over_8_kb` and `test_wsl_argv_raises_wsl_offline_in_the_offline_phase` pin the build checks. `test_run_makes_no_hold_check` pins two things in the offline phase: the literal `['wsl.exe', '--shutdown']` runs through `process.run`, and the same argv is refused when it is built. `test_watchdog_keep_alive_is_refused_offline_at_build` covers the watchdog's keep-alive: it is refused through `WSL.command` and `process.wsl_argv`, and its `Popen` is never reached.
  - Restated:
    - test_compaction and test_compaction_hold call `process.wsl_argv`.
    - test_compaction_hub's Polling spy patches `process.wsl_argv` once, where it patched four bindings.
    - test_pane_launch_files and test_wsl_cache_reclaim patch `process.wsl_argv`.
    - test_disk_fix6's builder census finds no `wsl.exe` builder in `placement_exec` and `wsl_argv` alone in `process`, and expects `CommandTooLong`.
    - test_runner_detach's two Windows detach tests take the flag from `process.CREATE_NO_WINDOW`. They no longer create `placement_exec.CREATE_NO_WINDOW` under `patch.multiple(create=True)`.
  - Deleted: `test_runner_detach.DetachTests.test_no_window_helper_is_empty_off_windows`. It pinned `placement.NO_WINDOW`, which is gone, and `test_process.Run.test_every_command_carries_no_shell_and_the_console_flag` already pins `process.NO_WINDOW`'s value on both platforms. That class is in the integration tier, so `INTEGRATION_CEILING` falls from 209 to 208. `IMPURE_CEILING` is unchanged.
  - test_herdr_client and test_disk_worker_status already read `process.NO_WINDOW`, so they needed no change.
- **R1.** No row changes. `placement_exec` (ADR-0002) and `hub/watchdog` (keep-alive) still spawn outside `process` by rule. `tests/seam_exceptions.json` is unchanged.

## Slice exit

- Every other short-lived command runs through `process`. The R1 rows that remain are the section 13 end rows: ci, disk_cleanup `locked_command`, pane_status_hook, placement_exec, placement_tree, runner, runner_supervise, hub/runtime and hub/watchdog. The rest are due in slice 4 (Tasks 24 to 28) or in Task 45. Each of them names `subprocess` types only. placement_git's row still says "Spawns directly", but since slice 3a it only builds `CompletedProcess` and `TimeoutExpired` around `process.run`. That stale reason is left for Task 28, which deletes the row.
- `grep -rn "0x08000000\|NO_WINDOW *=\|CREATE_NO_WINDOW *=" drover hub` finds three: process.py:29, hub/runtime.py:36 and the hook's literal at pane_status_hook.py:66.
- `placement_exec` and `hub/watchdog` read `process`'s flag.
- **Trace.** `git diff --exit-code 6a95dba -- tests/disk_decision_trace.json` prints nothing.
