# Drover dispatcher and ledger notes

Detail moved out of the README. Design: [design.md](design.md); ADR 0001 (the Windows hub is the only
process that opens the ledger). Paths below assume the repo root.

Python 3.12+, standard library only. Tests: `python3 -m unittest discover -s tests -t .` (from the repo root).

## `drover.ledger`

Domain library over sqlite3 (WAL, foreign keys, busy timeout): `ops.py` (pure functions taking a connection; each
mutation is one `BEGIN IMMEDIATE` transaction), `states.py` (the one transition table), `scope.py` (glob overlap),
`board.py` (BOARD.md), `schema.py` (forward-only migrations).

    DROVER_LEDGER=/tmp/x.db python -m drover.ledger init
    python -m drover.ledger --db x.db add task "Title" --scope 'src/**' --priority P1
    python -m drover.ledger --db x.db approve T-1 --actor owner | ready | claim | block T-1 why | show T-1 | board [--out BOARD.md]

Default db: `$DROVER_LEDGER`, else `<state dir>\ledger.db` on Windows (the state dir is `%USERPROFILE%\.drover` unless the config says otherwise); in WSL it refuses and points at the hub.

## Dispatcher (ADR 0002)

The dispatcher runs as a thread inside the Windows hub (`hub/dispatch.py`, `DirectPort`, one ledger connection per
thread) and starts every worker through `drover.runner --detach`: Windows placement runs the hub's python directly,
WSL placement goes through `wsl.exe --exec` (`runner.wsl_argv`, `--cd` to this directory so `drover` imports).
Placement: Codex always WSL; `platform: linux` (task field, `platform:linux` tag, or that text in title/goal) WSL;
dotnet/msbuild: Windows with a Claude seat, or WSL with a Claude or Codex seat (owner rule, 2026-10-17; Codex never
takes .NET on Windows), within the repo's `[run] platforms` (a WSL-only repo puts .NET on WSL; a Windows-only repo keeps
Windows + Claude), and with no such seat the task goes to the owner naming the rule; Claude otherwise Windows. Run files live in `<state dir>\runs\<id>\`.

- One instance: the hub holds `<state dir>\dispatcher.lock`; `python -m drover.dispatcher` refuses while it is held and
  refuses to run at all off Windows (the lock is not shared between Windows and WSL).
- Restart: open runs are adopted by run id from `runner.find_runners()` plus `wsl.exe --exec python3 -m drover.runner
  find --json` (WSL only when a WSL run is open, or a run's placement, recorded in a `run-launching` event before the
  launch, is unknown, in which case both sides are scanned). The dispatcher picks the launch nonce, records it in the `run-launching` event and passes it as `runner --nonce`, so a
  stop-request can be written before status.json exists and a late runner honours it. A launch that timed out gets one and its task is
  requeued only after a scan shows no runner for that run id. A finish that raises is retried (3 tries) and the gate runs
  at most once per run (`gate-started`/`gate-done` events). A failed scan is unknown and requeues nothing; an open run with no
  runner and no terminal `status.json` is orphaned after 2 consecutive clean scans.
- A task with no eligible backend or no placement is skipped for the tick and backed off 1, 2, 4 ... 30 minutes
  (hidden setting `_dispatch_backoff`); `ops.claim_next(..., exclude=ids)` keeps it from blocking the queue.
- `drover/contract.py` is the one definition of the run outcomes and of the shapes the dispatcher writes and the
  tuner reads (`runs.allowance_used`, `runs.gate_receipt`, `task_events` kind `gate`, `decisions.outcome`).
- The hub's WSL-facing API listener is off unless the `api_listen_wsl` setting is true. The watchdog only boots WSL
  when the dispatcher left a `_wsl_needed` note, and holds it up with a tracked `wsl.exe --exec sleep` keepalive until
  that note expires. The systemd unit is gone.
