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
WSL placement goes through `wsl.exe --exec` (`RunnerLauncher.runner_argv`, `--cd` to this directory so `drover` imports).
Every dispatcher requires a `Pipeline`; workers finish into checks, then review, before the merge queue. The unused
`--no-gate-dry-run` CLI flag and synchronous `gate_fn` mode have been removed. To observe recovery without admitting
work, keep `dispatch_enabled` false; paused dispatch still reaps and adopts runs.

Placement is decided before the worktree is made (`place()`), and a new WSL placement gets its worktree on the
distro's ext4 (`paths.wsl_worktrees`, ADR 0007) while a Windows placement gets the Windows root. A task keeps the
worktree it has, so a task that started in WSL and later escalates to a route that needs Windows (a `platform: windows`
route or tag) cannot follow it: `place()` returns needs-owner ("its worktree is on the WSL filesystem") instead of
launching a Windows worker in a Linux path. Escalation is not re-routed to a WSL-capable seat automatically; the owner
requeues it. Windows Claude seats may run in WSL, so ordinary escalation between Claude seats is unaffected.

`dispatch_repos` (ledger setting, JSON list; an absent key means every repo) limits dispatch to the named repos
(matched case-insensitively). Every other repo stays idle: no claims, gates, reviews, replan planners or landing
batches, including for work already finished (it waits, as under pause, and resumes when the filter clears).
`dispatch_enabled` false still pauses everything. The filter fails closed: an invalid value (bad JSON, a bare string, an
empty list) blocks every repo and logs an error each tick; only `clear` (which deletes the key) restores all repos.
`ops.claim_next` reads it itself, so the hub claim route and the ledger CLI obey it too. Tasks already claimed or
running when it is set keep running and are reaped; their gates and reviews wait as described above. Owner-only:
the token settings route refuses the `dispatch_` prefix, so set it with
`python -m drover.dispatch_repos [--db PATH] show | set REPO... | clear`; the hub header shows "only <repos>".

`Dispatcher(port, pipeline, runtime=None, usage=None, route=route.choose, decide=...)` takes the ledger and pipeline,
an execution environment (`DispatchRuntime`: launcher, resource checker, clock), the measured per-seat usage poller,
the router and the decider. Worktree operations belong to the pipeline; scans and WSL readiness belong to the launcher.
The router always receives seat load, repo platforms, measured seat headroom and seat ordering. Seat routing and
allowance accounting both use `drover.usage`; legacy allowance-file admission is no longer a dispatcher path.

Placement rules:

- Codex always runs in WSL.
- A task's explicit platform constrains placement.
- The repo's `[run].platforms` lists allowed platforms; `[run].default` supplies the default.
- Tasks with no eligible seat or platform go to the owner with a reason naming the constraint.

Repo rules come from `.drover/config.toml` at the task or CI commit, followed by the private repo registration's
local facts and env. Adopted repos reject machine policy; absent entries retain machine fallback. Only `drover.repos` loads these keys. `[scripts]` maps action names to relative PowerShell paths.
Each runs as `pwsh -NoLogo -NoProfile -NonInteractive -File <script>` with the worktree as cwd;
no command is inferred or rewritten for a platform. The script's exit code and output tail form the result.
`DROVER_CI_KIND` distinguishes `checks`, `full`, and `docs`; `DROVER_CHANGED_FILES` is a JSON path array.
Repo scripts own scoping and tool environment. `[run.env]` provides machine environment values, while
`[run].sandbox_writable_roots` names additional writable paths below `/tmp`, created with mode 0777.
PowerShell 7 must already be installed on every platform selected for actions.

Run files live in `<state dir>\runs\<id>\`.

- CI runners: hub settings `gate_slots` (default 5, clamped 1..32) and `gate_reserved` (default 1, clamped 0..slots-1) set how
  many checks run at once; owner-only, read each tick, no restart. An invalid value logs once and uses the default. The
  heartbeat carries `gate_queue: {waiting, oldest_s, slots}`. Merge groups (`run_leased`) keep the default slot count.
- One instance: the hub holds `<state dir>\dispatcher.lock`; `python -m drover.dispatcher` refuses while it is held and
  refuses to run at all off Windows (the lock is not shared between Windows and WSL).
- Restart: open runs are adopted by run id from `runner.find_runners()` plus `wsl.exe --exec python3 -m drover.runner
  find --json` (WSL only when a WSL run is open, or a run's placement, recorded in a `run-launching` event before the
  launch, is unknown, in which case both sides are scanned). The dispatcher picks the launch nonce, records it in the `run-launching` event and passes it as `runner --nonce`, so a
  stop-request can be written before status.json exists and a late runner honours it. A launch that timed out gets one and its task is
  requeued only after a scan shows no runner for that run id. A finish that raises is retried (3 tries). Checks and
  reviews are derived from ledger runs after recovery; infrastructure failures rerun checks on the same commit. A failed
  scan is unknown and requeues nothing; an open run with no runner and no terminal `status.json` is orphaned after 2 consecutive clean scans.
- A task with no eligible backend or no placement is skipped for the tick and backed off 1, 2, 4 ... 30 minutes
  (hidden setting `_dispatch_backoff`); `ops.claim_next(..., exclude=ids)` keeps it from blocking the queue.
- `drover/contract.py` is the one definition of the run outcomes and of the shapes the dispatcher writes and the
  tuner reads (`runs.allowance_used`, `runs.gate_receipt`, `task_events` kind `gate`, `decisions.outcome`).
- The hub's WSL-facing API listener is off unless the `api_listen_wsl` setting is true. The watchdog only boots WSL
  when the dispatcher left a `_wsl_needed` note, and holds it up with a tracked `wsl.exe --exec sleep` keepalive until
  that note expires. The systemd unit is gone.
