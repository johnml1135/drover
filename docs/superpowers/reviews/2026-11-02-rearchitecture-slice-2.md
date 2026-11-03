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

1. `publish` has no production caller outside `durable._land`, and `sync_dir` has none at all: `_land` calls `publish` without it, and only `tests/test_durable.py` passes it. Section 5.4's `atomic_json` row was the one planned caller, and ruling 15 took it away. This is the case carried to Task 13's exit, N10's precedent; it is the lead's to rule.
2. `drover/launch.py:50` still lists `"tail.tmp"` in `FRESH_FILES`. `Tail.flush` no longer writes that name, so a reused run id does not clear a killed writer's `tail.log.XXXX.tmp`. Nothing reads such a file. `launch.py` was outside Task 13's list, so the entry stands: it still clears a temp left by a run from before this slice.
3. Fixed temp names became unique ones at allowance, handoff, wizard_files, the tail and disk_schedule. A process killed between the temp and the replace leaves one `<name>.XXXX.tmp` that no later write overwrites, where the fixed name was reused. All such names end `.tmp`, so no glob over those folders reads one.
4. Section 5.4's `atomic_json` row and its `test_reload_protocol` line are superseded by ruling 15, and the spec text stays (N11).
5. For the efficiency review: `Tail.flush` now pays `durable`'s chmod on POSIX, on the 67 to 126 ms a write carried from Task 11 if a tail sits on 9p. Windows pays nothing.
6. Task 12's review named wait and sync pins as missing for the ordinary run files. Task 13's sites each have one: `tests/test_durable_sites.py`, `tests/test_durable.py` and `tests/test_disk_schedule.py`. The 16 run-file sites of Task 12 are still unpinned.
7. Carried to Tasks 30 and 43: `append_manifest` has no retry during a long state lock hold (Task 12 review, Minor 1).
