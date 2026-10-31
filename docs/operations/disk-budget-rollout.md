# Disk budget and compaction rollout (owner runbook)

Run these steps in order. Nothing here is done by the build; each step is the owner's.

1. **Deploy the hub.** Follow `docs/hub-deployment.md` ("Each deploy"): `python scripts/deploy_hub.py --deploy <repos>/drover.deploy --receipt <gate-receipt.json> --restart`.
   The budget purge, the 500 GB stop and `local_disposable` take effect only in the deployed code.

2. **Set `wsl_combined = true` on the `h` disk tracker** in the global config (`[[trackers]]` entry with `kind = "disk"`, name `h`).
   Without it WSL used space is not read, the budget never triggers, and a `wsl` claim ends `no_host`.
   Check with `python -m drover config migrate-machine` (print only) before editing.

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
   not depend on PATH. Do this only after Phase 3 lands, or if you accept that interactive WSL agents get no stop message yet:
   until then the job stops them without notice when the hold begins.

6. **Retire `HerdrDiskGuard` after 3 clean applied days:** three consecutive calendar days of successful applied daily reports for
   the same scope, with no cleanup errors or protected mutations. A missing, failed or scope-changed day restarts the count.

## Behaviour to expect

- **A symlink in a worktree makes the idle proof fail.** That worktree is then treated as not idle and keeps everything, including
  its disposable paths, until the symlink is removed.
- **One unreadable Windows process blocks a budget purge.** Any WSL worktree could be the one that process has open, so the run
  purges none and reports `processes_unknown` with the process names. Add the image basename to `[disk_cleanup] process_ignore`
  (for example `process_ignore = ["SomeService.exe"]`; basenames only) once you know it cannot hold a worktree.

## Rollback

Disable the two disk schedules and `\DroverCompaction`, keep disk state and the journal for inspection, restore the configuration backup
and previous code.
