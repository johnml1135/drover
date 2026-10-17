# Safe Self-Reload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans per assigned Drover task. Drover schedules the dependency graph; each worker implements only its own file scope. Steps use checkbox (`- [ ]`) syntax. No implementation is authorized by this research deliverable.

**Goal:** Safely replace the Windows hub after a relevant self-landing while detached workers continue, with verified startup and one automatic runtime fallback.

**Architecture:** An installed stdlib supervisor selects immutable Git exports and owns one hub child. A shared activity barrier drains hub-managed landing, gates, launch handshakes and service/API mutations; candidate admission remains closed through readiness and parent commit. Runtime fallback preserves main and the live ledger.

**Tech Stack:** Existing Python 3.13+, stdlib, FastAPI/Uvicorn/Jinja2, SQLite, Git and PowerShell; existing installed hub environment only.

**Spec:** [2026-10-17-self-reload-design.md](../specs/2026-10-17-self-reload-design.md)

## Global Constraints

- No new packages or services.
- Auto defaults **off** on enrollment.
- No new ledger tables or migration.
- No child-tree termination.
- Safety covers **hub-managed** gates and landings.
- First supervised installation establishes a rollback-capable baseline; it cannot recover to pre-feature code lacking the protocol.
- Drain deadline is 900 seconds. Shutdown deadline 30 seconds. Readiness uses 60 seconds total including 15 continuous stable seconds; validation-only shutdown grace is 10 seconds.
- Protocol/run format are 1; identical schema/dependency/bootstrap blobs and interpreter identity are required. Owner upgrades handle changes to those contracts.
- Supervisor and child have separate locks. Unknown identity, scan, detachment or compatibility blocks instead of forcing.
- Fallback selects a retained healthy runtime once, disables auto and holds admission; main, landed task history and ledger are preserved.
- Frozen bootstrap peers are supervisor.py, runtime.py, reload_protocol.py **and reload_machine.py**; all use only stdlib; select package imports vs standalone sibling imports by __package__. Freeze the start wrapper too; its installed mode reads source paths from enrollment. Compatibility covers both installer scripts.
- No personal paths, tokens or environment dumps in docs or events. Workers never operate the live scheduled task/service or owner processes.
- Windows dotnet/MSBuild and Windows-only verification go to an authorized Windows worker. Do not install missing test dependencies.

## Review Focus

1. Unknown WSL scan or Windows in-job runner must block, rather than look idle: task #4 tests recovery/detachment blockers; #10 verifies survival on Windows.
2. Crash between durable journal append and active-pointer replace must replay once: #0 tests torn state; #3 tests interrupted effects.
3. Standalone API claim during validation must get 503 even if dispatch_enabled is true: #8 tests the mutation middleware.
4. Owner checkout or interpreter changed after staging must be rechecked before drain/spawn, preserving source data: #1 tests mismatch, #3 tests the final eligibility recheck.
5. SSE IDs restart at one; reconnection to a changed instance must resync instead of showing stale status: #7 tests UUID-based reconnect behavior.

---

## Execution and file ownership

The JSON sibling is the importable source of task goals, acceptance, exact file scopes and 0-based dependencies. Each goal repeats the Type/Repo/Write access/Read access/Branch-worktree/Connected tasks header and the operational constraints because a worker sees only its assigned task. Index references `#N` refer to this list, not real task IDs. **Import preparation must resolve each index to the earlier created task ID**: today's importer checks deps against existing IDs (`hub/store.py:333-337`). Do not post these unresolved index strings directly to the current hub API.

Current behavior is grounded in the spec's main-SHA evidence table. Integration anchors: main `hub/dispatch.py:146-185,235-278` (landing/thread lock), `drover/dispatcher.py:987-1066,1195-1243` (adoption/admission), `hub/app.py:25-53` and `hub/__main__.py:43-49,76-125` (custom lifecycle), `hub/api.py:186-188,224-228` (liveness/claim), `hub/scripts/Install-DroverHub.ps1:13-25` (scheduled launcher). T-7's uncommitted fields are recorded in the spec and are not prerequisites; do not assume migration version 4 has landed.

All production files listed below are **proposed** ownership; no two tasks share a file or directory prefix. New focused tests use existing unittest conventions (`tests/test_hub_dispatch.py:5-17,26-35`). No dependency or schema file is modified. Tasks #1, #2, #4 and #5 may follow #0 independently; #3 follows #1/#2; #6 follows #1/#4/#5; #7 follows #6; #8 joins supervisor, drain and routes; #9 adds enrollment; #10 tests the assembled system. Admission capacity remains Drover's decision; this plan does not authorize extra parallel builds.

## Shared record and interface rules

Read the design's schema/example for record fields and legal phases. New constructor parameters are optional to preserve existing tests/callers. Snapshots are plain dicts with the exact keys below, not new ledger models. `ActivityBarrier` owns synchronous admission counts; dispatcher owns gate/run proof; services own their boundary acknowledgments; coordinator aggregates these into `child.json`; parent alone owns `state.json` and journal.

`child.json.barrier` must include `active_sections`, `open_gate_ids`, `landing_idle`, `services_idle`, `recovery_complete`, `scan_known`, `unsafe_detached_ids`, `unresolved_launches`, and `wsl_live_run_ids`, plus participant generation acknowledgments: dispatcher ack_generation, landing landing_ack_generation, services ack_generation, and API barrier generation only when its admitted sections are idle. These are updated after the participant has observed the closed barrier, never inferred from an old heartbeat. Parent starts and retains the WSL bridge until the new watchdog acknowledges live-run demand. Enrollment includes the installed reducer in the bootstrap digest; any of its source changes requires stopped owner re-enrollment.

The ready payload is `{protocol:1, runtime_sha:str, generation:int, instance:str, heartbeat_seq:int, recovery_complete:bool, dispatcher_lock:bool}`; 200 means recovery finished and UI bound for that instance. Parent still checks 15 seconds of advancing sequence before commit. `active` remains the last healthy runtime until durable commit. Parent never reads SQLite; child reports schema version/heartbeat/run proof. All clock-dependent tests use a fake monotonic clock.

Test commands below use `python` to mean the **already installed** test interpreter with this checkout as CWD/import root. Before running, confirm its identity and required dependencies; never substitute an install. On a configured owner Windows gate the existing full command is `uv run --no-sync --project hub python -m unittest discover -s tests -t .`; use `--no-sync` to avoid dependency resolution. In WSL, temporary ledgers must live on the native temporary filesystem (`drover/ledger/db.py:26-29` rejects DrvFs WAL).

### Task #0: Define the reload protocol, activity barrier and crash-safe journal

**Size/risk:** M/low. **Dependencies:** none.

**Files:**

- Create: `hub/reload_protocol.py`
- Create: `tests/test_reload_protocol.py`

**Interfaces:** Produce PROTOCOL=1, RUN_FORMAT=1; validate_record(kind: str, value: dict) -> dict; atomic_json(path: Path, value: dict) -> None; Journal(root: Path).append(kind: str, data: dict) -> dict and .replay(after: int) -> list[dict]. Produce ActivityBarrier with try_enter(kind: str, *, control: bool=False) -> context-manager-or-None, close(generation: int) -> None, seal(generation: int) -> None, reopen(generation: int) -> None, snapshot() -> dict. A returned section counts immediately, before __enter__; __exit__ always releases. Snapshot has generation, closed, sealed, active_sections and counts by kind. close denies work; counted control=True sections may enter until seal denies all admission. Matching reopen unseals. Admission close and section acquisition share one mutex. The module runs standalone with only stdlib imports.

- [ ] **Step 1: Pin the failing behavior in `tests/test_reload_protocol.py`.** Use a minimal temporary fixture with injected adapters; the assertion sketch below fixes the required behavior (fixture helpers are local to this task's test file). Add the other listed acceptance cases from the JSON as focused tests.

```python
def test_stale_generation_cannot_reopen(self):
    barrier = ActivityBarrier()
    barrier.close(12)
    barrier.reopen(11)
    self.assertTrue(barrier.snapshot()['closed'])
    self.assertIsNone(barrier.try_enter('claim'))
```

- [ ] **Step 2: Run `python -m unittest tests.test_reload_protocol -v`.** Expected initially: failure for missing interface/behavior, with no real machine mutation. Record the observed failure; do not accept a dependency-related skip as red.
- [ ] **Step 3: Implement the interfaces in this task's owned files.** Implement the protocol records and enums from the design, strict full lowercase 40-hex SHAs and increasing generations/sequences. Journal append uses one immutable sequence file per event before replacing state; recover a complete journal record whose state update was interrupted. Exclude partial files, detect gaps/corruption, and block unknown versions. Child and parent writers never overwrite each other's records. Atomic writes flush and fsync before replace. An installer-supplied state root carries owner ACLs; never log secrets. A stale generation cannot reopen a current barrier. This foundational module must import without FastAPI or the drover package.
- [ ] **Step 4: Run `python -m unittest tests.test_reload_protocol -v` again.** Expected: all focused cases pass with no skipped platform-independent tests. Check existing callers retain compatible defaults. For platform-specific cases record the platform and actual result separately.
- [ ] **Step 5: Review scope and hand off.** Run `git diff --check` and inspect changed paths against this task's JSON scope. Codex reports ready_to_commit and a one-sentence commit message without staging/committing; Drover makes the commit and gates the exact SHA. Do not call the task READY without the exact-SHA passing receipt.

### Task #1: Export and validate immutable hub releases

**Size/risk:** M/low. **Dependencies:** #0.

**Files:**

- Create: `hub/runtime.py`
- Create: `tests/test_hub_runtime.py`

**Interfaces:** Consume reload_protocol validators/atomic_json. Produce prepare_release(source: Path, sha: str, root: Path, enrolled: dict) -> dict (manifest including release_path); check_eligibility(enrolled: dict, running_sha: str, target_sha: str, ledger_schema: int) -> dict with eligible:bool and reasons:list[str]; child_command(enrolled: dict, release: dict, generation: int, instance: str) -> tuple[list[str], dict[str,str], str] returning argv, env, cwd. Inject Git, disk and interpreter probes via keyword adapters for tests. No drover/application imports.

- [ ] **Step 1: Pin the failing behavior in `tests/test_hub_runtime.py`.** Use a minimal temporary fixture with injected adapters; the assertion sketch below fixes the required behavior (fixture helpers are local to this task's test file). Add the other listed acceptance cases from the JSON as focused tests.

```python
def test_schema_change_is_pending(self):
    result = check_eligibility(enrolled, sha_a, sha_b, ledger_schema=3)
    self.assertFalse(result['eligible'])
    self.assertIn('schema-change', result['reasons'])
```

- [ ] **Step 2: Run `python -m unittest tests.test_hub_runtime -v`.** Expected initially: failure for missing interface/behavior, with no real machine mutation. Record the observed failure; do not accept a dependency-related skip as red.
- [ ] **Step 3: Implement the interfaces in this task's owned files.** Export exact Git objects with git archive to a temporary release, validate ZIP paths/types/expanded bytes, hash contents and atomically rename. Reuse only verified identical releases; retain every release. Compare canonical Git common-dir/default-branch identity, source cleanliness, exact HEAD/target ancestry, schema-file blob, hub dependency files, protocol/run format, interpreter identity and bootstrap blobs (four Python peers, Start-DroverHub.ps1 and both installer scripts). Refuse bootstrap, schema or dependency changes for auto and manual reload. Runtime does not query SQLite: ledger_schema is supplied by the child. Enrolled absolute state/config/overlay/repo/token paths stay fixed; launcher imports only from release, uses the existing Windows venv Python directly, and removes inherited project PYTHONPATH. Frozen files must support both package imports in the child and standalone sibling imports in bootstrap, selected by __package__. Require WSL reachability when reported WSL runs need it. Require free bytes >= 2 * expanded archive bytes + 256 MiB and expanded size <= 128 MiB. No Git ref mutation, worktree, install or release deletion.
- [ ] **Step 4: Run `python -m unittest tests.test_hub_runtime -v` again.** Expected: all focused cases pass with no skipped platform-independent tests. Check existing callers retain compatible defaults. For platform-specific cases record the platform and actual result separately.
- [ ] **Step 5: Review scope and hand off.** Run `git diff --check` and inspect changed paths against this task's JSON scope. Codex reports ready_to_commit and a one-sentence commit message without staging/committing; Drover makes the commit and gates the exact SHA. Do not call the task READY without the exact-SHA passing receipt.

### Task #2: Model reload transitions and the persistent retry budget

**Size/risk:** S/low. **Dependencies:** #0.

**Files:**

- Create: `hub/reload_machine.py`
- Create: `tests/test_reload_machine.py`

**Interfaces:** Consume protocol records. Produce transition(state: dict, event: dict, now: float) -> tuple[dict, list[dict]]: a pure reducer returning next state and ordered effect descriptions. Effects use {kind,data}; kinds are stage, command, spawn, probe, terminate_validation_pid, bridge_start, bridge_stop, latch, notify. Stable supervisor owns execution; no process, filesystem or SQLite access in reducer.

- [ ] **Step 1: Pin the failing behavior in `tests/test_reload_machine.py`.** Use a minimal temporary fixture with injected adapters; the assertion sketch below fixes the required behavior (fixture helpers are local to this task's test file). Add the other listed acceptance cases from the JSON as focused tests.

```python
def test_failed_fallback_latches(self):
    state, effects = transition(rolling_back_state, {'kind':'start_failed','generation':13}, now=90)
    self.assertEqual(state['phase'], 'failed')
    self.assertFalse(any(e['kind'] == 'spawn' for e in effects))
```

- [ ] **Step 2: Run `python -m unittest tests.test_reload_machine -v`.** Expected initially: failure for missing interface/behavior, with no real machine mutation. Record the observed failure; do not accept a dependency-related skip as red.
- [ ] **Step 3: Implement the interfaces in this task's owned files.** Define idle/pending/draining/stopping/validating/committed and blocked/rolling_back/rolled_back/failed transitions exactly as the design. Coalesce newest descendant before draining only; pin target thereafter. Drain timeout 900s blocks without kill, shutdown timeout 30s blocks, readiness timeout 60s includes 15 stable advancing-heartbeat seconds, failed validation shutdown grace 10s. One fallback only; two attempts per ten minutes including fallback, persisted failed latch survives scheduled-task relaunch. Only healthy candidate commit changes active/previous. Fallback disables auto and keeps admission closed; cancel then explicitly resumes it. Distinguish intentional exit 75, owner stop, unexpected exit and identity uncertainty. Stale events cannot change state.
- [ ] **Step 4: Run `python -m unittest tests.test_reload_machine -v` again.** Expected: all focused cases pass with no skipped platform-independent tests. Check existing callers retain compatible defaults. For platform-specific cases record the platform and actual result separately.
- [ ] **Step 5: Review scope and hand off.** Run `git diff --check` and inspect changed paths against this task's JSON scope. Codex reports ready_to_commit and a one-sentence commit message without staging/committing; Drover makes the commit and gates the exact SHA. Do not call the task READY without the exact-SHA passing receipt.

### Task #3: Supervise one hub child and perform verified startup rollback

**Size/risk:** M/high. **Dependencies:** #0, #1, #2.

**Files:**

- Create: `hub/supervisor.py`
- Create: `tests/test_hub_supervisor.py`

**Interfaces:** Consume runtime.prepare_release/check_eligibility/child_command and protocol Journal/atomic_json. Consume reload_machine.transition as a frozen bootstrap peer (installer must copy reload_machine.py too). Produce Supervisor(enrollment: dict, adapters: object).step() -> dict and main(argv: list[str]|None=None) -> int. Adapters expose spawn(argv,env,cwd), identity(pid), poll(pid), probe(url), terminate(pid), git(...), disk(...), clock(), sleep(seconds), bridge_start(distro,seconds), bridge_stop(handle), lock(path). Native Windows lock is implemented locally or in reload_protocol, without importing drover. The installed parent uses only stdlib/bootstrap peers.

- [ ] **Step 1: Pin the failing behavior in `tests/test_hub_supervisor.py`.** Use a minimal temporary fixture with injected adapters; the assertion sketch below fixes the required behavior (fixture helpers are local to this task's test file). Add the other listed acceptance cases from the JSON as focused tests.

```python
def test_old_child_alive_prevents_spawn(self):
    supervisor.step()
    self.assertEqual(fake_processes.spawned, [])
    self.assertEqual(fake_processes.terminated, [])
    self.assertEqual(supervisor.state['phase'], 'blocked')
```

- [ ] **Step 2: Run `python -m unittest tests.test_hub_supervisor -v`.** Expected initially: failure for missing interface/behavior, with no real machine mutation. Record the observed failure; do not accept a dependency-related skip as red.
- [ ] **Step 3: Implement the interfaces in this task's owned files.** Execute durable reducer effects with idempotent journal/state writes and a supervisor.lock. Resolve request/control records by generation/instance and challenge-response (command action=probe with fresh challenge UUID echoed in child status on its next heartbeat), never PID file alone. Wait for observed old PID exit plus dispatcher-lock availability before replacement or fallback; recheck source HEAD/cleanliness, compatibility and interpreter identity immediately before drain and spawn; never steal locks or create a second hub. Probe loopback /api/v1/hub/ready for exact protocol/SHA/generation/instance, recovery=true, lock=true, and advancing heartbeat for 15 continuous seconds within 60s. Failures close validation-only child cooperatively, then after 10s may terminate its verified PID, never its tree; uncertainty blocks. On a parent restart reattach matching live child; unknown child blocks. Persist failure budget/latch and quarantine. Unexpected active-child exit restarts committed SHA in recovery-only mode, with no fabricated drain. When live WSL runs were reported, own one bounded 300s keepalive bridge and stop only that created handle after the new watchdog acknowledges demand. Reserve logging for bounded errors without env/token dumps.
- [ ] **Step 4: Run `python -m unittest tests.test_hub_supervisor -v` again.** Expected: all focused cases pass with no skipped platform-independent tests. Check existing callers retain compatible defaults. For platform-specific cases record the platform and actual result separately.
- [ ] **Step 5: Review scope and hand off.** Run `git diff --check` and inspect changed paths against this task's JSON scope. Codex reports ready_to_commit and a one-sentence commit message without staging/committing; Drover makes the commit and gates the exact SHA. Do not call the task READY without the exact-SHA passing receipt.

### Task #4: Fence dispatcher admission and prove landing and gates have drained

**Size/risk:** M/high. **Dependencies:** #0.

**Files:**

- Modify: `hub/dispatch.py`
- Modify: `drover/dispatcher.py`
- Create: `tests/test_reload_dispatch.py`

**Interfaces:** Consume ActivityBarrier. Extend Dispatcher(..., activity=None) and DispatcherService(..., activity=None, on_landed=None); old callers retain behavior. Produce Dispatcher.reload_snapshot() -> dict with ack_generation, recovery_complete, scan_known, open_gate_ids, unresolved_launches, unsafe_detached_ids, wsl_live_run_ids and idle. Produce DispatcherService.reload_snapshot() -> dict including landing_idle and landing_ack_generation; on_landed(result: dict, repo_identity: dict) -> None fires after successful full landing cleanup. DispatcherService.stop(timeout=30) -> bool returns false while either thread lives and never releases dispatcher.lock early.

- [ ] **Step 1: Pin the failing behavior in `tests/test_reload_dispatch.py`.** Use a minimal temporary fixture with injected adapters; the assertion sketch below fixes the required behavior (fixture helpers are local to this task's test file). Add the other listed acceptance cases from the JSON as focused tests.

```python
def test_second_gate_platform_waits_for_commit(self):
    activity.close(12)
    finish_first_platform_gate()
    dispatcher.tick()
    self.assertEqual(new_gate_launches, [])
    self.assertEqual(recorded_first_platform_sha, gated_sha)
```

- [ ] **Step 2: Run `python -m unittest tests.test_reload_dispatch -v`.** Expected initially: failure for missing interface/behavior, with no real machine mutation. Record the observed failure; do not accept a dependency-related skip as red.
- [ ] **Step 3: Implement the interfaces in this task's owned files.** Guard each gate/review/worker admission and entire claim/launch handshake, not just top of tick; guard entire LandingService._land_repo call through finally, cleanup, recording and callback. With barrier closed continue heartbeat/reap/recover/watch and terminal gate ingestion, but launch no follow-on platform gate, review, repair or landing. Wait for every open gate run to end in ledger; healthy worker/review lifetimes remain detached and are excluded from section counts. Inspect status detachment evidence for live Windows runs; unknown/in-job blocks. Report unknown scan/recovery/unresolved launch as blockers, not empty work. Inject activity into landing and default dispatcher without changing admission capacity or push policy. Close per-thread Store connections on normal exit; release dispatcher lock only after both landing and dispatcher threads exit. Current reference: hub/dispatch.py:146-185,235-278 and drover/dispatcher.py:987-1066,1195-1243.
- [ ] **Step 4: Run `python -m unittest tests.test_reload_dispatch -v` again.** Expected: all focused cases pass with no skipped platform-independent tests. Check existing callers retain compatible defaults. For platform-specific cases record the platform and actual result separately.
- [ ] **Step 5: Review scope and hand off.** Run `git diff --check` and inspect changed paths against this task's JSON scope. Codex reports ready_to_commit and a one-sentence commit message without staging/committing; Drover makes the commit and gates the exact SHA. Do not call the task READY without the exact-SHA passing receipt.

### Task #5: Park service mutations and preserve WSL demand during handoff

**Size/risk:** S/low. **Dependencies:** #0.

**Files:**

- Modify: `hub/services.py`
- Modify: `hub/watchdog.py`
- Create: `tests/test_reload_services.py`

**Interfaces:** Consume ActivityBarrier. Extend Services(..., activity=None), Services.reload_snapshot() -> dict with idle and ack_generation, Services.stop(timeout=30) -> bool. Extend Watchdog(..., live_wsl_runs=None) with an injected callable returning known live WSL run IDs; Watchdog.reload_snapshot() -> dict reports thread_idle and keepalive demand/ownership; Watchdog.stop(timeout=30) -> bool joins its thread. Existing constructors and behavior remain compatible when callbacks omitted.

- [ ] **Step 1: Pin the failing behavior in `tests/test_reload_services.py`.** Use a minimal temporary fixture with injected adapters; the assertion sketch below fixes the required behavior (fixture helpers are local to this task's test file). Add the other listed acceptance cases from the JSON as focused tests.

```python
def test_row_boundary_retains_remaining_work(self):
    activity.close(12)
    finish_current_row()
    self.assertEqual(removed_branches, ['one'])
    self.assertFalse(garden_done_recorded)
    self.assertTrue(services.reload_snapshot()['idle'])
```

- [ ] **Step 2: Run `python -m unittest tests.test_reload_services -v`.** Expected initially: failure for missing interface/behavior, with no real machine mutation. Record the observed failure; do not accept a dependency-related skip as red.
- [ ] **Step 3: Implement the interfaces in this task's owned files.** Fence garden row, tuner pass and discovery mutation boundaries; finish an admitted unit, then park. Do not mark a partially parked garden batch done: retain per-row receipts and use existing resume_garden handling after commit. No new mutation while candidate validates. Watchdog may keep WSL warm during drain/recovery; live WSL runs sustain demand even if the old _wsl_needed note expires. Report keepalive ownership to parent so its own bridge overlaps until the new watchdog holds demand; do not stop WSL or another process. Thread owners close their connections when exiting. Existing services methods are at hub/services.py:118-136,200-225,249-278 and watchdog behavior at hub/watchdog.py:54-89,104-120.
- [ ] **Step 4: Run `python -m unittest tests.test_reload_services -v` again.** Expected: all focused cases pass with no skipped platform-independent tests. Check existing callers retain compatible defaults. For platform-specific cases record the platform and actual result separately.
- [ ] **Step 5: Review scope and hand off.** Run `git diff --check` and inspect changed paths against this task's JSON scope. Codex reports ready_to_commit and a one-sentence commit message without staging/committing; Drover makes the commit and gates the exact SHA. Do not call the task READY without the exact-SHA passing receipt.

### Task #6: Coordinate self-change detection, drain acknowledgments and durable history

**Size/risk:** M/high. **Dependencies:** #0, #1, #4, #5.

**Files:**

- Create: `hub/reload.py`
- Create: `tests/test_hub_reload.py`

**Interfaces:** Consume ActivityBarrier, Journal, runtime.check_eligibility and dispatcher/services reload_snapshot contracts. Produce ReloadCoordinator(store, settings, enrollment, activity, dispatch_snapshot, services_snapshot, watchdog_snapshot, adapters).request(action: str, source: str, key: str) -> dict; .set_auto(enabled: bool) -> dict; .on_landed(result: dict, repo_identity: dict) -> None; .tick() -> dict; .snapshot() -> dict; .ready() -> tuple[bool,dict]; .start() -> None; .stop(timeout:float=30) -> bool. Coordinator never launches or kills a process. Use enrollment protocol and exact source identity, not snapshot __file__.

- [ ] **Step 1: Pin the failing behavior in `tests/test_hub_reload.py`.** Use a minimal temporary fixture with injected adapters; the assertion sketch below fixes the required behavior (fixture helpers are local to this task's test file). Add the other listed acceptance cases from the JSON as focused tests.

```python
def test_docs_only_landing_does_not_request(self):
    coordinator.on_landed(docs_only_batch, enrolled_identity)
    coordinator.tick()
    self.assertEqual(request_files(), [])
    self.assertFalse(activity.snapshot()['closed'])
```

- [ ] **Step 2: Run `python -m unittest tests.test_hub_reload -v`.** Expected initially: failure for missing interface/behavior, with no real machine mutation. Record the observed failure; do not accept a dependency-related skip as red.
- [ ] **Step 3: Implement the interfaces in this task's owned files.** Identify successful completed self-landings by persisted batch ID/tip, enrolled canonical Git common-dir/branch and exact local source HEAD; detect hub/ or drover/ changes with git diff --name-only --no-renames running_sha tip -- hub/ drover/. Other repo, remote-only, failed or unrecorded tips do not reload; docs-only stays idle. Every 30s and at startup reconcile persisted landed batches to recover missed callback; use idempotency key landing:<repo>:<batch_id>:<tip>. Auto off still surfaces pending. set_auto enqueues a set_auto request with enabled:boolean; parent state is authoritative and journal mirroring projects hub_auto_reload, including false after fallback. Stage eligibility failures remain pending without draining. Close barrier on current-generation drain command, aggregate fresh participants plus live gate/detachment/scan proof, write child status and signal shutdown only when all safe. Keep child recovery-only until matching parent committed pointer; fallback remains closed until retry or cancel. Mirror journal events transactionally with ID deduplication and reserve hub.reload events for trusted paths. Tick coordinator every 1s; increment its heartbeat sequence only after a recovery tick is acknowledged, while dispatcher thread is alive and last acknowledgment age <=45s. Readiness requires that fresh per-instance heartbeat sequence, successful dispatcher lock/recovery, and owner UI listener binding, with optional missing WSL listener degraded. No DB migration/restore or owner dispatch preference overwrite.
- [ ] **Step 4: Run `python -m unittest tests.test_hub_reload -v` again.** Expected: all focused cases pass with no skipped platform-independent tests. Check existing callers retain compatible defaults. For platform-specific cases record the platform and actual result separately.
- [ ] **Step 5: Review scope and hand off.** Run `git diff --check` and inspect changed paths against this task's JSON scope. Codex reports ready_to_commit and a one-sentence commit message without staging/committing; Drover makes the commit and gates the exact SHA. Do not call the task READY without the exact-SHA passing receipt.

### Task #7: Expose owner reload controls and reconnect-aware status

**Size/risk:** M/low. **Dependencies:** #0, #6.

**Files:**

- Create: `hub/reload_api.py`
- Modify: `hub/api.py`
- Modify: `hub/store.py`
- Modify: `hub/templates/health.html`
- Create: `hub/templates/_reload.html`
- Modify: `hub/static/app.js`
- Create: `tests/test_reload_controls.py`

**Interfaces:** Consume ReloadCoordinator.request/set_auto/snapshot/ready. Produce hub.reload_api.router (JSON), .ui_router (forms), and .ready_router (loopback-only readiness). GET /api/v1/hub/reload returns snapshot; POST accepts action=request|retry|cancel and an idempotency key, returns 202 with request/status; PUT /api/v1/hub/reload/auto accepts enabled:boolean. GET /api/v1/hub/ready returns 200 or 503 from coordinator.ready, exposing only readiness identity fields; require loopback socket peer and owner UI local port, with no bearer credential. Other reload routes remain owner-only. UI POST /hub/reload and /hub/reload/auto delegate to the same controller and redirect to /health. Extend UiView(..., reload=None).health() to include reload card data; existing callers keep working.

- [ ] **Step 1: Pin the failing behavior in `tests/test_reload_controls.py`.** Use a minimal temporary fixture with injected adapters; the assertion sketch below fixes the required behavior (fixture helpers are local to this task's test file). Add the other listed acceptance cases from the JSON as focused tests.

```python
def test_token_cannot_request_reload(self):
    response = client.post('/api/v1/hub/reload', headers={'Authorization':'Bearer tok'}, json={'action':'request','key':'owner-1'})
    self.assertEqual(response.status_code, 403)
    self.assertEqual(request_files(), [])
```

- [ ] **Step 2: Run `python -m unittest tests.test_reload_controls -v`.** Expected initially: failure for missing interface/behavior, with no real machine mutation. Record the observed failure; do not accept a dependency-related skip as red.
- [ ] **Step 3: Implement the interfaces in this task's owned files.** Use existing owner identity and CSRF gates; a bearer token cannot request, retry, cancel, toggle auto or forge hub.reload events. Add event/setting prefix reservations in hub/api.py; reject all generic writes to reload-owned settings even for owner; dedicated auto control enqueues set_auto and returns 202, with parent-authoritative state mirrored into ledger. Surface current SHA, pending candidate, auto flag, phase, blocker, last error/time and supported start command when unsupervised. Include Reload now, Retry and Cancel drain only where allowed; no force control. Keep /api/v1/health backward compatible as liveness, separate from ready. On a changed instance UUID after SSE reconnect fetch fresh status and resync current page regardless of old Last-Event-ID; do not wait for higher SSE IDs. Current owner checks: hub/api.py:161-172,279-285,352-362; existing health template: hub/templates/health.html:4-25; memory SSE IDs: hub/events.py:29-54.
- [ ] **Step 4: Run `python -m unittest tests.test_reload_controls -v` again.** Expected: all focused cases pass with no skipped platform-independent tests. Check existing callers retain compatible defaults. For platform-specific cases record the platform and actual result separately.
- [ ] **Step 5: Review scope and hand off.** Run `git diff --check` and inspect changed paths against this task's JSON scope. Codex reports ready_to_commit and a one-sentence commit message without staging/committing; Drover makes the commit and gates the exact SHA. Do not call the task READY without the exact-SHA passing receipt.

### Task #8: Wire supervised startup, API mutation fencing and complete graceful exit

**Size/risk:** M/high. **Dependencies:** #0, #3, #4, #5, #6, #7.

**Files:**

- Modify: `hub/__main__.py`
- Modify: `hub/app.py`
- Modify: `hub/settings.py`
- Create: `tests/test_reload_lifecycle.py`

**Interfaces:** Consume ReloadCoordinator, reload_api router/ui_router/ready_router, shared ActivityBarrier, dispatcher/services/watchdog extensions. Extend Settings with optional enrollment, runtime_sha, generation, instance and supervisor-control paths, all absent for legacy tests. Extend create_app with injectable reload coordinator/activity factory. Extend serve(app, settings, discover=..., stop_requested=None) to stop UI server, API listener and adapter loop together; main(argv=None) returns 75 only after proven drained supervised shutdown, 0 for owner stop, nonzero startup failure. app.state.shutdown() -> bool reports all threads stopped, without closing store before that proof.

- [ ] **Step 1: Pin the failing behavior in `tests/test_reload_lifecycle.py`.** Use a minimal temporary fixture with injected adapters; the assertion sketch below fixes the required behavior (fixture helpers are local to this task's test file). Add the other listed acceptance cases from the JSON as focused tests.

```python
def test_candidate_api_claim_is_fenced(self):
    response = client.post('/api/v1/tasks/claim', headers=owner_headers, json={})
    self.assertEqual(response.status_code, 503)
    self.assertEqual(response.headers['Retry-After'], '5')
    self.assertEqual(claimed_task_ids(), [])
```

- [ ] **Step 2: Run `python -m unittest tests.test_reload_lifecycle -v`.** Expected initially: failure for missing interface/behavior, with no real machine mutation. Record the observed failure; do not accept a dependency-related skip as red.
- [ ] **Step 3: Implement the interfaces in this task's owned files.** Validate stable enrollment/control version and generation before Store opens/migrates; lock candidate admission before app creation. Build one coordinator/barrier shared by dispatch, services and routers, pass WSL live demand to watchdog and reload status to UiView. Count admitted HTTP mutation requests in the same barrier before routing; once closed, return 503/Retry-After:5 for new mutations except reload control/status and owner dispatch preference changes, with permitted controls admitted through activity.try_enter("api-control", control=True) and counted to finish before final exit. At stopping call activity.seal(generation). After drained/stopping, deny all new mutations including permitted controls and wait for existing controls to finish before exit. Existing reads/liveness remain available. Set UI-bound readiness only after actual bind. Start candidate recovery-only, keep services parked until commit, and never change dispatch_enabled as part of reload. Coordinate parent command to stop both listeners (including missing/changed WSL adapter) and join all background threads/close connections, release lock only after landing and dispatch exit. Direct -m hub remains diagnostic and advertises reload unavailable. Main references: hub/__main__.py:43-49,76-125; hub/app.py:16-53,59-69.
- [ ] **Step 4: Run `python -m unittest tests.test_reload_lifecycle -v` again.** Expected: all focused cases pass with no skipped platform-independent tests. Check existing callers retain compatible defaults. For platform-specific cases record the platform and actual result separately.
- [ ] **Step 5: Review scope and hand off.** Run `git diff --check` and inspect changed paths against this task's JSON scope. Codex reports ready_to_commit and a one-sentence commit message without staging/committing; Drover makes the commit and gates the exact SHA. Do not call the task READY without the exact-SHA passing receipt.

### Task #9: Provide one non-elevated owner start command and enroll the stable bootstrap

**Size/risk:** M/low. **Dependencies:** #0, #1, #2, #3, #8.

**Files:**

- Create: `hub/scripts/Start-DroverHub.ps1`
- Modify: `hub/scripts/Install-DroverHub.ps1`
- Modify: `hub/scripts/Install-Drover.ps1`
- Modify: `hub/scripts/README-INSTALL.md`
- Modify: `README.md`
- Create: `tests/powershell/test_hub_launcher.ps1`

**Interfaces:** Consume standalone bootstrap supervisor.py/runtime.py/reload_protocol.py/reload_machine.py and enrollment protocol. Produce ./hub/scripts/Start-DroverHub.ps1 [-Status|-Stop], resolving source root from PSScriptRoot in source mode or enrollment in installed mode, with registered config/venv. Existing Install-DroverHub.ps1 explicitly enrolls a stopped clean baseline, copies all four bootstrap peers into state/hub-runtime/bootstrap/v1, and also freezes the wrapper and registers DroverHub to invoke that installed copy. Compatibility digests cover the wrapper and both installer scripts. Keep installer parameters backward compatible. Enrollment fixes native paths, interpreter identity, schema baseline and Git identity without installing on start.

- [ ] **Step 1: Pin the failing behavior in `tests/powershell/test_hub_launcher.ps1`.** Use a minimal temporary fixture with injected adapters; the assertion sketch below fixes the required behavior (fixture helpers are local to this task's test file). Add the other listed acceptance cases from the JSON as focused tests.

```powershell
# PowerShell harness uses fake Start-Process/Register-ScheduledTask adapters.
# Assert checkout path with spaces is one argument and scheduled/manual targets agree.
# Assert no adapter observes package installation, elevation, or a tree-kill request.
```

- [ ] **Step 2: Run `powershell.exe -NoProfile -File tests/powershell/test_hub_launcher.ps1`.** Expected initially: failure for missing interface/behavior, with no real machine mutation. Record the observed failure; do not accept a dependency-related skip as red.
- [ ] **Step 3: Implement the interfaces in this task's owned files.** Default wrapper starts one foreground supervisor or returns existing status; Status works without HTTP and Stop requests safe drain/no restart. Pass arguments as arrays with correct quoting for scheduled-task paths; no hand-written uv/PYTHONPATH command, execution-policy bypass, automatic package install, elevation or process-name kill. Refuse legacy/unknown running dispatcher instead of stopping it. Honor persistent failure latch. Existing Install-Drover.ps1 may retain its explicit owner-requested setup sync but print the new supported start command. Owner-only state ACL applies to bootstrap/control/release data. Document migration, auto-off first run, successful manual test before enabling auto, standalone CLI coordination rule, same-schema-only rollback with main unchanged, recovery/latch reset by stopped owner repair and safe release retention. Update both README entrypoints. Existing installer/start evidence: hub/scripts/Install-DroverHub.ps1:13-25, hub/scripts/Install-Drover.ps1:88-112, README.md:24-28.
- [ ] **Step 4: Run `powershell.exe -NoProfile -File tests/powershell/test_hub_launcher.ps1` again.** Expected: all focused cases pass with no skipped platform-independent tests. Check existing callers retain compatible defaults. For platform-specific cases record the platform and actual result separately.
- [ ] **Step 5: Review scope and hand off.** Run `git diff --check` and inspect changed paths against this task's JSON scope. Codex reports ready_to_commit and a one-sentence commit message without staging/committing; Drover makes the commit and gates the exact SHA. Do not call the task READY without the exact-SHA passing receipt.

### Task #10: Exercise successful replacement and rollback with detached runs

**Size/risk:** M/high. **Dependencies:** #3, #4, #5, #6, #7, #8, #9.

**Files:**

- Create: `tests/test_reload_integration.py`

**Interfaces:** Consume installed-bootstrap and hub-child protocol end to end; no new production API. Use temporary local Git repos, state, ledgers and stdlib synthetic runners, injectable OS/HTTP/clock adapters and bounded subprocesses. Preserve the same supervisor/child entrypoints tested above; no real services, owner data or package installs. Windows survival scenarios are explicitly Windows-only and must execute on an authorized Windows worker; WSL CI uses fakes and temporary native ledgers.

- [ ] **Step 1: Pin the failing behavior in `tests/test_reload_integration.py`.** Use a minimal temporary fixture with injected adapters; the assertion sketch below fixes the required behavior (fixture helpers are local to this task's test file). Add the other listed acceptance cases from the JSON as focused tests.

```python
def test_completion_during_rollback_is_ingested_once(self):
    fixture.fail_candidate_import()
    fixture.finish_worker_while_no_hub(run_id=610)
    fixture.drive_until('rolled_back')
    self.assertEqual(fixture.finished_events(610), 1)
    self.assertEqual(fixture.main_sha(), fixture.sha_b)
    self.assertEqual(fixture.new_worker_launches(), 0)
```

- [ ] **Step 2: Run `python -m unittest tests.test_reload_integration -v`.** Expected initially: failure for missing interface/behavior, with no real machine mutation. Record the observed failure; do not accept a dependency-related skip as red.
- [ ] **Step 3: Implement the interfaces in this task's owned files.** Build one integrated fixture: enrolled A, recorded self-landed B, safe detached Windows worker plus WSL review, outstanding task gate and a full landing operation. Request reload, prove no replacement until gate receipt and whole landing finish, complete an existing worker during outage, then validate B and commit once with preserved run IDs. Next exercise import failure B/fallback A, stuck validation child, failing fallback and supervisor restart from interrupted durable transitions. Assert main is never reset, landed tasks stay landed, no ledger restore occurs, adoption/reaping happens exactly once and failed latch prevents endless restart. Include owner changing dispatch preference while draining and optional WSL listener unavailable. Tests must fail clearly, rather than silently claiming Windows behavior from a skipped run.
- [ ] **Step 4: Run `python -m unittest tests.test_reload_integration -v` again.** Expected: all focused cases pass with no skipped platform-independent tests. Check existing callers retain compatible defaults. For platform-specific cases record the platform and actual result separately.
- [ ] **Step 5: Review scope and hand off.** Run `git diff --check` and inspect changed paths against this task's JSON scope. Codex reports ready_to_commit and a one-sentence commit message without staging/committing; Drover makes the commit and gates the exact SHA. Do not call the task READY without the exact-SHA passing receipt.

## Final verification and rollout handoff

- [ ] Validate the task JSON before import: required fields/types, each goal header, only S/M sizes and low/high risks, earlier-index deps, and no pair of scope paths overlapping by equality or directory prefix.
- [ ] On the assembled implementation, run `python -m unittest discover -s tests -t .` in the preinstalled hub test environment. Expected: full suite passes; new platform-independent reload tests execute with zero skips. Report Windows-only results separately; a Linux mock run does not establish Windows job survival.
- [ ] Obtain the required authorized Windows receipt: one Windows detached worker and one WSL detached review survive successful replacement and fallback with unchanged IDs; main stays landed; an unknown/in-job runner blocks. All test state is disposable and no live service is started.
- [ ] Review every requirement in the design against #0–#10 and every interface name against its consumer. Treat missing ownership as a plan revision; a worker must not expand scope to fix it.
- [ ] After exact-SHA implementation gates/review, present the owner installation/migration rehearsal from #9. Installation, scheduled-task changes, enabling auto and live operation are explicit later owner actions. This research task performs none of them.

Research verification is limited to documentary consistency, source references, JSON validity/dependency/scope checks and docs-only changes; it is not an implementation gate receipt. Recommendations are pending owner review. The externally assigned research branch is retained; requested T-10 naming belongs to the orchestrator.
