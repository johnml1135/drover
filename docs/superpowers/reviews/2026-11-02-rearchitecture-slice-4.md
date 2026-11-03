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

## Task 25: `origin` and `ControlledGit`

`origin.git`, `landing_batch.git` and `landing_control._controlled_git` are deleted, and the R1 rows for `origin` and `landing_batch` go. Every `origin` function takes `git=`, an object with `ask`, `out` and `mutate`; the `git_client` module is the default. `landing_control.ControlledGit(conn, epoch)` has those three and checks the control epoch before any `merge`, `update-ref` or `push`, by `args[0]`, whichever verb carries it. The fetch, the compare-and-swap `update-ref`, the ff-only `merge` and the push are `mutate`s; every `check=True` read is `out(error=origin.sync_error)`, a SyncError naming only the command (`git rev-parse failed`), as `origin.git` did. Reads with no timeout take `LONG`; fetch, push and `ls-remote` take `FETCH` (120 s, as `FETCH_TIMEOUT_S`). `landing_control`'s own reads use `landing_batch._out` (CIError by `error=`) and `ask`. `placement.add_worktree` runs `git_client.mutate`, its `run=` gone, and takes `error=` and `timeout_s=LONG` (see the fix-round ruling below). `pane_close`'s `run` is shaped like `ask`, `git_client.ask` by default, and `pane_close._git` is deleted. `tests.test_origin_sync` runs on a FakeGit, so it leaves `tests.IMPURE` (ceiling 63 to 62).

The shipping defect: a merge that timed out (rc 124) raised MainMoved, so landing read it as `stale-base` and regated against a checkout that might hold `.git/index.lock`. Now only a merge that exited raises MainMoved. A timed-out or unavailable merge raises SyncError, `main fast-forward did not finish (git merge: timeout); owner intervention required`.

Handlers, `file:line, what it catches, what now reaches it, change`:
- landing_control.py:66 `local_fast_forward`, `origin.MainMoved` → CIError `rebase and regate`: a non-zero `update-ref` (a timeout included: it is atomic), a merge that exited, or a moved HEAD or branch, as before. A merge timeout no longer reaches it. Fixed.
- landing_control.py:68, `origin.DirtyCheckout`: re-raised to landing.py:206, as before. None.
- landing_control.py:70, `origin.SyncError` → `CIError(str(e))`: now also gets the merge-timeout SyncError, so landing.py:209 records `refused` with that reason, not `stale-base`. A failed read is still `git <cmd> failed`. Fixed.
- landing_control.py:62, `merge-base --is-ancestor` before the fast-forward: anything but YES, unknown included, raises `local main moved; rebase and regate`, as rc 124 did. Nothing lands. None (see report: kept, not split).
- landing_control.py:60, :192 and :222 `_out(rev-parse)`: CIError with git's stderr, or `timeout`, as `landing_batch.git(check=True)` gave. Not caught in `sync_main`; it reaches landing.py:40/:143/:196's callers as before. None.
- landing_control.py:194 `sync_main`, `AdmissionHeld`: ControlledGit's guard raises it from inside `origin`, as `_controlled_git` did. None.
- landing_control.py:196 `DirtyCheckout`, :200 `FetchFailed`: a fetch that exited, timed out or could not run is FetchFailed, as before. None.
- landing_control.py:210 `sync_main`, `origin.SyncError` → needs-owner: also gets the merge-timeout SyncError (in `sync` it was already SyncError, `moved during origin sync; retry`, so only the reason changes), and `git merge-base failed` when the second ancestry check is unknown (it read as `main diverged from origin` before). Message only.
- landing.py:225 `run_batch` push, `origin.SyncError`: `push_main`'s read and push failures, no stderr, as before. None.
- hub/store_ui.py:303 owner push, `origin.SyncError`: the same. None.
- pipeline.py:410 `_create`, `origin.FetchFailed`: as before. None.
- pipeline.py:418 `_create`, `origin.SyncError` → WorktreeRefused: when the SyncError's cause is GitFailed (a `check=True` read), the reason is `pipeline.refused(cause)`: the command and git's stderr tail, as `pipeline.git` gave it before Task 24. SyncError's own `str()` still holds no stderr. Other SyncErrors keep `str(e)`. Restored.
- landing_batch.py:155 `renew_owner`, `(OSError, ValueError)` → CIError: `subprocess.SubprocessError` goes with the R1 row. `ownership.tag` reaches `placement_git.worktree_metadata`, which raises `TimeoutExpired` on a timeout until Task 27, so that timeout now reaches landing.py:85 (`BaseException`: outcome `error`, batch recorded, re-raised) instead of a CIError. Same window as pipeline.py:346/:457; Task 27's carry covers it.
- landing_batch.py:162, pipeline.py:135 `add_worktree(error=)`: at `LONG` as before (each passed an untimed `run=`); a timeout raises `CIError` / `WorktreeRefused` `git worktree add failed: timed out after 600s` (was `... git worktree timed out after 600s`). Wording.
- worktree.py:18 `add_worktree` (default `run`): the limit goes from 60 s to `LONG`; disk_registrations.py:265 passes `timeout_s=git_client.NORMAL` and keeps 60 s. A timeout now raises the given error (RuntimeError, ValueError) instead of `TimeoutExpired`; worktree.py:41 and disk_registrations.py:276 hold both.
- pane_close.py:44 `worktree_facts`, `(_Unknown, *EXPECTED, ValueError)`: any UNKNOWN reply stops reading the checkout and keeps the pane, as a raised `TimeoutExpired` did. An exit outside each read's `no` is now unknown and keeps the pane. A repo with neither `origin/main` nor `main` (each `rev-parse --verify -q` exits 1, a no) is "not merged" without asking `merge-base`, as rc 128 read at db08f36, so its panes still close (fix round 1).
- origin.ahead: YES gives the count, a timeout `None`, anything else 0, as rc 124 and other codes did. A bad `.git` pointer (`ValueError`, which `origin.git` turned into 124, `None`) is now `unavailable`, 0. Display only.

Line counts: origin 100 to 105, landing_batch 326 to 315, landing_control 324 to 342, pipeline 489 to 488, placement 177 to 179, pane_close 285 to 291. Trace files unchanged.

Fix round 1 (lead ruling): `placement.add_worktree` takes `timeout_s=LONG`, a departure from the brief's "`error=` only". disk_registrations.py:265 passes `git_client.NORMAL`, because the registration repair runs inside a disk stage and section 6.1 holds a stage's changes to 60 s; worktree.py:18, pipeline.py:135 and landing_batch.py:162 keep `LONG`. `drover.pane_close._git` joins `qualified_names`. Trace unchanged.
