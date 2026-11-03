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
