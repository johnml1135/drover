# Safe self-reload for Drover

Date: 2026-10-17. Status: proposed; recommendations used without an interactive owner.
Deliverable: research and specification only. Implementation is a separate backlog.

## Problem and intent

After Drover lands its own changes, the owner wants the running hub to use them without a lead killing and restarting it. Reload must preserve detached workers and reviews, exclude active landing and gates, and recover automatically if the replacement cannot start. The owner also needs one supported start command. The manual-restart pain is the task's supplied requirement, not an observation of a live process.

All **current-behavior** references below are relative to `drover@main`, fixed at `acd5b6b2853b39d08fc5a3e5e365c2f107843123`. `T-7:` references mean the read-only, uncommitted T-7 worktree inspected on this date; they are provisional, not main. Everything described as proposed or required is new behavior. During research, main advanced to `d10444a131a478991bf6afa4bae9d0910f4f21ae`: its `hub/dispatch.py:134-145` adds WSL error context and `drover/landing.py:160-172` creates/repairs relative worktree pointers. Those reviewed changes preserve the lifecycle/landing boundaries below; fixed-SHA references remain reproducible.

## Current behavior and consequences

| Evidence on main | Consequence for this design |
| --- | --- |
| `hub/__main__.py:113-125` creates the app, starts background work, runs listeners, and shuts down in `finally`. `hub/app.py:25-53` explicitly owns lifecycle outside Uvicorn. | Replace the whole hub child under a parent; listener rebind is not process reload. |
| `hub/__main__.py:43-49,76-110` runs a UI server plus an optional independently rebound WSL API server. | Shutdown must stop both listeners and cancel the adapter loop. |
| `hub/dispatch.py:235-250,271-278` takes `dispatcher.lock`, starts dispatcher and landing threads, joins with timeouts, and releases the lock if the dispatcher thread stopped. | Existing stop is not a drain proof: landing may still be alive after its join times out. |
| `drover/lock.py:29-49,52-65` implements Windows byte locking and POSIX flock. `drover/dispatcher.py:1305-1315` refuses the standalone dispatcher on WSL. | Keep one native Windows dispatcher lock; never use a WSL lock as proof of Windows exclusion. |
| `hub/dispatch.py:146-185` lands on a separate thread when dispatch is enabled; it runs the full batch call and clears `landing_batch` in `finally`. | Drain covers the entire batch, including gate, attribution, cleanup and recording, not the UI setting alone. |
| `drover/landing.py:271-283,313-341` takes a TTL database lock only around fast-forward; `drover/landing.py:487-499,538-556` gates first, then records landed tasks and the batch. | A missing/expired landing lock cannot establish idleness; detect successful landing only after the whole call returns. |
| `drover/landing.py:313-339` fast-forwards the clean checkout holding the local default branch, or uses compare-and-swap `update-ref`. `hub/dispatch.py:161-179` selects local vs remote base from push policy. | Match the checkout actually running Drover; a remote-only success does not prove local source changed. |
| `drover/launch.py:409-428,479-484` starts detached runners and adopts by files. `drover/runner.py:454-480` attempts Windows job breakaway but records `detached_in_job` when refused. | Do not kill process trees; refuse reload while a Windows runner may still belong to the hub's parent job. |
| `drover/dispatcher.py:987-1029,1031-1066` adopts open runs and retries unknown scans; only consecutive clean misses orphan a run. `drover/dispatcher.py:1195-1209` reaps and recovers even while paused. | Reuse recovery; readiness must distinguish completed recovery from an unavailable scan. |
| `drover/dispatcher.py:1218-1243` starts gates, reviews and claims at different points in a tick. | A single early pause check cannot close a race with a reload request; fence each admission boundary. |
| `hub/api.py:224-228,265-268` also exposes claim and run-start endpoints. | Fence those endpoints during drain/validation; fencing just the dispatcher is insufficient. |
| `hub/services.py:118-136,200-225,249-278` joins for ten seconds, processes garden work and resumes unfinished approvals; `hub/watchdog.py:76-89,118-120` manages its own WSL keepalive. | Drain services too; bridge WSL keepalive across replacement, without stopping WSL. |
| `hub/api.py:186-188` returns constant health `{ok,version}`. `hub/store.py:260-267` stores a persistent heartbeat. | Neither is sufficient to identify a ready replacement; old heartbeat data can survive a restart. |
| `hub/scripts/Install-DroverHub.ps1:13-24` registers a per-user logon task running `uv ... -m hub` with failure restart. `README.md:24-28` gives multiple setup/start commands. | Retarget the scheduled task to the same supervisor used by the new owner command. |
| `hub/store.py:49-66` migrates on first connection; `drover/ledger/db.py:72-88` applies forward migrations; `drover/ledger/schema.py:1,73` defines append-only migrations. | Do not promise rollback across arbitrary migrations or restore an old ledger over live work. |
| `hub/events.py:29-38,47-54` keeps SSE replay and IDs in process memory; `drover/ledger/schema.py:32-40,58` persists append-only events and settings. | Durable reload history belongs outside the SSE replay buffer; reconnect must resync the UI. |

T-7 coordination: `T-7:drover/ledger/schema.py:73-85` proposes `kind`, `write`, `read_access`, and task links. `T-7:drover/ledger/ops.py:192-215` defaults research to read-only unless write is explicit. `T-7:drover/brief.py:59-68,93-96` renders access in briefs. This research task explicitly permits documentation writes. The implementation backlog uses the requested existing JSON fields and an access header in every goal; it does not require T-7 or modify its migration. When importing after T-7 lands, implementation tasks use `kind=implementation`, `write=true`, `read_access=["drover@main"]` through the then-supported importer.

## Goals, boundaries and alternatives

Required: automatic reload after relevant successful self-landings, safe manual “Reload now”, persistent auto on/off, preserved owner dispatch preference, adoption without duplicate launches, rollback to a known healthy runtime, durable events, and a non-elevated Windows start command. No new packages or services.

Non-goals: zero HTTP downtime, arbitrary schema/dependency upgrades, automatic Git revert/reset, supervisor hot replacement, runtime garbage collection, changes to admission capacity or landing/push policy. Safety covers **hub-managed** gates and landings. Standalone `drover.landing` also exists (`drover/landing.py:567-601`); owners must turn auto-reload off before independent CLI gates/landings and finish them before “Reload now”. Concurrent unregistered owner commands are outside this protocol.

| Option | Evaluation |
| --- | --- |
| **Stable small parent loop, scheduled task as optional launcher (recommended)** | Keeps recovery logic alive if child imports fail; owns old/new identities and runtime selection. Requires a durable protocol and immutable runtime copies. Reuses the installed per-user task rather than adding a service. |
| In-process re-exec/restart | Smaller entrypoint change but no independent observer remains to verify startup or select the old runtime; must still solve custom listener/thread drain. Reject for v1. |
| Uvicorn reload / file watcher | Ties restart to filesystem changes rather than completed gated landing; the custom two-listener lifecycle is outside Uvicorn's lifespan (`hub/app.py:25-26`). Reject as the safety authority. |

## Proposed architecture

`Start-DroverHub.ps1` → installed stdlib supervisor → exactly one hub child → detached runners. The scheduled task invokes its installed frozen copy of the same wrapper. Parent and child share an owner-only state directory and protocol version 1. The supervisor never opens the ledger or imports mutable hub application code.

The installer copies `hub/supervisor.py`, `hub/runtime.py`, `hub/reload_protocol.py`, and `hub/reload_machine.py` to `<state>/hub-runtime/bootstrap/v1/`. They must run as standalone stdlib modules with no imports from `drover` or application dependencies. Copy is an explicit owner installation action, never an automatic reload action. Parent holds `supervisor.lock`; child holds the existing `dispatcher.lock`. Neither lock substitutes for the other. A second start reports the existing supervisor status; it never spawns another child. Bootstrap changes require a stopped, owner-managed upgrade. Also freeze a copy of `Start-DroverHub.ps1` there for scheduled-task invocation; enrollment compatibility covers that wrapper and both installer scripts as well as the four Python peers. Source wrapper launches the installed parent using the enrollment paths; installed wrapper resolves source from enrollment, not its own parent directory.

At enrollment, export a clean source HEAD with `git archive --format=zip <40-hex-sha>` to a temporary directory; validate entries, then atomically rename to `<state>/hub-runtime/releases/<sha>/`. Archive all tracked files, no `.git`, untracked files, traversal, links, or private state. Store a content manifest. Never rewrite an existing release. Keep all v1 releases: detached runners can still import their launch generation. No worktree is created or pruned.

Run the existing installed `hub/.venv/Scripts/python.exe` directly with `-m hub`, CWD and sole project import root set to the release. Strip inherited project `PYTHONPATH` entries before setting it. Do not run `uv sync`, resolve dependencies, or install on reload. The existing installation creates the venv (`hub/scripts/Install-Drover.ps1:88-97`); dependencies are declared in `hub/pyproject.toml:5-16`.

Pin absolute ledger, runs, repo-config, config-home, token, source-root and overlay paths in the enrollment manifest. Set `DROVER_OVERLAY` to the original source overlay and preserve the owner environment. This matters because `drover/config.py:25,465-472` otherwise derives the overlay from the imported checkout and `drover/launch.py:383,419-428` derives runner code from its own package. Repo configs continue pointing to actual repositories; snapshots are never a landing destination. Both Windows and WSL runner imports come from the retained release. Require the release path to be reachable through the existing WSL drive mapping before enabling reload with WSL runs.

## Detection and eligibility

1. Enroll one explicit source checkout/default branch and its canonical Git common-directory identity. Runtime/source identity comes from the enrollment manifest, not the task's repo name or snapshot `__file__`.
2. After `LandingService._land_repo` returns `outcome=landed` and its `finally` finishes, inspect `batch_id`, `tip`, enrolled source HEAD and repo identity. Use the persisted batch as evidence. Require source HEAD exactly equals that tip and tip is a descendant of the running SHA. Different repositories, failed/stale/remote-only landings do not request reload.
3. Compare `git diff --name-only --no-renames <running_sha> <tip> -- hub/ drover/`. Include additions, deletions and rename source/destination by disabling rename detection. Any such change is relevant; docs-only changes are not. A relevant landing requests even when auto is off, so the UI shows pending work. Bootstrap-only edits request an owner upgrade instead of restart.
4. Reconcile successful recorded batches at startup and every 30 seconds to cover a crash between landing and notification (`drover/landing.py:547-556` is the existing batch receipt). Never execute an unrecorded source HEAD automatically. Coalesce eligible tips to the newest descendant **before** drain; pin one SHA once draining starts. A later tip waits for the next cycle.
5. Require clean source checkout, exact 40-hex SHA, valid immutable release, protocol/run-format version 1, identical `hub/uv.lock` and `hub/pyproject.toml`, identical `drover/ledger/schema.py` content, unchanged configured interpreter identity, and actual ledger schema version equal to the enrolled version. V1 deliberately treats any schema-file change conservatively, even comments. Stage failures leave the old child running with a pending reason.
6. Disallow automatic candidates that change any bootstrap source file. Do not dynamically load candidate code to inspect compatibility: inspect Git blobs/manifests with the stable parent. Protocol/run-format changes must bump constants and require owner enrollment. Unknown compatibility fails closed.

“Reload now” may restart the current enrolled SHA or use the newest eligible recorded tip; it cannot accept an arbitrary ref/path/command. Auto defaults **off** on enrollment. A repeated request uses the same durable idempotency key and is coalesced. Turning auto off cancels an automatic request before drain, but does not interrupt a drain already underway. The owner cannot force through blockers.

## Drain, handoff and readiness

The proposed `ActivityBarrier` is shared across hub dispatcher, landing, services and API admission. Its mutex atomically changes admission to closed and accounts for synchronous entry sections. Every claim, worker/review/gate launch, landing batch and service mutation enters before beginning. A caller holding a section may finish; once closed no new caller enters. A single “paused” boolean is insufficient. Control/preference requests use separately counted control sections that may enter while work admission is closed; sealing the barrier at `stopping` closes those too, and shutdown waits for their count to reach zero. Long detached worker/review lifetimes are not sections; the claim/launch handshake is.

Drain proceeds as follows:

1. Persist `draining` with generation/request/target; close barrier. Keep `dispatch_enabled` unchanged. Let a tick already inside admission finish its claim/launch handshake. Reap, adopt and watch existing runs; launch no workers, reviews, gates, repairs or landing batches. Preserve completed worker results awaiting a future gate.
2. Wait for the **entire** existing landing call (including queued lease, batch gate, retries, cleanup, receipt and notification) to exit. A landing continuation may finish inside its already-held section. Wait for every open ledger `role=gate` run and launcher cancellation handshake to reach a recorded terminal result, plus an idle dispatcher tick. Gate completion may update ledger outcomes; starting the next platform gate remains fenced.
3. Stop admission to garden/tuner/discovery mutations; finish the current operation and park each service at an acknowledged boundary. Garden may park between rows and resume using its existing receipt mechanism. Watchdog remains alive through drain; a new live-WSL-run demand callback keeps its keepalive active even after the old `_wsl_needed` note expires. Drain also waits for active API admissions and mutation requests to finish; then reject new mutations with 503 and `Retry-After: 5`, except reload status/control and dispatch preference updates. These permitted updates finish before shutdown and never bypass the work barrier.
4. Require completed recovery, known process scan, no unresolved launches, no active Windows runner with `detached_in_job=true` or missing detachment evidence, no busy sections, no open gates, no live landing/service operation, and fresh participant acknowledgments for this generation. Do not infer safety from missing process/status data. Healthy Windows/WSL workers and reviews remain running; their run IDs and files are untouched.
5. Drain deadline is 900 seconds. On expiry, persist `blocked` and reasons, keep old hub serving/reaping, keep admission closed, and wait for an owner retry or cancel. Cancel reopens admission according to the **current** owner dispatch preference. No automatic timeout kill, lease deletion, synthetic receipt or task requeue.
6. Before shutdown, parent starts its own bounded WSL keepalive when WSL runs are present; it never kills the old watchdog's process or shuts down WSL. Persist `stopping`, reject further mutations including preference/control changes with 503, wait for admitted controls to finish, send the generation-bound stop command, stop both listeners/adapter loop, join every background thread, close ledger connections, then exit **75**. Thread owners close their connections in their own `finally`; release dispatcher lock only after both dispatch and landing have stopped. No child-tree termination. Shutdown deadline 30 seconds; overrun blocks, leaving the existing PID alone.
7. Parent requires observed old PID exit and actual dispatcher-lock availability before spawning a replacement. It never starts two hub children or steals a lock. Candidate starts in `validating` with admission closed before app construction. Unknown supervisor protocol/identity refuses startup before opening the ledger.
8. Candidate acquires dispatcher lock, opens the same compatible ledger, starts recovery-only dispatch, and binds the owner UI. New `GET /api/v1/hub/ready` returns 503 until ready, then 200 with protocol, runtime SHA, generation, instance UUID, fresh heartbeat counter (coordinator advances it every second after an acknowledged recovery tick, with dispatcher thread alive and last acknowledged tick no older than 45 seconds), recovery completion and dispatcher-lock ownership. Readiness uses a fresh in-memory heartbeat from this instance, not only persisted `_heartbeat`. This minimal readiness route accepts only a socket peer on loopback arriving at the owner UI port; it exposes no control or secrets and requires no bearer token. Parent checks loopback UI port against expected fields, PID liveness and 15 continuous seconds of advancing heartbeat, within 60 seconds total. Missing WSL adapter for an optional listener is degraded, not fatal; unknown scans for actual open WSL runs block recovery and readiness.
9. Parent atomically commits the healthy active pointer and writes `committed`. Child opens admission only after matching that pointer/command. Resume services and dispatch according to the latest saved owner preference. WSL keepalive overlaps the new watchdog, then parent stops only the exact keepalive child it created. Reconnect browsers resync the page when instance UUID changes; do not rely on pre-restart SSE IDs.

## Failure and rollback

During validation there are no new gates, landings or service mutations. Adoption/reaping may persist legitimate existing run outcomes; keep those writes. If candidate import, bind, lock acquisition, recovery or heartbeat fails, close it cooperatively. A hung **validation-only** child may be terminated by its verified PID after 10 seconds because it has never received commit permission; never terminate its process tree. If old/new identity or validation-only status cannot be proved, block instead. Recheck PID exit and dispatcher lock before rollback.

Select the previous healthy immutable SHA and start it once with a fresh generation, using the same 60-second/15-second readiness rules. This is automatic **runtime rollback**: main stays at the landed SHA, task statuses stay landed, and ledger/run files are never restored. Once fallback is healthy, persist `rolled_back`, disable auto and keep new-work admission closed while the fallback adopts/reaps. Owner retry selects a subsequent eligible repair tip, or cancel explicitly resumes the fallback. Quarantine the failed SHA until a new tip or explicit owner retry. If fallback also fails, persist `failed`, exit nonzero, and require owner repair; do not alternate SHAs forever.

Supervisor unexpected child exit: do not turn a crash into an approved drain. Restart the last committed runtime in recovery-only mode, adopt first, and keep admission closed until readiness succeeds. Startup crash budget: two attempts in ten minutes including one fallback; exhaustion latches `failed`. Task Scheduler relaunches must honor that durable latch. A supervisor restart acquires its lock, reconciles child PID **and creation identity**, generation and a live challenge-response; reattach a matching child rather than spawning. Unknown or unresponsive existing child/lock means blocked. Never signal an unrelated PID or trust a stale PID file.

Schema/dependency/protocol changes, changed interpreter, dirty source, insufficient disk or corrupted manifest stay pending for an owner-managed upgrade. No ledger backup restore is part of rollback; forward migrations make that unsafe for adopted live runs. First supervised installation establishes a rollback-capable baseline; it cannot recover to pre-feature code lacking the protocol.

## Proposed schema and full worked example

No new ledger tables or migration. `hub_auto_reload` is an owner-only string setting (`"false"` default), projected from the parent-authoritative `auto_enabled` state. A dedicated toggle enqueues `action=set_auto` with `enabled:boolean`; the parent journals the change and the child mirrors the setting. Fallback mirrors it to false. Generic settings writes to reload-owned keys are rejected even for the owner; only the dedicated controller changes them. Reserve `hub.reload.` events and all `hub_reload_`/`hub_auto_reload` setting writes against bearer-token clients. Use dedicated owner routes for reload control; generic settings must not masquerade as reload requests. Existing owner checks are at `hub/api.py:161-172,279-285,352-362` and UI/CSRF middleware at `hub/app.py:59-69`.

Files under `<state>/hub-runtime/` use protocol `1`, UTF-8 JSON, atomic temp/write/fsync/replace, owner-only ACL inherited from the state root (existing installer ACL at `hub/scripts/Install-DroverHub.ps1:20-24`). Writers are explicit:

| Record | Writer and contract |
| --- | --- |
| `enrollment.json` | Installer only: protocol, source/common-dir/default-branch identity, absolute pinned paths, interpreter identity, enrolled schema version and bootstrap digest. |
| `releases/<sha>/manifest.json` | Parent after complete export: SHA, tree/content digest, protocol/run-format=1, dependency/schema/bootstrap blob digests. Reuse verifies digest. |
| `requests/<uuid>.json` | Child or wrapper, create-once: `{protocol,id,key,action,source,from_sha,target_sha,batch_id,auto,enabled,created_at}`. `action=request\|cancel\|retry\|stop\|set_auto`; `enabled` is boolean for `set_auto`, null otherwise; `source=landing\|owner\|reconcile`. |
| `command.json` | Parent only: `{protocol,generation,instance,request_id,action,target_sha}`, with `action=drain\|stop\|commit\|cancel\|probe`; an extra `challenge` UUID is required for probe and null otherwise. Child echoes the probe challenge in child status on the next heartbeat. Parent reattachment requires that fresh echo plus OS creation identity. Child ignores stale generation/instance. |
| `child.json` | Child only: `{protocol,generation,instance,pid,creation_identity,runtime_sha,phase,heartbeat_seq,challenge,recovery_complete,dispatcher_lock,barrier,blockers}`; `barrier` contains active sections/open gate IDs/landing and service acknowledgments, unresolved launches, WSL live-run IDs and participant generations. Supervisor verifies live handshake, never file existence alone. |
| `state.json` | Parent only: schema below; sole authority for active pointer, crash budget and phase. No secrets. |
| `journal/<sequence>.json` | Parent, create-once before matching state transition: `{protocol,seq,id,at,kind,data}`. State points to last applied sequence; restart replays any later complete records. Child mirrors into ledger events using `id` deduplication in a transaction, and emits SSE only as a hint. Parent does not access SQLite. |

State enums: `idle → pending → draining → stopping → validating → committed`; exceptional `blocked`, `rolling_back → rolled_back`, `failed`. Pending can cancel to idle; blocked can retry drain or cancel; rolled_back can retry replacement or cancel to resume fallback; failed needs owner repair/reset. `active` means last committed healthy release; `child` may be a validating different release. Unknown version, invalid SHA, missing required field or damaged journal blocks without modifying the active pointer. Times are UTC RFC3339; elapsed deadlines use a monotonic clock. Sequence and generation are increasing integers; SHA values are full lowercase 40-hex; nullable fields are explicit.

Full `state.json` example during replacement (synthetic identities and digests):

```json
{
  "protocol": 1,
  "generation": 12,
  "phase": "validating",
  "request": {
    "id": "00000000-0000-4000-8000-000000000042",
    "key": "landing:drover:42:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "action": "request",
    "source": "landing",
    "from_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "target_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "batch_id": 42,
    "auto": true,
    "enabled": null,
    "created_at": "2026-10-17T18:00:00Z"
  },
  "active": {"sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "instance": "10000000-0000-4000-8000-000000000001", "healthy_at": "2026-10-17T17:00:00Z"},
  "previous": null,
  "child": {"sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "instance": "10000000-0000-4000-8000-000000000002", "pid": 7200, "creation_identity": "spawn-12"},
  "auto_enabled": true,
  "owner_dispatch_enabled": true,
  "blockers": [],
  "drained": {"generation": 12, "open_gate_ids": [], "active_sections": 0, "landing_idle": true, "services_idle": true, "recovery_complete": true, "scan_known": true, "unsafe_detached_ids": [], "unresolved_launches": [], "wsl_live_run_ids": [612], "participant_generations": {"dispatcher": 12, "landing": 12, "services": 12, "api": 12}},
  "attempts": {"window_started": "2026-10-17T18:00:00Z", "count": 1, "fallback_used": false},
  "quarantined_sha": null,
  "updated_at": "2026-10-17T18:00:06Z",
  "last_journal_seq": 104
}
```

Worked cycle: runtime A is healthy and a recorded batch 42 lands B, changing `hub/dispatch.py`. Auto is on. Request key above is accepted once. Journal 101 is `hub.reload.requested`; drain sees gate run 611 and reports it as a blocker. Worker 610 and WSL review 612 keep running. Gate 611 finishes normally, its receipt is ingested, landing returns, services park, and journal 102 records `hub.reload.drained`. Parent starts its WSL bridge, old child exits 75, journal 103 records `hub.reload.starting`; the example is journal 104 `hub.reload.validating`. After 15 stable seconds, journal 105 is `hub.reload.succeeded`, `previous=A`, `active=B`, `child=B`, phase `committed`, and B adopts 610/612 without launching replacements.

Failure branch from the same example: B raises an import error; journal 105 is `hub.reload.start_failed` with bounded error text; generation 13 starts A, records `hub.reload.rolling_back`, then `hub.reload.rolled_back`. `active=A`, `previous=null`, `child=A`, `auto_enabled=false`, `quarantined_sha=B`, `attempts.count=2`, `fallback_used=true`; admission remains closed and main remains B. If A fails too, `hub.reload.failed` latches the stop. Other required events: `pending`, `blocked`, `cancelled`, `auto_changed`, `supervisor_recovered`; each carries request ID, generation, from/target/active SHA, phase, reason and actor, without tokens or environment dumps. The next healthy child mirrors the whole journal, including events written while no hub was reachable.

## Owner experience and migration

Health gains a Reload card: running SHA, latest landed candidate SHA, auto toggle, pending/draining/validating/fallback status, blocker reasons, last error/time, and “Reload now”, “Retry” and “Cancel drain” when applicable. Reload means “when safe”; it has no force option. Dedicated owner-authenticated `GET /api/v1/hub/reload`, `POST /api/v1/hub/reload` (`action=request|retry|cancel`), and `PUT /api/v1/hub/reload/auto` (`enabled:boolean`) return status/202 with idempotency key. UI forms use the same controller and existing CSRF middleware. A direct unsupervised hub reports reload unavailable and the supported command, with controls disabled.

Supported owner command from the repository root: `./hub/scripts/Start-DroverHub.ps1`. It resolves its source path independently of CWD, checks enrollment/venv, and starts or reports the supervisor. `-Status` reads durable status even when HTTP is down; `-Stop` cooperatively drains and stops without restart. Arguments and paths are passed as argument arrays, never interpolated command text. No elevation, policy bypass, uv/PYTHONPATH incantation, dependency installation, broad taskkill, or process-name matching. If prerequisites are absent it prints the existing owner installation command and exits nonzero. The scheduled task is optional, installed explicitly by the owner; neither wrapper nor auto-reload registers or starts system services implicitly.

Migration: land the complete gated feature; owner disables automatic activity and waits for existing gates/landing to finish, stops the legacy hub gracefully, runs the existing installer to enroll that clean SHA and retarget the scheduled task, then uses the supported command. Installer must refuse a running legacy/unknown dispatcher instead of stopping it. First supervised start validates the baseline, adopts existing safe detached runs, and keeps the existing owner dispatch preference. Auto remains off until the owner enables it after a successful manual reload. Update README and installation instructions to the single start command. No data migration, worktree cleanup, main reset or ledger restore.

## Validation and risks

Test with injectable Git/process/HTTP/clock/filesystem adapters and temporary local ledgers. Use barriers/events rather than timing sleeps for races. Cover renamed/deleted runtime paths, other repo and remote-only landings, missed notifications, coalescing, dirty checkout, schema/dependency/bootstrap refusal, torn journal replay, stale generation/PID, second supervisor, lock contention, paused owner preference changes, launch/drain race, gate between platforms, busy batch beyond lock TTL, garden row resume, unknown scan and in-job runners, optional API rebind, candidate import/bind/hang failures, rollback failure/latch, worker/review completion during outage, and browser instance resync. A Windows owner-run acceptance scenario must leave one Windows and one WSL detached run alive across success and fallback, with no duplicate launches or run-ID changes. Tests must not install dependencies, touch real state or start a service.

Main risks: a candidate with unchanged migrations can still contain a data-corruption bug (startup rollback is not transactional application rollback); job inheritance can prevent safe restart; unavailable WSL scans delay readiness; retaining releases consumes disk; a corrupt stable bootstrap cannot repair itself. Require at least twice the archive's expanded size plus 256 MiB free before staging, with a 128 MiB expanded archive limit; low disk stays pending. Owner manages release cleanup only after proving no live run references it. T-8 may later classify infrastructure failures; this feature emits explicit reasons without depending on that work. T-5 is related, not a prerequisite.

## Owner decisions

Recommendations are used to finish this non-interactive research task; they are proposals, not owner approvals to execute installation or implementation.

| Question | Recommendation used |
| --- | --- |
| Parent loop, in-process replacement or Uvicorn reload? | Stable stdlib parent; existing per-user scheduled task may launch it. |
| Must the hub keep executing in main? | Keep main as the landing/source checkout; execute immutable exports so rollback preserves Git history and workers' import roots. |
| What counts as self-change? | Explicit enrolled repo identity, successful recorded landing, exact local source HEAD, and a diff touching `hub/` or `drover/`. |
| Should auto be on immediately? | Off by default; enable after a supervised manual reload. “Reload now” always observes the same barriers. |
| How long should drain/startup wait? | Drain 900s, shutdown 30s, readiness 60s including 15 stable seconds; block instead of forcing active work. |
| May healthy workers/reviews survive, and what blocks? | Preserve safe detached workers/reviews; wait for gates, complete landing and service/API boundaries; unknown scans/detachment block. |
| What does rollback undo? | Runtime selection only, one fallback attempt; no reset/revert, no task-state undo, no ledger restore. Keep fallback admission closed until owner action. |
| Can auto install dependencies or migrate data? | No. Unchanged schema file, dependency files, interpreter and protocol required; other changes need owner upgrade. |
| Can independent owner CLI gates overlap reload? | No in v1; turn auto off and finish them before manual reload. Cross-process registration is deferred. |
| Should a watchdog/launcher failure trigger endless retries? | Persist a two-attempt/ten-minute budget and failed latch across scheduled-task relaunches. |
| How does the owner start and diagnose it? | One PowerShell wrapper with `-Status` and cooperative `-Stop`, working without HTTP. |
| How should bootstrap updates and snapshot retention work? | Explicit stopped owner upgrade for bootstrap; retain all v1 snapshots until owner-reviewed cleanup. |
| Which worktree name applies to this research artifact? | Use the externally assigned task worktree/branch. Requested T-10 naming needs the orchestrator; do not create/switch shared worktrees from this worker. |
