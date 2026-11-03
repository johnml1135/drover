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

## Task 26: `ci`, `cleanup`, disk git, small callers

`ci.git`, `cleanup.git`, `disk_worktrees.git`, `dispatch_hold._git`, `task_access._git` and `decisions._git` are deleted and named in `qualified_names`; R1's `dispatch_hold` row and R3's `decisions._git` row go. Every `placement.git` call in `ci`, `cleanup`, `disk_worktrees`, `disk_paths`, `disk_registrations`, `dispatch_hold`, `task_access`, `decisions`, `brief`, `context_files` and `repos` now goes through `git_client`; `ledger/ops_outside` is Task 27's. Each module keeps its domain type:
- `ci`: `_out` is `out(error=ci_error)`, CIError worded as `ci.git` worded it (`git <args> timed out after 60 seconds`, or `git <args>: <stderr>`), at `NORMAL` with `git_environment()`. `contains` asks with `no=(1,)`: any exit reads as before (a base that does not resolve is not found, an ancestry exit is not contained); a timeout raises CIError, as before, and so now does git that cannot start (it read as "base not found" or "not contained", rc 127). `ci.py:119` stays an R3 row.
- `cleanup`: `_ask` at `LONG`, `_changed` (`mutate`) at `LONG`, both with `GIT_OPTIONAL_LOCKS=0`. Any exit is read by its code as before; a read that times out or cannot start raises GitError, so `delete_branch` refuses with `git error, nothing deleted` instead of letting `TimeoutExpired` escape (ledger carry). A ref change or bundle that does not finish refuses as a failed one did. `_git_ok` no longer exists (Task 23 replaced its one use with `git_worktrees.listing`).
- `disk_worktrees`: `_out` is `out(error=ValueError)`, git's stderr or `git command failed` as before, and `str(GitFailed)` for a timeout, at `NORMAL`. `lock_at` is a `mutate`, so the best-effort relock raises nothing. `merged` asks with `no=(1,)`; an unknown is `ancestry unavailable` / `merge-tree unavailable` as before. `disk_registrations` imports `_out` in place of `git`.
- `disk_registrations`: `config --get` and `rev-parse --verify --quiet` ask with `no=(1,)`, and an UNKNOWN now raises ValueError (it read as "unset" and "no such branch" for any exit, and raised for a timeout). The report-only `suggest` reads go through `_out`. reregister's cleanup removal is a `mutate`.
- `disk_paths.repository_config`: both reads `ask` with `no=()`; anything but a clean YES (and, for `config --list`, no stderr) returns the fail-closed True, as before. No GitFailed is raised there; the `OSError` tuple stays.
- `dispatch_hold`: the injected `git` is shaped like `git_client.ask`, which is the default, with `no=()`; only YES gives stdout, so an unknown diff is never cached as "changed nothing".
- `task_access`: `_out` is `out(error=ValueError)` (`read checkout: <stderr tail>`, or `str(GitFailed)`), so a timed-out local ref falls through to the remote ref at lines 58-63.
- `decisions`: `_out` is `out(error=DecisionError)` at `QUICK` (30 s, as before), worded as before: git's words for an exit, `git: timed out after 30 s`, `git: <detail>` when git cannot start.
- `brief.framework_text`, `context_files._read_all`: `ask` with `no=()`; anything but YES lists nothing, as a non-zero exit did. `context_files` reports a listing that did not end as `unreadable: git timeout` (a raised TimeoutExpired gave `unreadable: TimeoutExpired`).
- `repos.load`: three `out(error=RepoError)` reads at `QUICK`, with today's messages.

Handlers, `file:line, what it catches, what now reaches it, change`:
- landing.py:209 `run_batch`, `ci.CIError` around `local_fast_forward` (`ci.problems`, `ci.head`): CIError for any git failure, as before. None.
- landing_batch.py:158 `ci.head` in `build_tip`: CIError, reaching landing.py:85 as before. None.
- pipeline.py:81, :113, :482 (`changed_docs_only`, `dirty`, `head`): CIError as before; :213 `commit_ready` catches it. None.
- landing_control.py:43 `repos.load` in `local_fast_forward`, `(RepoError, OSError, ValueError, KeyError)` → CIError: a timeout is now RepoError (TimeoutExpired escaped it before). Fixed.
- cleanup.py:104 `delete_branch`, `(GitError, OSError)`: now also gets GitError for a read that timed out or could not start, and RegistrationsUnknown from the listing as before. A raw timeout no longer escapes to hub/services.py:242. Fixed.
- disk_worktrees.py:234, :253, :278, :469, `(OSError, ValueError, SubprocessError)`: ValueError for every `_out` failure, a timeout included (TimeoutExpired before, caught by the same tuples). None.
- disk_worktrees.py:370 `remove`, `(OSError, ValueError, SubprocessError)` (section 5.2's :381): a git miss after `unlock_at`, a timeout included, is ValueError; `lock_at` then relocks through `mutate`, which raises nothing, so `failed` is always journalled. Before, a relock that timed out raised TimeoutExpired out of the handler: no `failed` row and the worktree left unlocked. Fixed; pinned by `test_git_failure_after_unlock_restores_the_marker_and_journals_failed`.
- disk_worktrees.py:378, `OSError` around the `failed` append: unchanged.
- disk_registrations.py:69 (was :66) `protect`, `ValueError` around `config gc.worktreePruneExpire never`: ValueError from `_out`, as before. None.
- disk_registrations.py:234, :245 (`report`), `(OSError, ValueError, [RuntimeError,] SubprocessError)`: ValueError now also for an unknown `rev-parse` in `candidate` (the folder is unproven, as a timeout made it). Same row.
- disk_registrations.py:282 `reregister`, `(OSError, ValueError, RuntimeError, SubprocessError)`: the cleanup removal is a `mutate`, so a slow removal no longer replaces the original error, and `failed` is journalled. None to the caller.
- disk_worktrees.py:278 `registrations` (calls `protect`): an unknown `config --get` is now a repo error, and the repo's orphans are not judged (it read as unset before). Towards keeping.
- disk_cleanup.py:43 `scope_key`, `(OSError, ValueError)` around `outputs_at` → `repos.load`: a timeout is RepoError, so the policy reads `<unavailable>` (TimeoutExpired escaped scope_key before). Towards keeping: the scope differs, so no report authorizes against it.
- disk_paths.py:84 `repository_config`, `(OSError, ValueError, SubprocessError)` → True: `ask` raises nothing for git's failure; CommandTooLong (ValueError) still reaches it. None.
- dispatch_hold.py `tick_holds`, `Exception`: unchanged; git failures no longer reach it (they are None).
- dispatcher_finish.py:219, `(ValueError, OSError)` around `task_access.verify`; replan.py:106 (`prepare` in `_inspect`) and :155, `(OSError, ValueError, WorktreeRefused)`: a timeout is now ValueError (TimeoutExpired escaped both before). Fixed.
- dispatcher_admission.py:389 `_worktree`, `Exception` around `task_access.prepare`: a timeout is ValueError, needs-owner `worktree failed: ValueError(...)`. Message only.
- config.py:464 `config sync`, `decisions.DecisionError`; decisions.py:99 (was :88) push, `DecisionError` → warning; wizard.py:122 tuple naming DecisionError: DecisionError for any failure, as before. None.
- context_files.py:120 `Exception`; brief.py `framework_text` (no handler): a timeout no longer raises; see above.

Line counts: ci 493 to 491, disk_worktrees 483 to 482, disk_paths 490 to 488, disk_registrations 289 to 294, cleanup 141 to 152. Trace file unchanged.
