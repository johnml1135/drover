# Disk tracker and disk cleanup: owner design (2026-10-20, grilled with the owner)

Why: H: (hosts the repos and WSL's ext4.vhdx) filled to under 1 GB and killed WSL. Drover's disk trackers only gate admission inside the hub, never alert, never run while the hub is paused or WSL is down, and the `wsl` tracker measured free space inside the VHDX (475 GB "free") while the host drive was full.

## Decisions
D1 Disk tracker = the cheap reading of one drive against its levels. Runs every 15 minutes from a Windows scheduled task (user-level, no admin, created by Drover's install; systemd user timer on Linux-only machines), independent of the hub and WSL; the hub also reads on every tick. Measure the real drive (H:, C:), never inside a virtual disk. Drop the inside-WSL `wsl` tracker (display at most).
D2 Levels per disk tracker, in machine config: warn, queue, hold, critical. Defaults H: 150/100/50/25 GB; C: 60/40/25/15 GB. queue = no new agent runs; hold = running agents wind down, no builds/tests start; critical = Drover pauses itself. Optional per-repo budget_gb (alert when over).
D3 Disk cleanup (`drover disk-cleanup`; rename drover/garden.py's role; never call it garden). Runs daily, on demand, and immediately when a disk tracker drops below warn. Stages, safest first:
  1 trim: fstrim WSL via `wsl.exe -u root -- fstrim -v /` (no admin, no sudo); also re-trim after any stage that freed a lot.
  2 temp: OS temp, Drover scratch, agent scratchpads untouched 7+ days.
  3 tool caches: each tool's own clear/prune command, declared in machine config (uv, pip, npm, nuget http-cache, cargo; unreferenced Ollama blobs).
  4 settled worktrees: clean AND (merged, or squash-merged by git cherry / merge-tree, or owning task landed/cancelled, or untagged/lease-lapsed orphan after a 3-day quarantine). Also: clean folders inside *.worktrees that git does not list; registered entries whose folder is gone (this side only; remove the entry by path; never `git worktree prune`). Branch kept; restore entry (exact `git worktree add`) written to a manifest.
  5 declared build outputs in idle worktrees: paths each repo lists in its .drover/config.toml ([cleanup] build_outputs = [...]); no language/.NET knowledge in Drover.
  6 report: everything else listed for the owner.
  Daily run = stages 1-4. Triggered run walks stages in order, next stage only while the drive is still below warn. If all stages ran and it is still below warn: Windows toast + owner inbox item (owner said: just act; alert only on exhaustion).
D4 No re-clean without change (owner, 2026-10-20): if a triggered cleanup leaves the drive below warn, do not trigger again on the next readings unless free space has changed (dropped further by a meaningful amount since the post-cleanup reading) or the drive rose above warn and fell again. The daily run still runs.
D5 Never removed automatically: branches; dirty worktrees; anything outside declared paths. Report only: dirty idle worktrees (owner, file count, age), lock notes that are not side markers, unowned folders in declared roots (~/work etc.), .venv dirs, older model versions, loose files (logs/patches/dumps/zips) in ~ and the top of the repos root, large untracked data in repos.
D6 Scope: every repo in the machine config plus extra paths declared in machine config; both Windows and WSL sides (each side handles its own worktrees per the shared-repo lock convention: lock reason `windows`/`wsl`; never touch the other side's).
D7 Ownership tag at worktree creation: owner (task / lane / session) + lease expiry. Drover tags its own; others use `drover worktree add` or write the same tag file. Untagged or lapsed -> quarantine -> removed after 3 days if still clean.
D8 One machine-wide cleanup lock in Drover's state dir, taken by every cleanup (Drover, the PowerShell Cleanup-StaleWorktrees script, herdr garden while it still runs).
D9 Compacting the VHDX (needs admin and WSL shutdown) stays a manual owner command, out of the automatic path.
D10 First live run on this machine is report-only (dry run), then apply.
D11 herdr's garden.py keeps running until Drover's daily disk cleanup has had one clean week; Drover does not modify herdr.

## Glossary
Tracker (four levels), Disk tracker, Disk cleanup, Ownership tag: see GLOSSARY.md.
