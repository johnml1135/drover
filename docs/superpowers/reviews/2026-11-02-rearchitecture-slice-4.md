# Slice 4: `git_client`

`SLICE_BASE`: 4d8039d. Commits: Task 22's, then Tasks 23 to 28.

Tasks 24 to 27 add their handler lists here, one line per handler: `file:line, what it catches, what now reaches it, change` (ruling 12, widened by 2M2).

## Task 22: `git_client`

`drover/git_client.py` is new, as section 5.2: `Answer`, `Why`, `GitReply`, `GitDone`, `GitFailed(OSError)`, `QUICK, NORMAL, FETCH, LONG`, `ask`, `out`, `mutate`, `argv`, and `request_git`. No caller moves in this task.

- **Ordering.** argv and env from `placement_git._git_command`, then `process.run`, then the count. The count is the one `placement_git.git` keeps: `git_client` increments `placement_git.git_spawns` under `placement_git.git_spawn_lock`, and `request_git` is `placement_git`'s object, so both routes count in one place until Task 28 moves the definitions. A command that never spawned is not counted.
- **Why.** Built from `process`'s kinds: `timed_out` is `timeout`, `unavailable` is `unavailable`, and an exit outside 0 (and outside `no`, for `ask`) is `exit`. `code` is `None` unless git exited, so no 124 or 127 reaches a caller.
- **At build.** An `OSError` or `ValueError` from building the argv (`WslOffline`, a bad `.git` pointer from `_git_location`, a bad distro name or unreadable config) is `unavailable`, with `err` reading `git unavailable: <text>`, as `placement_git.git`'s rc 127 did. `CommandTooLong`, a `ValueError`, still raises: it is a caller's bug. `git_client` may not import `compaction_hold` (R5), so it catches `WslOffline` as the `OSError` it is.
- **Output.** Decoded as `placement_git.git` decodes it today (`placement_git._text`: UTF-8 with `replace`, newlines made LF). An unavailable run's `err` carries `process`'s detail.
- **`GitFailed`.** `str()` is `git <subcommand> failed: exit <code>` or `... failed: <why>`: neither stderr nor the remaining arguments, which can hold a remote URL.
- **`sink`.** Not on `ask`: ruling N10 dropped `Command.sink` and `ask(sink=)`, as no production caller remains under ruling 15. The brief's interface line predates that ruling.
- **R5.** `test_seams` pins the live rule for `git_client` (it imports only `process`, `placement_git` and `placement_paths`) and that `placement_git` reaches `git_client` only by stated, function-level rows; there are none until Task 27.
- **Trace.** Untouched.

## Task 24: `pipeline`, `replan`, `landing`, `landing_batch`

`pipeline.git` and `landing.timed_git` are deleted (`GIT_TIMED_OUT` too), and both R1 rows go. `pipeline.Repository` keeps its methods over `git_client`: `out` with `error=pipeline.refused` (WorktreeRefused, the first three arguments and the last 300 characters of stderr, or stdout) at every `check=True` site, `ask` elsewhere, `LONG` where no timeout was named, 10 s for `status`. `replan` inspects through `out(..., timeout_s=60, error=refused)`. `landing_batch`'s own reads and the replay use `out(error=ci_error)` (CIError, as `landing_batch.git` words it) and `ask`; `landing_batch.git` stays for `landing_control` and for `add_worktree`'s `run=` until Task 25. `contained` returns `Answer`, at `LONG`, and its caller raises unless it is `YES`. `outside_land._out` now asks `git_client` and still raises `TimeoutExpired` or `GitUnknown`, so its handlers are unchanged until Task 27.

Handlers, `file:line, what it catches, what now reaches it, change`:
- pipeline.py:213 `commit_ready`, `ci.CIError`: WorktreeRefused from `error=` as before; no GitFailed (every `out` has `error=`). None.
- pipeline.py:346 and :457, `(ValueError, OSError)` around `Repository.tag`: `SubprocessError` is dropped with `import subprocess` (R1). `ownership.tag` reaches `placement_git.worktree_metadata`, which still raises `TimeoutExpired` on a timeout until Task 27, so a metadata timeout now propagates instead of becoming WorktreeRefused. Carried to Task 27.
- pipeline.py:411 and :419, `origin.FetchFailed` and `origin.SyncError` around `Repository.sync`: sync now runs on `origin.git` (was `pipeline.git`). A failed fetch, a timeout included, is still FetchFailed. A failed `check=True` read inside sync is now SyncError with no stderr, turned into WorktreeRefused at :419; before it was WorktreeRefused with stderr. Same type for the caller.
- pipeline.py:460 `_create`, `Exception`: `Repository.remove` raises WorktreeRefused from `error=` (timeout and unavailable too). None.
- replan.py:106 and :155, `(OSError, ValueError, WorktreeRefused)`: WorktreeRefused for an exit, a timeout or no git, as rc 124 and 127 were. None.
- landing_batch.py:192 (new) cherry-pick: a timeout or no git raises CIError before any `--abort` or `reset --hard`; any exit is a conflict, as before.
- landing.py:85 `run_batch`, `BaseException`: now also gets that CIError; outcome `error`, batch recorded, re-raised to hub/dispatch.py:156, as for any CIError from `build_tip` today. The `finally` still force-removes the batch worktree.
- landing.py:253 (was :256) `verify_manual_land`: an UNKNOWN `contained` raises ValueError, as NO does; a timeout raised `TimeoutExpired` before. No production caller.
- outside_land.py:147, :405, :462, :467: unchanged; `_out` gives `TimeoutExpired` for a timeout and `GitUnknown` for any other unknown, as `timed_git` did (`exited 127` now reads `unavailable`).

Line counts: pipeline 490 to 489, landing 275 to 272, landing_batch 307 to 326. Trace files unchanged.
