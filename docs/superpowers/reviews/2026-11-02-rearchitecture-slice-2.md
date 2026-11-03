# Slice 2: `durable`

`SLICE_BASE`: 9b4016b. Commits: 5dd6c31 (Task 11), d9738d8 (Task 12), and Task 13's. 59bd4ca, between the last two, is the slice 0 checkpoint report.

## Landing state

- Every temp-then-replace writer and file move in `drover` goes through `durable`, except `placement_git._METADATA_IO`, which runs inside WSL as text and which R2 cannot see. Its text is unchanged, and a docstring below it says so.
- R2 holds four rows, none temporary: `disk_state.CleanupLock._reclaim`, `task_access.prepare`, `hub/runtime.prepare_release` (three stated renames) and `hub/reload_protocol.atomic_json`.
- `atomic_json` stays local (ruling 15). Its one production caller, hub/runtime.py:483, writes into a private `mkdtemp` staging directory, so it takes no retry. `hub/reload_protocol.py` is untouched, and its `OVER` pin of 571 stands.
- `write_json_atomic`, `_retried` and `_outlast_readers` are gone from `drover` and `hub`, and all three are in `architecture_removed_symbols.json`.
- `compaction_hold.claim`, `owner.json` and `reload_protocol`'s create-once requests stay as they were.
- `private=True` stands at four sites: `disk_state._save` (for `_write` and `save_file`, and so `compaction_hold`), `config._migrate_disks` and `placement_git._native_metadata_io`.

## The wait at each site, Task 13

Audited by AST, `git show 59bd4ca` against the commit. Nothing lost an fsync, and nothing gained one.

| Site | Base | Now |
|---|---|---|
| `allowance.set_manual` | no fsync, no wait | `write_json`, `sync=False`, ORDINARY, `indent=None` |
| `handoff.write` | no fsync, no wait, `newline='\n'` | `write_text`, `sync=False`, ORDINARY, `newline='\n'` |
| `wizard_files.write_file` | no fsync, no wait, `newline='\n'` | `write_text`, `sync=False`, ORDINARY, `newline='\n'` |
| `launch_handle.Tail.flush` | no fsync, no wait, swallows `OSError` | `write_text`, `sync=False`, ROUTINE, swallows `OSError` |
| `config._migrate_disks` | fsync, no wait, 0600 | `write_text`, `sync=True`, ORDINARY, `private=True`; the backup's own fsync stays |
| `disk_schedule.install` | fsync, no wait, umask mode | `write_text`, `sync=True`, ORDINARY |
| `placement_git._native_metadata_io` | fsync, no wait, 0600 | `write_text`, `sync=True`, ORDINARY, `private=True` |
| `handoff.follow`, `disk_cleanup._Progress._log` | one attempt | `durable.move`, one attempt |
| `disk_registrations.reregister` | one attempt | `durable.move`, `wait=durable.NO_WAIT` |

## Changed trace rows

None. `git diff --exit-code 9b4016b -- tests/disk_decision_trace.json` prints nothing.

## Command trace

No change. `tests/placement_trace.json` has no diff, and `_METADATA_IO`, the one script it could carry, compares equal by AST.

## Handler lists

No `except` clause changed in Task 13. `durable.move` and `durable.write_*` raise what the replaced calls raised, so `Tail.flush`, `handoff.follow`, `_Progress._log` and `reregister` catch what they caught. A refused write that once failed at once now fails after its wait, always with `PermissionError`.

## What a checkpoint must know

1. `publish` and `sync_dir` are gone, by the lead's ruling on N10's precedent: `sync_dir` lost its one planned caller, section 5.4's `atomic_json` row, to ruling 15, and `publish` then had one caller. Its replace is folded into `_land`, and section 5.4's text stays (N11). `drover.durable.publish` is in `architecture_removed_symbols.json`. `hub/runtime` and `hub/reload_protocol` keep their local `_sync_directory`.
2. `"tail.tmp"` left `FRESH_FILES`, as `Tail.flush` no longer writes that name. `_fresh_run_dir` unlinks exact names, so a reused run id does not clear a killed writer's `tail.log.XXXX.tmp`, as it never cleared the run files' unique temps. Nothing reads a temp in a run folder.
3. Fixed temp names became unique ones at allowance, handoff, wizard_files, the tail and disk_schedule. A process killed between the temp and the replace leaves one `<name>.XXXX.tmp` that no later write overwrites, where the fixed name was reused. All such names end `.tmp`, so no glob over those folders reads one, and no stage selects or measures under `state_dir`. The state folder can be a private git repo, and `decisions._sync` runs `git add --all`, so such a temp could have been committed and pushed. `IGNORE` now holds `*.tmp`, and `_sync` tops up an existing `.gitignore`, since only the wizard calls `initialize`. The first sync of an existing repo commits that one line; a real `config sync` there writes a decision record listing `.gitignore`. A temp already committed stays tracked. `stats.prune_logs` removes exact names, so a leaked `tail.log.XXXX.tmp` outlives the 30-day prune, as `tail.tmp` did.
4. Section 5.4's `atomic_json` row and its `test_reload_protocol` line are superseded by ruling 15, and the spec text stays (N11).
5. For the efficiency review: `Tail.flush` now pays `durable`'s chmod on POSIX, on the 67 to 126 ms a write carried from Task 11 if a tail sits on 9p. Windows pays nothing.
6. Task 12's review named wait and sync pins as missing for the ordinary run files. Task 13's sites each have one: `tests/test_durable_sites.py`, `tests/test_durable.py` and `tests/test_disk_schedule.py`. The 16 run-file sites of Task 12 are still unpinned.
7. Carried to Tasks 30 and 43: `append_manifest` has no retry during a long state lock hold (Task 12 review, Minor 1).
