# Slice 5: the stage run

`SLICE_BASE`: 16a3623. Commits: Task 30's, then Tasks 31 to 36.

Changed decision trace rows, cumulative, by `python -m tests.disk_trace --compare` against `SLICE_BASE`:

| Task | Scenario | Part | Category | Row |
|---|---|---|---|---|
| 34 | `settled_past_the_line.applied` (new scene) | row | report_added | `<TMP>/repos/alpha`, keep, `main_checkout` |
| 34 | `settled_past_the_line.applied` (new scene) | row | report_added | `<TMP>/worktrees/alpha.worktrees/w`, keep, `removal_deadline` |

No other row moved, and no row reads `towards_deletion`.

## Task 30: partial delivery proven; `StageRequest` and `StageClock`

### Step 1: partial delivery through a real `wsl.exe`, by hand

Run 2026-11-03 on Windows 11 Pro 10.0.26300, WSL 3.0.3.0 (kernel 6.18.54.1-1), distro Ubuntu, coordinator Python 3.13.5 (`H:/repos/drover.deploy/hub/.venv`), at 16a3623 plus the uncommitted Task 30 tree. The script was a throwaway in the session scratchpad and is not committed (ruling 6). Nothing touched `C:\Users\johnm\.drover` and no cleanup ran.

**Command.** The coordinator was `process.run` itself, so the kill was the one a stage's worker gets on its timeout: the whole tree ended through the kill-on-close job object, then a drain of at most `DRAIN_S`. The command, as `process.run` received it:

```
process.Command(process.wsl_argv(['-d', 'Ubuntu', '--', 'python3', '-c', STAGE, '/mnt/c/.../scratchpad/t30/t30-stage-XXXX/sent.log']), TIMEOUT)
```

`STAGE` wrote 60 lines, one a second, each `{"v":1,"row":{"n":<n>,"t":<seconds since start>}}`, with `sys.stdout.flush()` after each, and appended the same line to `sent.log` in the temp tree with `fsync`, as the record of what was sent. WSL was warmed first with `wsl.exe -d Ubuntu -- true` (0.12 s and 0.15 s), so the timeout measured the stage, not the VM's start.

| Trial | Timeout | `Result.kind` | Returned after | Lines sent (`sent.log`) | Lines held (`Result.out`) | Partial last line | `sent.log` 4 s and 7 s after the kill |
|---|---|---|---|---|---|---|---|
| 1 | 6.5 s | `timed_out`, `timed out after 6.5 s` | 6.52 s | 7 (n 0 to 6, t 0.000 to 6.079) | 7, identical byte for byte, in order | none | 7, 7: the Linux stage ended with the tree |
| 2 | 4.3 s | `timed_out`, `timed out after 4.3 s` | 4.32 s | 5 (n 0 to 4) | 5, identical | none | 5, 5 |

**Result.** No row was lost: every line the stage flushed before the kill was held by the coordinator, including the one flushed 0.42 s before the trial 1 kill (n 6 at t 6.079 s against 6.5 s). `wsl.exe`'s relay held nothing back at a flush boundary, and the kill returned within 0.02 s of the timeout, well inside `DRAIN_S`. The Linux side stopped writing when `wsl.exe` was killed, so no stage outlived its coordinator here. The pipe route stands; the file-route fallback is not needed.

What this does not show: a kill in the middle of a large line's relay (each line here was 33 bytes), and a cold WSL start, which the warm-up excluded. `read_rows` discards a trailing partial line (Task 31), which covers the first.

### Steps 2 to 6

`drover/disk_stage.py` is new, as section 6.1, holding `StageRequest` and `StageClock` only; `StageRun`, `read_rows` and `StageAnswer` come in Task 31. No caller moves in this task.

- **Wire.** `to_wire` gives today's keys: the field names map `policy` to `paths`, `inventory_id` to `_inventory_id` and `worker_id` to `_worker_id`. An optional field (`mode`, `budget`, `approved_path`, `inventory_id`, `worker_id`) that is None is left off the wire, as today's dicts leave it off (stage 4's approved request has no `mode` or `_inventory_id`; stage 6 has no `mode`). Every reader of those keys today uses `.get`, so a key holding None and a key left out read the same. Today's stage 1 trim writes `budget=None` explicitly; on the wire that key will now be absent, which every reader treats the same. `to_wire` and `from_wire` copy deeply, and `repos` is a tuple on the request and a list on the wire.
- **`for_side(side, convert)`.** Today's crossing translation at disk_runtime.py:32, :36 and :39-58: the side set, `state_dir`, the task snapshot's worktrees and each repo's `repo` and `worktrees` converted, and the temp roots, report roots and caches filtered to `side` and then converted. It does not decide whether to cross: `cleanup_call` still calls it only for a WSL worker started from Windows (Task 33). `test_for_side_matches_todays_translation_with_spaces_and_non_ascii` compares it with a frozen copy of that code over paths with spaces, `ö`, `ü`, `é`, `ï` and a `\\?\` prefix. The copy was checked once, by a scratch script that ran today's `cleanup_call('wsl', ...)` with `process.run` faked to capture stdin: the copy and `for_side` each equal what `cleanup_call` sends, over the four requests the test uses.
- **`decide_view()`.** The wire dict, with `approved_path` always present (None when unset), so `dict(view, approved_path=None)` and `view.get('approved_path')` both work as on today's dict. A fresh copy each call.
- **`StageClock`.** One `clock()` reading at construction. `late`, `left`, `remaining`, `patience`, `can_mutate` as section 6.1 and the brief. `mutation_cap()` is computed once, at construction, as `min(60, (timeout_s - timeout_s * SHARE) / MUTATION_STEPS)`: the margin is subtracted rather than multiplied by 0.2, so 100 s gives exactly 4.0 (multiplying gives 3.9999999999999996, and the brief's `assertEqual(..., 4)` would fail).
- **`can_mutate`.** `not late() and left() >= MUTATION_STEPS * mutation_cap()`, exempt always True (fix round 1, the lead's ruling, recorded in Appendix C). No removal begins past the 80% line even if a stage omits its own `late()` check: at the default 1,800 s it is False from 1,440 s, though five 60 s steps would still fit until 1,500 s. At 11 s, five 0.44 s steps fill the 2.2 s margin exactly, so the two conditions coincide.
- **`within(budget_s)`** (judgement, as section 6.1 says only "the temp stage's per-root budget under the stage clock"). It gives a non-exempt clock with the same `clock`, worker timeout and mutation cap, whose line is the earlier of `budget_s` from now and the stage's own line (the worker timeout, when the stage clock is exempt). So `late()` on it is the per-root budget or the stage's line, whichever comes first, and a per-root budget binds even under an exempt clock. Today's `disk_temp` check is strict (`>`); `late()` is `>=`. Task 35 moves `disk_temp` onto it.
- **R5.** `DIRECTIONS` already held `disk_stage: only('placement_paths')` from Task 3; it is live now that the module exists. `test_seams.Seams.test_disk_stage_imports_only_placement_paths` pins it, as for `process` and `git_client`: no R5 row, no project import but `placement_paths`, and only the standard library otherwise. No `getattr` in the module, so the dynamic entry point inventory is unchanged; no allowlist row.
- **Trace.** Untouched.

Mutants a pre-merge gate should run (none run here, by the memory rule): `>=` to `>` in `late` (the 80 s case), `max(0.0, ...)` dropped in `left` and `remaining`, `FLOOR_S` dropped from `patience` (the 1,439 s row), `min(60.0, ...)` dropped from the cap (1,800 s gives 72), `self._exempt or` or `not self.late() and` dropped from `can_mutate`, `inner._exempt = False` dropped from `within`, the side filter dropped from `for_side`'s caches, and `_OPTIONAL` emptied (the absent-keys test).

## Task 33: carried notes

- **`native_bad_json`'s input changed** in `tests/placement_trace.py`, from `'not json'` to `'not json\n'`, so the scene still records the malformed-line branch: a complete line that is not a stage line gives `worker_failed` with `not a stage line: not ASCII JSON`. The old input, unterminated text at exit 0, now takes the "ended without its end line" branch. The trace no longer records that branch; `test_disk_worker_status.test_exit_zero_without_an_end_line_is_a_worker_failure` covers it.
- **Row order.** Unstamped rows, the process rows among them, now cross before the root-grouped rows, because `read_rows` lists them first. Before, they followed compact's groups. No decision reads the order, and the decision trace sorts rows.

## Task 34: stages 4 and 5 and the budget purge on the run

- **Stages.** `settled(run)`, `outputs(run)`, `purge(run)` and `disposable_rows(run)` take the `StageRun`. Their five stop checks (`disk_paths` at `disposable_rows` twice, `disk_worktrees.settled` twice, `disk_budget.purge` once) read `run.clock.late()`, and each deadline ends with `run.stop(reason, machine, left, later)`, the one deadline row. `purge` reads `run.stopped`, not `out['items']`, to learn that gathering ran out of time. `disk_budget.deadline`, `_clock`, `DEADLINE_SHARE` and `disk_paths.deadline_row` are gone and listed in `qualified_names`, with `disk_inventory.compact` and `disk_side._serve` from Task 33's review.
- **Rows.** `disposable_rows` hands each candidate to its consumer unadded; `outputs` adds it after `remove()`, and `purge` adds each when decided (removed, chosen in a dry run, or kept with the outcome's reason). Kept rows are added where they are made. `remove_output` counts bytes on `run.reclaimed` as each path goes.
- **Clock.** `serve` builds the clock as `clock_factory(worker_timeout_s, exempt=bool(approved_path))`, so an approved removal's clock has no line and is never cut off. `disk_worktrees.remove(..., clock=)` returns `removal_deadline`, kept, unless `clock.can_mutate()`, just before journal `planned`; it writes no journal row and touches nothing. `WorktreeEvidence` and `unlisted` carry the stage's clock to it.
- **Checkpoints** run between removal units only: at the start of each listed worktree in stages 4 and 5, after each applied output unit, after each purged folder, and after each repo's unlisted folders. None runs inside `remove` or `remove_output`.
- **Bridge.** Stages 4 and 5 left `disk_side._bridge`. Until Task 35, `disk_paths.stage_dict(run)` builds the request dict the stage helpers still read: `decide_view()` with the evidence's inventory, listings, first-pass proof and diagnostics. So `decide` receives `decide_view()` plus those underscore keys; it reads only `apply` and `approved_path`. The bridge for stages 1, 2, 3 and 6 uses the same builder.
- **HOLDING.** `removal_deadline` joins `HOLDING` in `tests/disk_trace_compare.py` in this commit, as the HOLDING ruling requires, with the new stage-4 scene `settled_past_the_line`. In that scene the line passes as the settled worktree's removal starts. Before this task that worktree was removed; now it is kept with no journal row.
- **Why so few trace rows moved.** The trace's clock reads 0 unless a scene jumps it, so starting the clock earlier changes nothing there. The jumping scenes (`output_deadline_*`, `budget_deadline_mid_purge`) stop at the same checks as before.
- **Seams.** R6 rows fall: `disk_budget.purge` (now imports `disposable_rows` at load) and `disk_paths.outputs` are deleted; `disk_paths.disposable_rows` and `disk_worktrees.settled` drop `disk_budget`, each to 1.
