# Disk budget and compaction rollout (owner runbook)

Run these steps in order. Nothing here is done by the build; each step is the owner's.

1. **Deploy the hub.** Follow `docs/hub-deployment.md` ("Each deploy"): `python scripts/deploy_hub.py --deploy <repos>/drover.deploy --receipt <gate-receipt.json> --restart`.
   The budget purge, the 500 GB stop and `local_disposable` take effect only in the deployed code.

2. **Set `host_floor_gb = 50` on the `h` disk tracker** in the global config (`[[trackers]]` entry with `kind = "disk"`, name `h`).
   `h` already has `wsl_combined = true`: the default tracker sets it and an entry named `h` merges over the default. So
   the deploy in step 1 is what turns on the WSL used-space reading, the `wsl` claim and the 500 GB hold; there is no
   switch to set first. `python -m drover config explain trackers` shows the resolved entry.
   The floor needs raising because the default of 10 GB rests on free space inside the VHDX being already allocated on H:.
   Nightly compaction returns that space, so WSL growth takes real H: space again. At the default, H: at 30 GB free with
   700 GB free inside WSL is `ok` for WSL work; at 50 it is held.

3. **Register the `machine` repo with `local_disposable`.** Add `local_disposable = ["obj", "bin/**"]` (the machine's real
   build output paths) to `<state>/repos/machine.toml`. It takes the same paths and globs as `cleanup.disposable`, and only this
   owner-written file can set it. A bad entry is a configuration error.

4. **Dry report, then applied schedules.**
   - `python -m drover disk-cleanup --dry-run --json` and review the report. A completed report saves the receipt that `--apply` needs.
   - `python -m drover disk-tracker install --dry-run`, then `install` (report-only), then `install --apply` after the matching report.
   - Install from native Windows Python.

5. **Register the overnight compaction.** From an **elevated** PowerShell:
   `powershell -File <repos>/drover.deploy/scripts/Install-CompactionTask.ps1`
   The task `\DroverCompaction` runs nightly at 03:30 with `<Root>\hub\.venv\Scripts\python.exe` (override with `-Python`), so it does
   not depend on PATH. The task is registered with `-LogonType Interactive`, so it runs only while you are logged on (a locked
   session counts); a night with nobody logged on is not compacted, and a missed night is not made up.
   Do this only after Phase 3 of the spec lands (directors in panes, which adds the pane stop that sends "stop now" to agents in
   WSL panes), or if you accept that interactive WSL agents get no stop message yet: until then the job kills them without notice
   at its kill step, as soon as the hub has drained its own runs. `compaction.pane_stop` is the empty hook for it.

6. **Retire `HerdrDiskGuard` after 3 clean applied days:** three consecutive calendar days of successful applied daily reports for
   the same scope, with no cleanup errors or protected mutations. A missing, failed or scope-changed day restarts the count.

## Behaviour to expect

- **A symlink in a worktree makes the idle proof fail.** That worktree is then treated as not idle and keeps everything, including
  its disposable paths, until the symlink is removed.
- **One unreadable process that `process_ignore` does not name blocks a budget purge.** Any WSL worktree could be the one that
  process has open, so the run purges none, reports `processes_unknown` and raises an Inbox notice naming it. Add the image
  basename to `[disk_cleanup] process_ignore` (for example `process_ignore = ["SomeService.exe"]`; basenames only) once you know it
  cannot hold a worktree. For a budget purge alone, on both sides, that name is taken for a process whose executable cannot be
  read; other cleanup stages keep their stricter rule. On this machine the default list covers the session 1 system processes a
  non-elevated hub cannot open. A same-user `sleep.exe` from Git Bash is not on it and shows up whenever a shell is waiting.
- **A budget outcome other than `met` raises one Inbox notice per outcome per episode.** A reading below `wsl_purge_gb` ends the
  episode. Report-only claims notify too.

## Rollback

Disable the two disk schedules and `\DroverCompaction`, keep disk state and the journal for inspection, restore the configuration backup
and previous code.
