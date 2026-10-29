---
schema_version: 1
id: ADR-0007
status: accepted
date: "2026-10-21"
decision-makers: [johnm]
applies_to: ["drover/**", "hub/**", "docs/**"]
enforcement: null
supersedes: []
links:
  - rel: amends
    target: docs/adr/0002-windows-home-wsl-worker-pool.md
  - rel: related
    target: docs/adr/0003-task-writes-one-repo.md
---

# One Git repository per repo, on Windows; worktrees mount it

## Context

Drover runs work in two placements, Windows and WSL (ADR 0002). We tried a Git clone on each side for the same repo
and went back to one repository with worktrees. Two clones meant two object stores, two sets of refs and branches,
fetches and pushes twice, and landing that had to reconcile two histories. One repository with worktrees has none of
that. The cost is that a worktree on the WSL filesystem must reach a `.git` that lives on the Windows filesystem.

WSL worktrees on `/mnt/<drive>` are slow (drvfs) and fill the Windows drive with build output. On the WSL ext4
filesystem they are fast and their space is reclaimable with the distro's VHDX.

## Decision

- **One repository per repo.** Each registered repo has exactly one Git repository: one common directory, one object
  store, one set of refs. That is the machine's primary clone, and it lives on the Windows filesystem (for example
  `H:\repos\<name>`), even for repos whose work runs only in WSL.
- **Drover never makes a second repository of a registered repo.** No WSL-side clone, no mirror, no per-task clone.
  Fetch, push, branch, landing and ref updates all run against the one repository.
- **Every checkout is a worktree of that repository**, created with `git worktree add` from it:
  - Windows-placed work: worktrees on the Windows filesystem, under the repo's Windows worktree root.
  - WSL-placed work normally uses the WSL ext4 filesystem, under `~/work` (for example
    `~/work/<name>.worktrees/<branch>`), never `/mnt/<drive>`. The native-gate exception below uses the shared root.
  - **Native-gate compatibility exception:** a newly created WSL task whose actual creation base's committed gate
    policy includes Windows uses the existing Windows worktree root. Its worker still runs in WSL. WSL-only gate
    policy keeps the ext4 root. Resolve the base first, including a fallback source branch tip, and read policy at
    that commit; dirty working files do not select placement. When an existing WSL-only worktree needs a Windows gate,
    a clean checkout under Drover's WSL worktree root is removed and recreated under the shared root on that same branch,
    after origin sync and destination checks succeed. A task event records the completed move. Other checkouts and
    uncommitted work are left untouched and need owner attention; ignored files in a moved checkout may be discarded.
    A shared root that Windows cannot reach is refused. If creation fails after removal, the branch remains available
    for recovery. WSL-only gates retain their existing worktrees. The placement layer creates portable pointers; there
    is no staging backend or second clone.
  - Read-only inputs (another repo or a pinned commit) are detached worktrees of that repo's one repository, or
    exported snapshots. They are not clones.
- **Assume `.git` must be mounted.** Any process that runs in a worktree — a worker, a gate, a sandbox — is given the
  worktree plus the repository's common directory (resolved through the worktree's `gitdir`/`commondir`). In WSL the
  common directory is reached through `/mnt/<drive>`. Sandboxes bind it explicitly; nothing assumes `.git` sits beside
  the worktree.
- **The owning side operates its worktrees.** An ext4 WSL worktree's `.git` file and the repository's
  `worktrees/<id>/gitdir` record contain WSL paths that Windows Git cannot resolve, and the reverse can hold for
  Windows worktrees. The shared-root exception uses the placement layer's portable relative pointers so both gates can
  resolve the checkout. Commands that inspect, repair, remove or prune worktrees run on the side that owns the
  worktree. Neither side runs `git worktree prune` over the other side's worktrees. Path translation stays in the
  placement module.

## Consequences

- One history, one landing path, one fetch. Disk cleanup reclaims worktrees and their build output on both sides from
  one registration list.
- Repo registration facts carry the Windows repository path plus a worktree root per placement; the WSL root defaults to
  `~/work/<name>.worktrees`. Config validation refuses a WSL worktree root outside the WSL filesystem.
- WSL work needs the Windows drive mounted in WSL. If WSL is down, WSL worktrees are unavailable (the existing
  `wsl-down` wait applies).
- Git object reads from WSL cross drvfs. That cost is accepted in exchange for a single history.
- The native-gate exception puts WSL worker I/O and build output on the Windows filesystem. WSL pays drvfs
  performance costs and the worktree consumes Windows drive space; this cost is accepted so Windows gates can open
  the task's exact checkout. Existing clean WSL-only worktrees move when their Windows gate requires it; dirty ones
  wait for the owner to commit or otherwise resolve the changes.
- Existing WSL worktrees placed under `/mnt/<drive>` before the native-gate exception drain: they finish in place and
  settled ones are removed by disk cleanup. They are not moved.
- Read checkouts are currently independent clones under `<state>/read-checkouts` (`drover/task_access.py`). They do not
  conform and are to be replaced with detached worktrees or exported snapshots.
