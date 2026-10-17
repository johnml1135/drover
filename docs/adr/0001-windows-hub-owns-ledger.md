# 0001: A Windows hub process owns the ledger; WSL talks to it over HTTP

Date: 2026-10-17 · Status: accepted (owner requirement; can be revisited)

## Context

The owner wants the website to run on Windows, because WSL can restart. At minimum, something on Windows must be
able to bring WSL back up. Two earlier decisions shape the options:

- The ledger is SQLite in WAL mode.
- WAL does not work across the 9P/DrvFs boundary between Windows and WSL.

So a SQLite file cannot be shared between a Windows process and WSL processes.

## Decision

**Drover Hub** is a single long-running Windows process (Windows Python 3.13). It does four jobs:

- It is the **only process that opens the ledger** (`<state>\ledger.db`, local NTFS, WAL).
- It serves the HTTP API and the website.
- Task Scheduler starts it at logon, with restart-on-failure.
- It runs a watchdog that restarts the WSL dispatcher, or WSL itself, when heartbeats stop.

WSL components (the dispatcher, workers and the CLI) read and write the ledger only through the hub's API. They
reach it at the Windows gateway IP, which they discover at runtime. A firewall rule scopes access to the WSL
adapter and the Tailscale interface.

## Consequences

- There is one writer, so there is no SQLite locking problem, and claims are atomic inside one process.
- The website and ledger keep running through WSL restarts, and Tailscale access keeps working.
- WSL needs the hub to be up. When it is not, the CLI fails clearly, the dispatcher admits no new work, and
  running workers buffer their handoff records locally and replay them later.
- The state directory defaults to `%USERPROFILE%\.drover` and is configurable through `[paths]`.
