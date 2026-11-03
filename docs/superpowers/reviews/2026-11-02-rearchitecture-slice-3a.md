# Slice 3a: `process`, placement and disk callers

`SLICE_BASE`: 9619826. Commits: Task 14's, then Tasks 15 and 16.

## Task 14: `process`

`drover/process.py` holds `Kind`, `Command`, `Result`, `CommandTooLong`, `run`, `NO_WINDOW` and `list2cmdline`, as section 5.1 with two departures:

- **No `Command.sink`.** Ruling N10 dropped it: no production caller under ruling 15. `tests/fakeci.process_options` has no `sink=` for the same reason, and the brief's `test_sink_takes_stdout` is not written.
- **`Command` checks itself at construction.** A non-positive, infinite, `None`, `bool` or non-numeric timeout, an empty argv or a bare string argv raises `ValueError` when the `Command` is built, so `run` never meets a bad command. A `str` payload goes to the child as UTF-8 bytes.

A timeout is now typed: `Result.kind == 'timed_out'`, and `process.run` raises nothing for a process that failed. That is what the Task 23 carry needs to narrow `git_worktrees._git`'s `except Exception`, once `git_client` sits on `process` (slice 4). Nothing here touches that handler.

**A timeout ends the whole tree** (lead ruling, fix round 1, overriding the brief's "calls `subprocess.run` once"). The module's `subprocess` reference is a small adapter: the real module, except that `run` spawns with `Popen`. On Windows the child starts suspended, joins a job object with kill-on-close, and is then resumed; a timeout terminates the job. On POSIX the child leads its own session, and a timeout kills the process group. After the kill the run drains its pipes for at most `DRAIN_S` (2 s) and returns `timed_out` with the real elapsed time. A tree that finishes in time has kill-on-close lifted before the job closes, so a descendant that outlives it survives, as before. If no job can be had (for example, when the process is already in a job that forbids breakaway), it logs one warning and a timeout ends only the direct child; the run is still bounded by the drain. `FakeCommands.patch(process)` replaces the whole reference, so fakes see the same options as before. The real-tree test is `tests.test_proc_scan.ProcessTree`, in the default tier, in an IMPURE module that already spawns; neither ceiling moves.

`process.NO_WINDOW` is a second definition of the flag beside `placement_exec.NO_WINDOW` until Task 20 moves the others onto it.

## The call lists (Step 1)

Read-only, by an AST walk of `drover/` and `hub/` at 9619826 (scratch script, not committed), then each site read by hand. Line numbers are at 9619826.

### Commands with no timeout today

Raw spawns:

| Site | Command | Today |
|---|---|---|
| disk_schedule.py:43 | `schtasks` or `crontab` install argv, via `runner or subprocess.run` | No timeout; section 5.1 gives 600 s. The spec's `disk_schedule:49` is this line. |
| hub/runtime.py:90 `_git` | git reads and the bounded archive | No timeout; `LONG`, a guess. Outside `process` by ruling 15. |
| ci.py:449 | gate step under the runner | Stays outside, by rule |
| disk_cleanup.py:400 `locked_command` | the owner's own command | Stays outside, by rule |
| placement_exec.py:83, :107, :133, :138, :140 | worker trees and detach, `Popen` | Stay outside (ADR-0002) |
| pane_status_hook.py:67 | standalone hook's detached child, `Popen` | Stays outside |
| hub/watchdog.py:27 | keep-alive `sleep`, `Popen` | Stays outside |

Git wrapper calls that reach `placement_git.git` with `timeout=None`:

| Wrapper | Calls with no `timeout=` |
|---|---|
| `cleanup.git` (never passes one), from cleanup.py:16 | cleanup.py:30 (`_git_ok`), :39, :43, :49 (`_git_ok`), :57, :67, :72, :116, :118, :132, :134, :141, :144 |
| `pipeline.git` | pipeline.py:71, :77, :89, :92, :95, :98, :106 (`inventory`, `worktree list`), :129, :142, :145, :146, :150, :155, :156, :157, :160, :164 |
| `landing_batch.git` | landing_batch.py:73, :90, :101, :161, :163, :164, :165, :168, :170, :171, :174 (`cherry-pick`), :175, :176, :180, :183 |
| `landing_batch.git` from landing | landing.py:45, :99, :111, :125, :146, :250, :254 |
| `landing.contained` → `timed_git(timeout=None)` | landing.py:256 |
| `landing_batch.git` from landing_control | landing_control.py:43, :44, :174, :204; `_controlled_git` (:18) passes its caller's options through |
| `origin.git`, `fast_forward(run=git)` | origin.py:27 (`worktree list`), :34 (`update-ref` CAS), :40, :42, :43, :46 (`merge --ff-only`) |
| `origin.git`, `sync(run=git)` | origin.py:56, :58, :59, :61 (the `fetch` at :53 has one) |
| `origin.git`, `push_main` | origin.py:91, :93, :100 (`update-ref` of tracking; the `push` at :97 has one) |
| `placement.add_worktree` given `run=pipeline.git` (pipeline.py:138) or `run=landing_batch.git` (landing_batch.py:157) | placement.py:134 (`worktree add`), :142 (the retry without `--relative-paths`), :146 (`config gc.worktreePruneExpire`); `LONG` per section 5.1's Adapter paragraph |
| `pipeline.git` through `origin.sync(run=git)` (pipeline.py:125) | the `origin.py` `sync` and `fast_forward` lines above run with `pipeline.git`, not `origin.git`, on this path |

Every other `placement.git` and `placement_git.git` call names a timeout. Wrappers that always set one: `ci.git` 60, `disk_worktrees.git` 60, `dispatch_hold._git` 60, `task_access._git` 60, `pane_close._git` 30, `replan`'s 60. `placement.add_worktree`'s default `run` sets 60, but both of its callers pass an untimed `run` (row above). `origin.ahead` defaults to `None`, and its one caller, hub/task_views.py:128, passes 30.

Section 5.1's table matches this list. It did not name `cleanup.git`'s own line (:16), `origin.push_main`'s reads, or `landing_control`'s pass-through; they fall in its `cleanup.git`, `origin.git` and `landing_control` rows. `add_worktree`'s path is in section 5.1's Adapter paragraph, not its table.

Scope: `drover/` and `hub/` only, as the spec's. `scripts/` is not listed (scripts/deploy_hub.py:36 runs git with no timeout).

### `text=True` with no `encoding`

These decode by the locale code page today (cp1252 here). Moved onto `process`, `Result.stdout` decodes UTF-8 with `replace`. **Locale** marks a site that should read `Result.out` and decode by locale itself, keeping today's reading of a Windows tool's non-ASCII text.

| Site | Command | Decoding after the move |
|---|---|---|
| placement_host.py:72 | `reg query` | **Locale** (section 5.1) |
| resources.py:145 | `nvidia-smi` | **Locale** (section 5.1) |
| hub/auth.py:28 | `icacls` | **Locale** (section 5.1) |
| placement_process.py:134 | PowerShell process scan | **Locale** (section 5.1) |
| placement_process.py:310 | PowerShell process scan, `errors='replace'` | **Locale** (section 5.1) |
| placement_worker.py:167 | PowerShell toast; stderr is read on failure | **Locale**, a Windows tool's message text |
| compaction_vhd.py:36 (`run`) | `pwsh` mount and compact scripts; stderr is the error text | **Locale**, a Windows tool's message text |
| disk_schedule.py:43 | `schtasks` / `crontab`; stderr is the error text | **Locale** on Windows, a Windows tool's message text |
| hub/__main__.py:26 | PowerShell, an IP address | UTF-8: ASCII output |
| hub/auth.py:85 | `tailscale status --json` | UTF-8: Go writes UTF-8, so this corrects today's reading |
| compaction.py:118 (`run_command`) | `wsl.exe` Linux commands (`ps`) | UTF-8: Linux output |
| placement_host.py:172 (`run`) | `wsl.exe df` | UTF-8 |
| resources.py:224 (`run`), :244 | `wsl.exe cat /proc/meminfo` | UTF-8 |
| wsl_cache_reclaim.py:65 (`run`) | `wsl.exe cat /proc/meminfo` | UTF-8 |
| decide.py:176, stats.py:268 | `ip route` | UTF-8 |
| decisions.py:23 | git, `errors='replace'` | UTF-8 |
| hub/task_views.py:113 | git | UTF-8 |
| disk_runtime.py:92 | the cleanup worker; Drover's own JSON | UTF-8 |
| launch.py:406, :480 (`self.run`) | Drover's own launcher and runner scan, JSON | UTF-8 |
| disk_side.py:46 | the trim argv; stderr kept in the report | UTF-8 |
| disk_paths.py:161 | a disposable path's configured clean argv; output kept in the report | UTF-8 |
| pane_status_hook.py:31 | `wslpath -u` | Stays outside, by rule |

Payloads change too: disk_runtime.py:92 and placement_worker.py:167 send `input=` text, encoded by locale today and as UTF-8 through `process`. Both send `json.dumps` output, which is ASCII, so the bytes are the same.

## Task 15: placement callers

Section 5.1's Placement sites now run through `process`: placement_exec:155, placement_git:221, placement:168, placement_host:72, :131, :143 (`wsl_running`), :172 (`_wsl_df`), placement_process:100, :134, :310 and placement_worker:167. `placement_git.git` (the old :119) does too, so every `placement.git` and `placement_git.git` call reaches the one fake at `process`. Its conventions stay until Task 28: it returns `CompletedProcess` with text decoded as before (UTF-8, `replace`, newlines made LF) and raises `subprocess.TimeoutExpired` with the output so far. Its `runner=` parameter and `reachable`'s are gone; no production caller passed one.

- **Untimed git.** `process` needs a timeout, so a `placement_git.git` call that names none now runs under `placement_git.LONG` = 600 s, section 5.1's `LONG`. Those are the calls in the "Commands with no timeout today" table above, including `add_worktree` under `pipeline.git` and `landing_batch.git`. A timeout raises `TimeoutExpired`, as a timed call does today; `pipeline.git` and `landing_batch.git` turn it into rc 124, and `cleanup.git` does not catch it (it reaches `run_garden`'s `except Exception` and reads as `error`). Two callers then misread rc 124 until slice 4:
  - landing_batch.py:174 (`cherry-pick`, `check=False`) reads it as a **rebase conflict**: `cherry-pick --abort`, `reset -q --hard`, and the task goes to NEEDS_OWNER with reason 'rebase conflict'. If an `index.lock` is left, the reset fails and raises `CIError` instead, so nothing is reset over the lock.
  - origin.py:46 (`merge --ff-only`) reads it as **`MainMoved`**, and landing_control.py:49 raises "local main moved; rebase and regate". With an `index.lock` left and a clean status, every later landing repeats this and never reaches the owner.

  Section 5.1 forbids both end states. The ledger holds both for Tasks 24 and 25 (rc 124 at :174 raises `CIError` before the abort and reset; rc 124 at origin.py:46 raises `SyncError`; `cleanup.git` catches the timeout), and rules that the branch deploys only whole, after Task 49, so no slice ships before those fixes. Task 16 makes the two wrappers' message name the limit the call ran under (`timed out after 600s`), where it printed "Nones".
- **Locale sites.** placement_host:72 (`reg`), placement_process:134 and :310 (PowerShell) and placement_worker:167 (the toast's stderr) decode `Result.out` or `.err` by `placement_exec.locale_text`, which reads bytes as `text=True` without an encoding did: the locale's encoding (UTF-8 in UTF-8 mode), newlines made LF, strict or `replace` as the site had it. placement_process:100 keeps UTF-8 with `replace` (its script sets UTF-8 output). placement_host:131, :143 keep `decode_output`; `_wsl_df` reads `.stdout` (digits).
- **Errors.** Where a `Result` replaced a catch tuple, the tuple went: the `wsl_control` sites keep `except OSError` for `WslOffline` only. `_cim_rows` raises the builtin `TimeoutError` on `timed_out` and `OSError` on `unavailable`; placement_process:415 and placement_proof:118 test for `TimeoutError`, so `process_query_timeout` stays. `recover_ci_children` and `stop_ci_children` raise `ScanError` for a run that did not exit, where a `TimeoutExpired` or `OSError` escaped before; every caller treats either as unconfirmed. `worktree_metadata` keeps raising `TimeoutExpired` and `OSError`. `git` returns rc 127 for any refused spawn, where only `FileNotFoundError` did before: a `PermissionError` now reads as a failed git, not an exception.
- **Gains, by design.** Every one of these commands now has the console flag, and DEVNULL stdin unless it sends a payload. Before, `ci_command`'s probe had neither, the toast had no console flag, and git, two of the three PowerShell scans, `reg` and the `wsl.exe` reads inherited stdin.
- **R1.** Rows for `placement`, `placement_host`, `placement_process`, `placement_worker` are gone; `placement_exec` (ADR-0002) and `placement_git` (until Task 28) keep theirs. `placement_proof`'s row stays for Task 45 and now names `SubprocessError` only. The `NO_WINDOW` imports at placement_git, placement_host and placement_process went with their last use; `placement.NO_WINDOW` stays as a re-export for disk_runtime.
- **Traces.** `tests/placement_trace.json` re-recorded from `placement_trace.collect()`: no change. Its recorded calls are disk_runtime's worker starts (Task 16), and no placement site in it reaches `process`, checked with `process`'s spawn refused. `tests/disk_trace.PATCH_TARGETS` is `(process, disk_paths)`; `TraceGit` answers bytes when no `text` is asked. `git diff --exit-code 9619826 -- tests/disk_decision_trace.json` prints nothing.

## Task 16: disk callers

disk_runtime:92 (the cleanup worker), :141 (its startup proof on WSL), disk_side:46 (trim) and disk_paths:161 (a declared cache command) run through `process`. `tests/disk_trace.PATCH_TARGETS` is `(process,)`.

- **Outcomes kept.**
  - Worker: `timed_out` takes the old `TimeoutExpired` branch (`worker_timeout`, partial output and stderr kept, `done` left `None`), and `unavailable` takes the old `OSError` branch (`worker_unavailable`).
  - Startup proof: a proof that does not exit raises `OSError(detail)` inside the existing handler, so it stays unconfirmed with the reason in the row.
  - Trim: not exited, or a non-zero exit, is `trim_unavailable` as before.
  - Cache: not exited is an error row, and the row stays a `candidate`.
  - Output decodes as UTF-8 with `replace`, as the call list above set for these three sites, so an undecodable worker stdout no longer re-raises a `UnicodeError`.
  - `cleanup_enqueue`'s tuple is `OSError` alone: `cleanup_identity` spawns nothing.
- **Gains, by design.** The cache command and trim gain the console flag and DEVNULL stdin, and the startup proof gains DEVNULL stdin.
- **Containment on POSIX** (lead ruling, its own commit). A `process` child leads its own session, so a cache command's leftover descendant fell outside the worker's process group. The worker's proof (`stop_ci_children`, which only killed and awaited that group) therefore let it outlive the worker after the lock was released. Real WSL showed it in four composed tests. The proof now also kills and awaits every live process of this user whose environment holds the exact `DROVER_RUN_ID=<run_id>` entry, skipping itself, its ancestors, zombies and anything it cannot read. It stays unconfirmed while any match survives. On Windows the `process` job nests inside the worker's job, so nothing changed there.
- **Decision trace** (lead ruling). One row changed: `cache_command_times_out.applied`'s error text went from `Command '['trace-cache']' timed out after 120 seconds` (the harness exception's own text) to `timed out after 120 s` (`Result.detail`). `action`, `path` and `reason_code` are unchanged, and `deleted` is `[]` before and after. `--compare` reads the lost text as `towards_deletion` and the new one as `report_added`, and the lead ratified it as text only. So `git diff 9619826 -- tests/disk_decision_trace.json` shows this one line, not nothing.
- **placement_trace.** Its `Recorder` patches `process.subprocess.run` and answers bytes. The re-record changes `kwargs` only: `text` went from the worker calls, `cwd` was added there, and `cwd`, `shell` and `stdin` were added on the WSL proof. Answers, commands, inputs and timeouts are unchanged.
- **R1.** The rows for `disk_runtime` and `disk_side` are gone. `disk_paths` keeps its row for Task 45: it now only names `SubprocessError` in its exception tuples.
- **Tests.**
  - `tests.test_disk_trim` left `tests.IMPURE`, so `IMPURE_CEILING` is 64. Its real worker-plus-cache test is restated as a fake at `process`, and the worker's argv is pinned in `test_disk_worker_status`.
  - The composed real-worker tests (test_disk_fix2, fix5, test_disk_fixes, test_disk_wsl_outputs) now patch `process.subprocess.run`.
  - fix5's setsid fixture raises `EINVAL`: the worker is already a session leader, so `EPERM` is the accepted case.
- **Untimed git message.** `pipeline.git` and `landing_batch.git` now say `timed out after {limit}s` with the limit the call ran under, 600 for an untimed call, where they printed "Nones".
