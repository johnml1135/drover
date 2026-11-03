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
