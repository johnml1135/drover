# 0002: Windows is home; WSL is a worker pool

Date: 2026-10-17 · Status: accepted · Amends 0001

## Context

ADR 0001 put the ledger in a Windows hub. It still kept the dispatcher in WSL, talking to the hub over HTTP. That
cost an HTTP API port, a bearer token, a firewall rule, re-binding when the WSL IP changes, and heartbeats. Most of
the hub's security review findings came from that surface.

WSL is needed for only three reasons:

1. Some work is easier on Linux.
2. Linux testing.
3. Codex shelling out on Windows corrupts the Windows login registry.

## Decision

- **The dispatcher runs inside the Windows hub process.** It uses `DirectPort` on the ledger.
- **Every worker runs under one cross-platform runner:** `drover.runner --run-id <id> -- <cli …>`.
  - On Windows the hub starts it directly.
  - For WSL the hub starts it through `wsl.exe -d <distro> -- python3 -m drover.runner …`.
  - The runner owns the worker's process tree: a job object on Windows, a process group on Linux.
  - It enforces timeouts.
  - It writes `out.log`, `status.json` and `record.json` under `<state>\runs\<id>\`; the launcher maps this configured path for WSL.
- **Placement:**
  - Claude workers default to Windows.
  - Codex workers always run in WSL.
  - Tasks tagged `platform: linux` run in WSL.
  - dotnet and MSBuild: (Windows + Claude) or (WSL + (Claude or Codex)), within the repo's `[run] platforms`.
    Amended 2026-10-17 by owner override of the earlier rule (Windows, Claude only). Reason: Codex on Windows leaves
    login artifacts from shelling out, so Codex never takes .NET work on Windows; on WSL it is fine.
  - The router may override placement per task.
- **Adoption after a hub restart:** find runner processes by the `--run-id` marker in their command line (`/proc` on
  Linux, CIM on Windows) and keep reading their files. Never signal a process that lacks the marker.
- **The watchdog** only ensures WSL is up when a WSL worker is due.
- **The hub's WSL-facing API listener and token** are off by default and kept for later remote use.

## Consequences

- One process owns the ledger, the dispatcher and the UI.
- WSL restarts affect only the WSL workers in flight. Those are adopted or requeued.
- The runner must implement both a Windows and a Linux process model.
