# Safe Self-Reload Implementation Plan

> Historical document, written before the 2026-10 terminology change. Read: gate -> checks / full CI, receipt -> CI result, landing -> merging, landed -> merged, landing batch -> merge group, needs-owner -> Waiting on you, docket -> Inbox, garden -> cleanup, rung/tier/ladder -> escalation level, HOLD/LAND -> changes requested/approved. See GLOSSARY.md.

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
- Protocol/run format are 1; identical schema/dependency/bootstrap blobs and interpreter identity are required. Owner upgrades handle changes to those contracts. Child status and readiness include actual ledger_schema, equal to enrollment and release; enrollment and supervised startup independently derive the release required_schema from MIGRATIONS and require it to match the actual ledger via a non-creating mode=ro probe before Store construction. All supervised Store connections keep migrate_db=False, including later threads.
- Supervisor and child have separate locks. Unknown identity, scan, detachment or compatibility blocks instead of forcing. Controls target operation_id/generation/instance; stale controls cannot reopen admission or affect a later same-SHA attempt.
- Fallback selects a retained healthy runtime once, disables auto and holds admission; main, landed task history and ledger are preserved. Candidate source checks and retained-runtime recovery checks are separate: recovery never requires source HEAD/cleanliness/ancestry to match its SHA.
- Frozen bootstrap peers are supervisor.py, runtime.py, reload_protocol.py **and reload_machine.py**; all use only stdlib; select package imports vs standalone sibling imports by __package__. Freeze the start wrapper too; its installed mode reads source paths from enrollment. Compatibility covers both installer scripts.
- No personal paths, tokens or environment dumps in docs or events. Workers never operate the live scheduled task/service or owner processes.
- Windows dotnet/MSBuild and Windows-only verification go to an authorized Windows worker. Do not install missing test dependencies.

## Review Focus

1. Unknown WSL scan or Windows in-job runner must block, rather than look idle: task #4 tests recovery/detachment blockers; #11 verifies survival on Windows.
2. Crash between durable journal append and active-pointer replace must replay once: #0 tests torn state; #3 tests interrupted effects.
3. Standalone API claim during validation must get 503 even if dispatch_enabled is true: #8 tests the mutation middleware.
4. Candidate source must be rechecked before drain/spawn, while fallback A must start with source still B: #1 tests separate candidate/recovery validators; #3/#11 prove fallback and crash recovery without source reset, still refusing incompatible retained releases.
5. SSE IDs restart at one; reconnection to a changed instance must resync instead of showing stale status: #7 tests UUID-based reconnect behavior.

---

## Execution and file ownership

The JSON sibling is the importable source of task goals, acceptance, exact file scopes and 0-based dependencies. Each goal repeats the Type/Repo/Write access/Read access/Branch-worktree/Connected tasks header and the operational constraints because a worker sees only its assigned task. Index references `#N` refer to this list, not real task IDs. **Import preparation must resolve each index to the earlier created task ID**: today's importer forwards deps into task creation (`hub/store.py:351-354`), which resolves them as existing IDs (`drover/ledger/ops.py:250-265,302-309`). Do not post these unresolved index strings directly to the current hub API.

Current behavior is grounded in the spec's main-SHA evidence table. Integration anchors: main `hub/dispatch.py:147-186,236-279` (landing/thread lock), `drover/dispatcher.py:1006-1089,1227-1275` (adoption/admission), `hub/app.py:25-54` and `hub/__main__.py:43-49,76-125` (custom lifecycle), `hub/api.py:213-215,251-255` (liveness/claim), `hub/scripts/Install-DroverHub.ps1:13-25` (scheduled launcher). T-7's fixed-checkout field evidence is recorded in the spec; examples do not pin the owner's actual schema version. All unprefixed citations use the spec's fixed main research baseline.

All production files listed below are **proposed** ownership; no two tasks share a file or directory prefix. New focused tests use existing unittest conventions (`tests/test_hub_dispatch.py:6-18,27-36`). No dependency or schema file is modified. Tasks #1, #2, #4 and #5 may follow #0 independently; #3 follows #1/#2; #6 follows #1/#4/#5; #7 follows #6; #8 joins supervisor, drain and routes; #9 adds enrollment; independent #10 repairs detach-test contract coverage; #11 tests the assembled system after #10. Admission capacity remains Drover's decision; this plan does not authorize extra parallel builds.

## Shared record and interface rules

Read the design's schema/example for record fields and legal phases. New constructor parameters are optional to preserve existing tests/callers. Snapshots are plain dicts with the exact keys below, not new ledger models. `ActivityBarrier` owns synchronous admission counts; dispatcher owns gate/run proof; services own their boundary acknowledgments; coordinator aggregates these into `child.json`; parent alone owns `state.json` and journal.

`child.json.barrier` must include `active_sections`, `open_gate_ids`, `landing_idle`, `services_idle`, `recovery_complete`, `scan_known`, `unsafe_detached_ids`, `unresolved_launches`, and `wsl_live_run_ids`, plus participant generation acknowledgments: dispatcher ack_generation, landing landing_ack_generation, services ack_generation, and API barrier generation only when its admitted sections are idle. These are updated after the participant has observed the closed barrier, never inferred from an old heartbeat. Parent starts and retains the WSL bridge until the new watchdog acknowledges live-run demand. Enrollment includes the installed reducer in the bootstrap digest; any of its source changes requires stopped owner re-enrollment.

The ready payload is `{protocol:1, runtime_sha:str, operation_id:str|null, generation:int, instance:str, ledger_schema:int|null, heartbeat_seq:int, recovery_complete:bool, dispatcher_lock:bool}`; 200 requires the actual ledger_schema to equal enrollment/release, recovery finished and UI bound for that instance. Child status includes the same schema and identity fields plus the design's full barrier proof. Coordinator queries schema_version(store.conn) every heartbeat; query failure publishes null, freezes sequence and returns 503; mismatch publishes the actual integer but also prevents successful heartbeat/challenge advancement and readiness. Parent issues a new probe and accepts an advancing sequence/challenge echo only within 5 monotonic seconds for the expected operation/generation/instance/PID creation identity, before drain/stopping and during the 15 stable readiness seconds. File age/persisted heartbeat is never schema proof. No child means retained recovery may spawn only into non-creating read-only preflight, never infer readiness from historical status. A planned candidate spawn may consume the final handoff_schema proof only after exit 75, for the same operation within the 30s shutdown window; expiry or supervisor restart selects retained recovery. `active` remains the last healthy runtime until durable commit. Parent never reads SQLite. All clock-dependent tests use a fake monotonic clock.

Request records include the observed `(operation_id, generation, instance)` from parent status. Cancel/retry require non-null operation_id and exact match; null is only valid when status has no operation, never as a wildcard. The coordinator and forms preserve the submitted tuple; they never retarget stale actions. Parent returns stored exact-duplicate results before checking new controls atomically against the current tuple, stores idempotency key/payload/result, and rejects stale_control without barrier/state mutation (409 at ingress; durable rejection if racing after 202). A retry retires the old operation and mints a new UUID even for the same SHA/PID, advancing generation; each spawn changes generation/instance while fallback retains the operation ID. Commands/child status/journal carry operation_id too. A newer drain command must match durable parent state and this child instance; cancel matches only the current tuple. Exact duplicates return prior results without effects; changed payload under the same key conflicts.

Runtime task #1 exposes separate check_eligibility and check_recovery. Candidate checks require recorded exact local HEAD/ancestry and clean source, plus live schema evidence before drain/final stop; supervisor rechecks source/contracts immediately before candidate spawn using only the narrowly valid handoff proof if no child is alive. Recovery checks use only an enrolled baseline or durable active/previous healthy receipt and its anchored manifest/content/schema/dependency/bootstrap/protocol/interpreter/path/WSL contracts; they never inspect current source HEAD/cleanliness/ancestry/blobs or re-export A. First startup, committed-runtime crash recovery and fallback use this path. Every supervised child checks the actual pinned ledger through the non-creating read-only probe against its independently derived release requirement before Store, then must produce fresh matching schema/identity/heartbeat for readiness. A retained mismatch blocks; it never falls through to candidate eligibility.

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

Add `test_control_identity_required`: missing operation_id/generation/instance or null operation for cancel/retry fails record validation; a valid observed tuple round-trips unchanged. Add `test_child_schema_required`: missing ledger_schema and boolean/negative schema fail, explicit null is valid status but never ready, and both worked JSON examples validate.

- [ ] **Step 1b: Add compatibility regressions.** Test enrollment/release required_schema: missing, bool, negative or unequal actual/required values reject; valid matching pairs round-trip. Schema-field excerpts are not complete records; validate the full state and child examples with complete fixture enrollment/manifest.

- [ ] **Step 2: Run `python -m unittest tests.test_reload_protocol -v`.** Expected initially: failure for missing interface/behavior, with no real machine mutation. Record the observed failure; do not accept a dependency-related skip as red.
- [ ] **Step 3: Implement the interfaces in this task's owned files.** Implement the protocol records and enums from the design, strict full lowercase 40-hex SHAs and increasing generations/sequences. Journal append uses one immutable sequence file per event before replacing state; recover a complete journal record whose state update was interrupted. Exclude partial files, detect gaps/corruption, and block unknown versions. Child and parent writers never overwrite each other's records. Atomic writes flush and fsync before replace. An installer-supplied state root carries owner ACLs; never log secrets. A stale generation cannot reopen a current barrier. This foundational module must import without FastAPI or the drover package. Validate request.operation_id/generation/instance, command.operation_id and child.operation_id/ledger_schema, enrollment/release ledger_schema and required_schema (both required, strictly non-negative integers, equal within each record) and healthy receipt manifest_digest. Require UUID identity fields, non-negative integer generations/schema (reject booleans); child schema alone may be null before preflight/on read failure. Cancel/retry require non-null operation ID. Define handoff_schema with operation/generation/instance/creation identity/schema/challenge/sequence as in the worked example; null when no planned handoff. Preserve observed identities exactly, including the request identity preceding the new parent-assigned operation.
- [ ] **Step 4: Run `python -m unittest tests.test_reload_protocol -v` again.** Expected: all focused cases pass with no skipped platform-independent tests. Check existing callers retain compatible defaults. For platform-specific cases record the platform and actual result separately.
- [ ] **Step 5: Review scope and hand off.** Run `git diff --check` and inspect changed paths against this task's JSON scope. Codex reports ready_to_commit and a one-sentence commit message without staging/committing; Drover makes the commit and gates the exact SHA. Do not call the task READY without the exact-SHA passing receipt.

### Task #1: Export and validate immutable hub releases

**Size/risk:** M/low. **Dependencies:** #0.

**Files:**

- Create: `hub/runtime.py`
- Create: `tests/test_hub_runtime.py`

**Interfaces:** Consume reload_protocol validators/atomic_json. Produce prepare_release(source: Path, sha: str, root: Path, enrolled: dict) -> dict (manifest including release_path); check_eligibility(enrolled: dict, running_sha: str, target_sha: str, ledger_schema: int) -> dict with eligible:bool and reasons:list[str]; check_recovery(enrolled: dict, release: dict, healthy: dict) -> dict with the same result shape (release includes release_path, healthy is a durable active/previous receipt or explicit enrolled baseline); child_command(enrolled: dict, release: dict, generation: int, instance: str) -> tuple[list[str], dict[str,str], str] returning argv, env, cwd. Inject Git, disk and interpreter probes via keyword adapters for tests. No drover/application imports.

- [ ] **Step 1: Pin the failing behavior in `tests/test_hub_runtime.py`.** Use a minimal temporary fixture with injected adapters; the assertion sketch below fixes the required behavior (fixture helpers are local to this task's test file). Add the other listed acceptance cases from the JSON as focused tests.

```python
def test_schema_change_is_pending(self):
    result = check_eligibility(enrolled, sha_a, sha_b, ledger_schema=4)
    self.assertFalse(result['eligible'])
    self.assertIn('schema-change', result['reasons'])
```

Add `test_retained_a_recovers_with_source_b_c_or_dirty`: A's durable healthy receipt/manifest succeeds for each source condition with zero source-eligibility probes or exports; candidate eligibility still refuses stale/dirty/non-descendant tips. Add `test_retained_contract_mismatch_refused`: corrupt content/manifest, wrong healthy SHA, schema/dependency/bootstrap/protocol/interpreter mismatch and bad WSL mapping each refuse. Add `test_actual_schema_required`: candidate eligibility refuses null/missing/bool/mismatched schema instead of guessing from blobs.

- [ ] **Step 1b: Add compatibility regressions.** Test candidate and retained validation refuse a missing/unequal required_schema, including enrollment forged as ledger_schema=3/required_schema=4; no parent SQLite or candidate-code import is introduced.

- [ ] **Step 2: Run `python -m unittest tests.test_hub_runtime -v`.** Expected initially: failure for missing interface/behavior, with no real machine mutation. Record the observed failure; do not accept a dependency-related skip as red.
- [ ] **Step 3: Implement the interfaces in this task's owned files.** Export exact Git objects with git archive to a temporary release, validate ZIP paths/types/expanded bytes, hash contents and atomically rename. Reuse only verified identical releases; retain every release. For candidate check_eligibility compare canonical Git common-dir/default-branch identity, source cleanliness, exact HEAD/target ancestry, schema-file blob, hub dependency files, protocol/run format, interpreter identity and bootstrap blobs (four Python peers, Start-DroverHub.ps1 and both installer scripts). Refuse bootstrap, schema or dependency changes for auto and manual reload. Runtime does not query SQLite: ledger_schema is the actual integer supplied by the child after identity/freshness verification by the consumer, and must equal enrollment/release; missing/null/boolean/mismatch refuses eligibility. Include ledger_schema and required_schema in each release manifest, both equal to the independently verified enrollment pair and copied only after schema-blob equality. Reject missing/bool/mismatched required_schema in enrollment or retained manifests; copying an arbitrary observed schema is forbidden. Enrolled absolute state/config/repo/token paths stay fixed; paths.overlay is inert compatibility metadata, with no application overlay exports; launcher imports only from release, uses the existing Windows venv Python directly, and removes inherited project PYTHONPATH. Frozen files must support both package imports in the child and standalone sibling imports in bootstrap, selected by __package__. Require WSL reachability when reported WSL runs need it. Require free bytes >= 2 * expanded archive bytes + 256 MiB and expanded size <= 128 MiB. No Git ref mutation, worktree, install or release deletion. Implement check_recovery separately: select only the enrolled baseline or a SHA named by the durable healthy receipt; compare its anchored manifest_digest, all retained file digests, SHA, protocol/run format, schema/dependency/bootstrap digests and expected ledger_schema against enrollment, current configured interpreter identity, pinned paths and needed WSL reachability. Do not run source HEAD/cleanliness/ancestry/blob checks, export or stage on this path. Return specific refusal reasons on retained corruption or contract mismatch; never fall through to candidate checks. Retained recovery supplies no live schema assertion: its child must run non-creating read-only actual-versus-required-schema preflight, migration-disabled Store connections and fresh readiness before permission to resume.
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

Add `test_delayed_cancel_and_retry_cannot_affect_later_same_sha_drain`: after X retries into Y, inject X controls and independently wrong operation/generation/instance fields; assert rejection, identical phase/admission/auto, no command/spawn effects. Repeat after journal replay. Add `test_duplicate_control_does_not_reapply` and `test_schema_mismatch_never_commits`; stale/missing schema evidence cannot advance into drain/commit. Pin handoff expiry to recovery rather than candidate spawn.

- [ ] **Step 2: Run `python -m unittest tests.test_reload_machine -v`.** Expected initially: failure for missing interface/behavior, with no real machine mutation. Record the observed failure; do not accept a dependency-related skip as red.
- [ ] **Step 3: Implement the interfaces in this task's owned files.** Define idle/pending/draining/stopping/validating/committed and blocked/rolling_back/rolled_back/failed transitions exactly as the design. Coalesce newest descendant before draining only; pin target thereafter. Drain timeout 900s blocks without kill, shutdown timeout 30s blocks, readiness timeout 60s includes 15 stable advancing-heartbeat seconds, failed validation shutdown grace 10s. One fallback only; two attempts per ten minutes including fallback, persisted failed latch survives scheduled-task relaunch. Only healthy candidate commit changes active/previous. Fallback disables auto and keeps admission closed; cancel then explicitly resumes it. Distinguish intentional exit 75, owner stop, unexpected exit and identity uncertainty. Stale events cannot change state. Request events retain their observed operation_id/generation/instance. Compare them atomically with current state before any effect; reject stale tuples or disallowed phases, journal control_rejected and preserve phase/barrier/auto. Check stored key/payload/result before identity comparison: exact duplicates return the prior result without effects; key reuse with changed payload conflicts. For each new reload/stop or retry consume a parent-generated event.new_operation_id UUID (journaled before reducer application, making replay deterministic), retire the old ID even when SHA/PID is unchanged, and advance generation for a new drain/retry; fallback retains operation ID but advances generation/instance. Propagate identities through commands, child status and journal. Require schema-match/freshness evidence before drain/stopping/readiness; expired handoff permission or unexpected exit selects retained recovery, never an unfenced candidate spawn.
- [ ] **Step 4: Run `python -m unittest tests.test_reload_machine -v` again.** Expected: all focused cases pass with no skipped platform-independent tests. Check existing callers retain compatible defaults. For platform-specific cases record the platform and actual result separately.
- [ ] **Step 5: Review scope and hand off.** Run `git diff --check` and inspect changed paths against this task's JSON scope. Codex reports ready_to_commit and a one-sentence commit message without staging/committing; Drover makes the commit and gates the exact SHA. Do not call the task READY without the exact-SHA passing receipt.

### Task #3: Supervise one hub child and perform verified startup rollback

**Size/risk:** M/high. **Dependencies:** #0, #1, #2.

**Files:**

- Create: `hub/supervisor.py`
- Create: `tests/test_hub_supervisor.py`

**Interfaces:** Consume runtime.prepare_release/check_eligibility/check_recovery/child_command and protocol Journal/atomic_json. Consume reload_machine.transition as a frozen bootstrap peer (installer must copy reload_machine.py too). Produce Supervisor(enrollment: dict, adapters: object).step() -> dict and main(argv: list[str]|None=None) -> int. Adapters expose spawn(argv,env,cwd), identity(pid), poll(pid), probe(url), terminate(pid), git(...), disk(...), clock(), sleep(seconds), bridge_start(distro,seconds), bridge_stop(handle), lock(path). Native Windows lock is implemented locally or in reload_protocol, without importing drover. The installed parent uses only stdlib/bootstrap peers.

- [ ] **Step 1: Pin the failing behavior in `tests/test_hub_supervisor.py`.** Use a minimal temporary fixture with injected adapters; the assertion sketch below fixes the required behavior (fixture helpers are local to this task's test file). Add the other listed acceptance cases from the JSON as focused tests.

```python
def test_old_child_alive_prevents_spawn(self):
    supervisor.step()
    self.assertEqual(fake_processes.spawned, [])
    self.assertEqual(fake_processes.terminated, [])
    self.assertEqual(supervisor.state['phase'], 'blocked')
```

Add `test_parent_rejects_stale_same_sha_controls`: queued cancel/retry with retired operation ID, old generation or replaced instance cannot emit a current command, even across parent restart. Add `test_schema_proof_freshness`: missing/null/mismatch, unchanged sequence, wrong challenge/identity and >5s response refuse drain/commit; a valid report advances normally. Add `test_fallback_a_with_head_b`: after failed B with HEAD still B (also C/dirty), assert check_recovery, one A spawn, no candidate check/export/reset; changed interpreter or retained manifest/schema/dependencies block. Add `test_expired_handoff_recovers_a`: old proof, unexpected exit or parent restart cannot authorize B; A still requires fresh child readiness.

- [ ] **Step 2: Run `python -m unittest tests.test_hub_supervisor -v`.** Expected initially: failure for missing interface/behavior, with no real machine mutation. Record the observed failure; do not accept a dependency-related skip as red.
- [ ] **Step 3: Implement the interfaces in this task's owned files.** Execute durable reducer effects with idempotent journal/state writes and a supervisor.lock. Resolve request/control records by observed operation_id/generation/instance, with atomic reducer rejection of stale controls and durable key/payload/result deduplication; verify child identity by challenge-response (command action=probe with fresh challenge UUID echoed in child status on its next heartbeat), never PID file alone. Wait for observed old PID exit plus dispatcher-lock availability before replacement or fallback; for a new candidate recheck source HEAD/cleanliness, recorded target/ancestry, compatibility and interpreter identity immediately before drain and candidate spawn via check_eligibility; for fallback/committed-runtime crash recovery call check_recovery on the retained healthy manifest, without source HEAD/cleanliness/ancestry checks or re-export; never steal locks or create a second hub. Probe loopback /api/v1/hub/ready for exact protocol/SHA/operation_id/generation/instance, actual ledger_schema equal to enrollment/release, recovery=true, lock=true, and advancing heartbeat for 15 continuous seconds within 60s. Failures close validation-only child cooperatively, then after 10s may terminate its verified PID, never its tree; uncertainty blocks. On a parent restart reattach matching live child; unknown child blocks. Persist failure budget/latch and quarantine. Unexpected active-child exit restarts committed SHA in recovery-only mode, with no fabricated drain. When live WSL runs were reported, own one bounded 300s keepalive bridge and stop only that created handle after the new watchdog acknowledges demand. Reserve logging for bounded errors without env/token dumps. Before candidate drain and stopping and throughout readiness, require child ledger_schema with fresh probe challenge/advancing sequence from the expected PID creation identity within 5 monotonic seconds. Missing/null/bool/mismatch/read error/stale status refuses progression. Persist handoff_schema from the final verified sample: after proven drained exit 75, use it only for the same handoff within the 30s shutdown window, then require candidate non-creating read-only preflight and fresh readiness. Timeout, unexpected exit or parent restart invalidates candidate handoff permission and uses retained recovery. No-child recovery relies only on retained contract validation to spawn closed; never treats old schema status as ready or reads SQLite. Parent passes startup identity/expected schema to the child, and includes operation ID in commands/probes. Generate and journal event.new_operation_id before applying a new reload/stop/retry so the pure reducer never creates random IDs during replay. Status exposes observed tuple and durable per-request accepted/rejected results; no delayed action is automatically retargeted.
- [ ] **Step 4: Run `python -m unittest tests.test_hub_supervisor -v` again.** Expected: all focused cases pass with no skipped platform-independent tests. Check existing callers retain compatible defaults. For platform-specific cases record the platform and actual result separately.
- [ ] **Step 5: Review scope and hand off.** Run `git diff --check` and inspect changed paths against this task's JSON scope. Codex reports ready_to_commit and a one-sentence commit message without staging/committing; Drover makes the commit and gates the exact SHA. Do not call the task READY without the exact-SHA passing receipt.

### Task #4: Fence dispatcher admission and prove landing and gates have drained

**Size/risk:** M/high. **Dependencies:** #0.

**Files:**

- Modify: `hub/dispatch.py`
- Modify: `drover/dispatcher.py`
- Create: `tests/test_reload_dispatch.py`

**Interfaces:** Consume ActivityBarrier. Extend Dispatcher(..., activity=None) and DispatcherService(..., activity=None, on_landed=None, dispatcher_lock=None); old callers retain behavior. Produce Dispatcher.reload_snapshot() -> dict with ack_generation, recovery_complete, scan_known, open_gate_ids, unresolved_launches, unsafe_detached_ids, wsl_live_run_ids and idle. Produce DispatcherService.reload_snapshot() -> dict including landing_idle and landing_ack_generation; on_landed(result: dict, repo_identity: dict) -> None fires after successful full landing cleanup. DispatcherService.stop(timeout=30) -> bool returns false while either thread lives and never releases dispatcher.lock early. Optional dispatcher_lock is an already-acquired FileLock passed by supervised main; start adopts it without reacquiring, stop releases it only after both threads exit. Legacy callers still construct/acquire the lock themselves.

- [ ] **Step 1: Pin the failing behavior in `tests/test_reload_dispatch.py`.** Use a minimal temporary fixture with injected adapters; the assertion sketch below fixes the required behavior (fixture helpers are local to this task's test file). Add the other listed acceptance cases from the JSON as focused tests.

```python
def test_second_gate_platform_waits_for_commit(self):
    activity.close(12)
    finish_first_platform_gate()
    dispatcher.tick()
    self.assertEqual(new_gate_launches, [])
    self.assertEqual(recorded_first_platform_sha, gated_sha)
```

Add `test_preacquired_dispatcher_lock_not_reacquired`: supervised startup transfers an already-held FileLock, start acquires it zero additional times, and stop releases it exactly once after both threads exit.

- [ ] **Step 2: Run `python -m unittest tests.test_reload_dispatch -v`.** Expected initially: failure for missing interface/behavior, with no real machine mutation. Record the observed failure; do not accept a dependency-related skip as red.
- [ ] **Step 3: Implement the interfaces in this task's owned files.** Guard each gate/review/worker admission and entire claim/launch handshake, not just top of tick; guard entire LandingService._land_repo call through finally, cleanup, recording and callback. With barrier closed continue heartbeat/reap/recover/watch and terminal gate ingestion, but launch no follow-on platform gate, review, repair or landing. Wait for every open gate run to end in ledger; healthy worker/review lifetimes remain detached and are excluded from section counts. Inspect status detachment evidence for live Windows runs; unknown/in-job blocks. Report unknown scan/recovery/unresolved launch as blockers, not empty work. Inject activity into landing and default dispatcher without changing admission capacity or push policy. Adopt an injected preacquired dispatcher_lock without reacquiring; keep legacy lock behavior when omitted. Close per-thread Store connections on normal exit; release dispatcher lock only after both landing and dispatcher threads exit. Current reference: hub/dispatch.py:147-186,236-279 and drover/dispatcher.py:1006-1089,1227-1275.
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
- [ ] **Step 3: Implement the interfaces in this task's owned files.** Fence garden row, tuner pass and discovery mutation boundaries; finish an admitted unit, then park. Do not mark a partially parked garden batch done: retain per-row receipts and use existing resume_garden handling after commit. No new mutation while candidate validates. Watchdog may keep WSL warm during drain/recovery; live WSL runs sustain demand even if the old _wsl_needed note expires. Report keepalive ownership to parent so its own bridge overlaps until the new watchdog holds demand; do not stop WSL or another process. Thread owners close their connections when exiting. Existing services methods are at hub/services.py:112-130,194-219,243-272 and watchdog behavior at hub/watchdog.py:54-89,104-120.
- [ ] **Step 4: Run `python -m unittest tests.test_reload_services -v` again.** Expected: all focused cases pass with no skipped platform-independent tests. Check existing callers retain compatible defaults. For platform-specific cases record the platform and actual result separately.
- [ ] **Step 5: Review scope and hand off.** Run `git diff --check` and inspect changed paths against this task's JSON scope. Codex reports ready_to_commit and a one-sentence commit message without staging/committing; Drover makes the commit and gates the exact SHA. Do not call the task READY without the exact-SHA passing receipt.

### Task #6: Coordinate self-change detection, drain acknowledgments and durable history

**Size/risk:** M/high. **Dependencies:** #0, #1, #4, #5.

**Files:**

- Create: `hub/reload.py`
- Create: `tests/test_hub_reload.py`

**Interfaces:** Consume ActivityBarrier, Journal, runtime.check_eligibility and dispatcher/services reload_snapshot contracts. Produce ReloadCoordinator(store, settings, enrollment, activity, dispatch_snapshot, services_snapshot, watchdog_snapshot, adapters).request(action: str, source: str, key: str, *, operation_id: str|None, generation: int, instance: str) -> dict; .set_auto(enabled: bool, key: str, *, operation_id: str|None, generation: int, instance: str) -> dict; .on_landed(result: dict, repo_identity: dict) -> None; .tick() -> dict; .snapshot() -> dict; .ready() -> tuple[bool,dict]; .start() -> None; .stop(timeout:float=30) -> bool. Coordinator never launches or kills a process. Use enrollment protocol and exact source identity, not snapshot __file__.

- [ ] **Step 1: Pin the failing behavior in `tests/test_hub_reload.py`.** Use a minimal temporary fixture with injected adapters; the assertion sketch below fixes the required behavior (fixture helpers are local to this task's test file). Add the other listed acceptance cases from the JSON as focused tests.

```python
def test_docs_only_landing_does_not_request(self):
    coordinator.on_landed(docs_only_batch, enrolled_identity)
    coordinator.tick()
    self.assertEqual(request_files(), [])
    self.assertFalse(activity.snapshot()['closed'])
```

Add `test_control_keeps_observed_operation`: enqueue X cancel after snapshot changes to Y and assert the file still targets X; Y's barrier stays closed when parent rejects it. Add `test_actual_schema_each_heartbeat`: temporary ledger reports its real schema; changed/missing/null/read-failed schema never yields ready or an advancing successful heartbeat/challenge, and a cached integer cannot mask a later mismatch. Assert child.json and readiness expose ledger_schema and operation identity, with no parent SQLite access.

- [ ] **Step 2: Run `python -m unittest tests.test_hub_reload -v`.** Expected initially: failure for missing interface/behavior, with no real machine mutation. Record the observed failure; do not accept a dependency-related skip as red.
- [ ] **Step 3: Implement the interfaces in this task's owned files.** Identify successful completed self-landings by persisted batch ID/tip, enrolled canonical Git common-dir/branch and exact local source HEAD; detect hub/ or drover/ changes with git diff --name-only --no-renames running_sha tip -- hub/ drover/. Other repo, remote-only, failed or unrecorded tips do not reload; docs-only stays idle. Every 30s and at startup reconcile persisted landed batches to recover missed callback; use idempotency key landing:<repo>:<batch_id>:<tip>. Auto off still surfaces pending. set_auto enqueues a set_auto request with enabled:boolean; parent state is authoritative and journal mirroring projects hub_auto_reload, including false after fallback. Stage eligibility failures remain pending without draining. Close barrier on current-generation drain command, aggregate fresh participants plus live gate/detachment/scan proof, write child status and signal shutdown only when all safe. Keep child recovery-only until matching parent committed pointer; fallback remains closed until retry or cancel. Mirror journal events transactionally with ID deduplication and reserve hub.reload events for trusted paths. Tick coordinator every 1s; read actual schema_version(store.conn) into ledger_schema on each heartbeat and increment its heartbeat sequence only after that query succeeds with the enrolled/release version and a recovery tick is acknowledged, while dispatcher thread is alive and last acknowledgment age <=45s. Readiness requires that fresh per-instance heartbeat sequence, successful dispatcher lock/recovery, and owner UI listener binding, with optional missing WSL listener degraded. No DB migration/restore or owner dispatch preference overwrite. Persist the caller-supplied observed operation_id/generation/instance in every control envelope; never replace a delayed cancel/retry with the newest snapshot tuple. Snapshot exposes parent operation tuple plus per-request results; return stored exact-duplicate key/payload results before stale checking, conflict on changed payload and reject detectable stale new controls with 409 and preserve later parent rejection after 202. On landing/reconcile obtain the tuple from parent status. Enforce the parent command operation/generation/instance and durable state when closing/reopening; a retired operation cannot reopen a newer drain. Publish ledger_schema (actual non-negative int; null on read error) in child.json and ready payload, bound to fresh challenge/heartbeat and identity. Query failure freezes heartbeat/challenge advancement and makes ready false; mismatch with enrollment/release publishes the actual integer but also freezes successful sequence/challenge advancement and makes ready false. The coordinator supplies verified current schema to check_eligibility; it never substitutes a source constant. Honor migration-disabled startup preflight from lifecycle task #8 before Store/adoption and keep recovery closed until fresh schema readiness. Command probe echo is published only with a successful fresh query.
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

**Interfaces:** Consume ReloadCoordinator.request/set_auto/snapshot/ready. Produce hub.reload_api.router (JSON), .ui_router (forms), and .ready_router (loopback-only readiness). GET /api/v1/hub/reload returns snapshot; POST accepts action=request|retry|cancel, idempotency key and observed operation_id/generation/instance, returns 202 with request/status; PUT /api/v1/hub/reload/auto accepts enabled:boolean, key and the same observed tuple. GET /api/v1/hub/ready returns 200 or 503 from coordinator.ready, exposing readiness identity, ledger_schema and heartbeat/recovery/lock fields; require loopback socket peer and owner UI local port, with no bearer credential. Other reload routes remain owner-only. UI POST /hub/reload and /hub/reload/auto delegate to the same controller and redirect to /health. Extend UiView(..., reload=None).health() to include reload card data; existing callers keep working. Extend Store(db_path, publish=None, *, migrate_db: bool=True, expected_schema: int | None=None); migrate_db=False requires a strict non-negative expected_schema. Each thread-local connection uses that migration policy and must match expected_schema before exposure; no writable connection may enable migration in supervised mode.

- [ ] **Step 1: Pin the failing behavior in `tests/test_reload_controls.py`.** Use a minimal temporary fixture with injected adapters; the assertion sketch below fixes the required behavior (fixture helpers are local to this task's test file). Add the other listed acceptance cases from the JSON as focused tests.

```python
def test_token_cannot_request_reload(self):
    response = client.post('/api/v1/hub/reload', headers={'Authorization':'Bearer tok'}, json={'action':'request','key':'owner-1'})
    self.assertEqual(response.status_code, 403)
    self.assertEqual(request_files(), [])
```

Add `test_delayed_form_cannot_cancel_later_same_sha_drain` and `test_stale_retry_rejected`: render X, advance to Y for B, submit X tuple and assert 409/no current command or barrier reopen; independently vary operation/generation/instance. Missing tuple is 422. Add `test_race_after_202_reports_stale_result`, `test_duplicate_key_payload_conflict`, and `test_ready_schema_mismatch_is_503`; verify the ready route returns actual ledger_schema and operation_id only on a valid report.

- [ ] **Step 1b: Add compatibility regressions.** Add test_supervised_store_never_migrates_on_any_thread: construct with migrate_db=False/expected_schema=4, access conn on construction and two later threads, close/reopen on its owning thread, and spy that every ledger connect received False and migrate was never called. Change actual schema to 3 between preflight and a new connection: assert closed/refused before any mutation, schema remains 3, no migration. Reject missing/bool expected_schema; verify unsupervised Store keeps its legacy default.

- [ ] **Step 2: Run `python -m unittest tests.test_reload_controls -v`.** Expected initially: failure for missing interface/behavior, with no real machine mutation. Record the observed failure; do not accept a dependency-related skip as red.
- [ ] **Step 3: Implement the interfaces in this task's owned files.** Use existing owner identity and CSRF gates; a bearer token cannot request, retry, cancel, toggle auto or forge hub.reload events. Add event/setting prefix reservations in hub/api.py; reject all generic writes to reload-owned settings even for owner; dedicated auto control enqueues set_auto and returns 202, with parent-authoritative state mirrored into ledger. Surface current SHA, pending candidate, auto flag, phase, blocker, last error/time and supported start command when unsupervised. Include Reload now, Retry and Cancel drain only where allowed; no force control. Keep /api/v1/health backward compatible as liveness, separate from ready. On a changed instance UUID after SSE reconnect fetch fresh status and resync current page regardless of old Last-Event-ID; do not wait for higher SSE IDs. Current owner checks: hub/api.py:188-199,306-312,379-389; existing health template: hub/templates/health.html:4-25; memory SSE IDs: hub/events.py:29-54. GET/snapshot exposes the current operation tuple and durable request outcomes. Include hidden operation_id/generation/instance in forms and retain their original values across enqueue; cancellation/retry with missing identity returns 422, a well-formed stale tuple returns 409 stale_control without action, and a post-202 race surfaces the parent rejection. Never auto-refresh and resubmit a control against another operation. Check stored key/payload/results before current tuple validation so exact duplicates preserve their original results; same key/different payload returns 409. Readiness must include actual ledger_schema and operation_id, never fabricate a version from enrollment or source. Implement Store migration policy in hub/store.py: keep unsupervised defaults compatible; supervised callers explicitly pass migrate_db=False and expected_schema. Forward migrate_db=False to ledger connect on construction and every later/reopened thread-local connection, verify schema_version against expected_schema before storing/exposing that connection, close on mismatch/read error, and never retry with migration enabled. This guard prevents post-preflight schema drift from triggering an implicit migration (current behavior: hub/store.py:49-66). Use tests/test_reload_controls.py for focused Store policy/thread coverage as well as route tests; no new overlapping file scope.
- [ ] **Step 4: Run `python -m unittest tests.test_reload_controls -v` again.** Expected: all focused cases pass with no skipped platform-independent tests. Check existing callers retain compatible defaults. For platform-specific cases record the platform and actual result separately.
- [ ] **Step 5: Review scope and hand off.** Run `git diff --check` and inspect changed paths against this task's JSON scope. Codex reports ready_to_commit and a one-sentence commit message without staging/committing; Drover makes the commit and gates the exact SHA. Do not call the task READY without the exact-SHA passing receipt.

### Task #8: Wire supervised startup, API mutation fencing and complete graceful exit

**Size/risk:** M/high. **Dependencies:** #0, #3, #4, #5, #6, #7.

**Files:**

- Modify: `hub/__main__.py`
- Modify: `hub/app.py`
- Modify: `hub/settings.py`
- Modify: `hub/task_views.py`
- Create: `tests/test_reload_lifecycle.py`

**Interfaces:** Consume ReloadCoordinator, reload_api router/ui_router/ready_router, shared ActivityBarrier, dispatcher/services/watchdog extensions. Extend Settings with optional enrollment, runtime_sha, operation_id, generation, instance and supervisor-control paths, all absent for legacy tests. Extend create_app with injectable reload coordinator/activity factory and dispatcher_lock=None (an already-held FileLock passed to DispatcherService without reacquiring). Extend serve(app, settings, discover=..., stop_requested=None) to stop UI server, API listener and adapter loop together; main(argv=None) returns 75 only after proven drained supervised shutdown, 0 for owner stop, nonzero startup failure. app.state.shutdown() -> bool reports all threads stopped, without closing store before that proof. Produce hub.__main__.probe_ledger(db_path: Path, *, expected_schema: int | None=None) -> dict[str,int] returning ledger_schema and required_schema only on equality. Derive required_schema independently as max((version for version, _ in drover.ledger.schema.MIGRATIONS), default=0) from the running immutable release. Add probe-only CLI -m hub --schema-preflight --ledger <absolute-path>; it acquires the existing native dispatcher lock, calls the helper without an enrolled expectation, prints only that JSON on success, releases the lock and exits 0, never constructs Store/app or starts listeners/services. Error exits nonzero without a success record. Enrollment consumes this mode before publishing state.

Extend `RepoStates.stop(timeout: float=30) -> bool`: atomically stop new submissions (later `get` returns cached data only), cancel queued probes, wait within the timeout for running futures, then join the executor and return true. On timeout return false, retain stopped admission and allow a later stop to finish. Keep existing `close()` callers compatible. Current `close()` does not wait (`hub/task_views.py:130-154`); the supervised lifecycle must call `stop` and include its result in shutdown proof.

- [ ] **Step 1: Pin the failing behavior in `tests/test_reload_lifecycle.py`.** Use a minimal temporary fixture with injected adapters; the assertion sketch below fixes the required behavior (fixture helpers are local to this task's test file). Add the other listed acceptance cases from the JSON as focused tests.

```python
def test_candidate_api_claim_is_fenced(self):
    response = client.post('/api/v1/tasks/claim', headers=owner_headers, json={})
    self.assertEqual(response.status_code, 503)
    self.assertEqual(response.headers['Retry-After'], '5')
    self.assertEqual(claimed_task_ids(), [])
```

Add `test_schema_preflight_before_store_in_every_supervised_mode`: baseline/candidate/crash/fallback with mismatched actual schema or read failure never calls Store, migrate or adoption, and leaves the ledger unchanged; matching schema reaches recovery-only startup. Add `test_stale_operation_command_cannot_reopen`: matching generation/instance with wrong operation still cannot cancel the current barrier.

Add `test_probe_pool_blocks_shutdown_until_idle`: hold a probe with an event, request stop with a short injected deadline, assert false and no further submissions from `get`; release it and assert a later stop joins the pool and permits shutdown. Queued probes cancel without execution; store closure and exit 75 wait for this proof. No process-name kills or timing sleeps.

- [ ] **Step 1b: Add compatibility regressions.** Add lifecycle tests for all four supervised modes and probe-only CLI: missing parent/file stays absent; empty/non-SQLite/older/newer/unreadable ledgers and read errors refuse before Store/adoption. A schema-3 ledger and enrollment/manifest both claiming 3 still fail against code requiring 4. Snapshot existing database bytes, schema rows and journal mode before failed read-only probes and assert unchanged; DELETE-mode fixture stays DELETE, no WAL/SHM is introduced there. WAL fixture with committed schema changes visible only in WAL must report the committed version, never ignore WAL through immutable=1. Mock ledger connect/migrate to fail if used by preflight; spy read-only URI/uri=True. On compatible success, assert Store receives migrate_db=False/expected_schema=4; probe-only success returns separate actual/required values without app construction/listeners. Paths with spaces and URI-reserved characters are encoded by as_uri, not interpolated.

- [ ] **Step 2: Run `python -m unittest tests.test_reload_lifecycle -v`.** Expected initially: failure for missing interface/behavior, with no real machine mutation. Record the observed failure; do not accept a dependency-related skip as red.
- [ ] **Step 3: Implement the interfaces in this task's owned files.** Validate stable enrollment/control version and generation before Store opens/migrates; lock candidate admission before app creation. Build one coordinator/barrier shared by dispatch, services and routers, pass WSL live demand to watchdog and reload status to UiView. Count admitted HTTP mutation requests in the same barrier before routing; once closed, return 503/Retry-After:5 for new mutations except reload control/status and owner dispatch preference changes, with permitted controls admitted through activity.try_enter("api-control", control=True) and counted to finish before final exit. At stopping call activity.seal(generation). After drained/stopping, deny all new mutations including permitted controls and wait for existing controls to finish before exit. Existing reads/liveness remain available. Set UI-bound readiness only after actual bind. Start candidate recovery-only, keep services parked until commit, and never change dispatch_enabled as part of reload. Coordinate parent command to stop both listeners (including missing/changed WSL adapter) and join all background threads/close connections, release lock only after landing and dispatch exit. Direct -m hub remains diagnostic and advertises reload unavailable. Main references: hub/__main__.py:43-49,76-125; hub/app.py:16-54,60-70. After protocol/identity validation, supervised main acquires the existing dispatcher FileLock before Store construction, retaining ownership through preflight and passing it via create_app to DispatcherService; on pre-app failure it releases that exact lock. Before Store construction, independently derive code_required_schema from this release's MIGRATIONS and require enrollment.ledger_schema == enrollment.required_schema == manifest.ledger_schema == manifest.required_schema == code_required_schema (strict integers, not booleans). Call probe_ledger with that expected_schema. Its only database open is sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True, isolation_level=None, timeout=10); read schema_version and require actual == code_required_schema, closing in finally. Do not call ledger connect for preflight: it creates parent/database and enables WAL even when migrate_db=False (drover/ledger/db.py:26-42). No mkdir, migration, DDL/DML, journal_mode setting, immutable=1 or writable fallback. Read committed WAL normally or refuse. A missing file/parent stays absent; failure preserves database contents/schema/journal mode. All supervised modes (baseline, candidate, crash recovery and fallback) take this path; mismatch/read failure exits before Store, adoption, writes or readiness. Then create_app must pass Store(..., migrate_db=False, expected_schema=code_required_schema), including when constructing recovery/fallback apps; #7 owns that constructor's per-thread enforcement. Never let a later connection migrate after a successful probe. Reject partially populated supervised settings instead of downgrading to legacy defaults. Pass startup operation_id and expected schema to coordinator/settings. Validate operation_id on every parent command, permit only the newer drain tuple named by durable parent state for this instance, and preserve submitted tuples for API controls during drain.
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

**Interfaces:** Consume standalone bootstrap supervisor.py/runtime.py/reload_protocol.py/reload_machine.py and enrollment protocol. Produce ./hub/scripts/Start-DroverHub.ps1 [-Status|-Stop], resolving source root from PSScriptRoot in source mode or enrollment in installed mode, with registered config/venv. Existing Install-DroverHub.ps1 explicitly enrolls a stopped clean baseline, copies all four bootstrap peers into state/hub-runtime/bootstrap/v1, and also freezes the wrapper and registers DroverHub to invoke that installed copy. Compatibility digests cover the wrapper and both installer scripts. Keep installer parameters backward compatible. Enrollment fixes native paths, interpreter identity, ledger_schema and required_schema from the immutable baseline release's successful read-only probe-only CLI and Git identity without installing on start. Only accept equal strict integer values independently checked by that release; a schema-3 ledger for code requiring 4 must refuse enrollment, never record 3 as the release requirement.

- [ ] **Step 1: Pin the failing behavior in `tests/powershell/test_hub_launcher.ps1`.** Use a minimal temporary fixture with injected adapters; the assertion sketch below fixes the required behavior (fixture helpers are local to this task's test file). Add the other listed acceptance cases from the JSON as focused tests.

```powershell
# PowerShell harness uses fake Start-Process/Register-ScheduledTask adapters.
# Assert checkout path with spaces is one argument and scheduled/manual targets agree.
# Assert no adapter observes package installation, elevation, or a tree-kill request.
```

Add harness assertions that `-Stop` preserves the observed operation/generation/instance and reports stale_control rather than rewriting its request for a new operation, and baseline enrollment records both actual and independently release-derived required schema from the read-only probe. Failed probes publish no bootstrap/enrollment and never retarget the scheduled task.

- [ ] **Step 1b: Add compatibility regressions.** PowerShell harness must reject schema-3/required-4 probe output, nonzero exit, malformed/missing/bool fields, missing ledger/parent and older/newer/unreadable ledger failures with zero enrollment/bootstrap publication or scheduled-task changes. Accept matching actual=required=4 and persist both fields; assert invocation uses the pinned immutable release, argv array and read-only probe-only CLI, never Store or ledger connect. Existing enrollment and scheduled-task adapters remain untouched on every failed probe.

- [ ] **Step 2: Run `powershell.exe -NoProfile -File tests/powershell/test_hub_launcher.ps1`.** Expected initially: failure for missing interface/behavior, with no real machine mutation. Record the observed failure; do not accept a dependency-related skip as red.
- [ ] **Step 3: Implement the interfaces in this task's owned files.** Default wrapper starts one foreground supervisor or returns existing status; Status works without HTTP and Stop requests safe drain/no restart. Pass arguments as arrays with correct quoting for scheduled-task paths; no hand-written uv/PYTHONPATH command, execution-policy bypass, automatic package install, elevation or process-name kill. Refuse legacy/unknown running dispatcher instead of stopping it. Honor persistent failure latch. Existing Install-Drover.ps1 may retain its explicit owner-requested setup sync but print the new supported start command. Owner-only state ACL applies to bootstrap/control/release data. Document migration, auto-off first run, successful manual test before enabling auto, standalone CLI coordination rule, same-schema-only rollback with main unchanged, recovery/latch reset by stopped owner repair and safe release retention. Update both README entrypoints. Existing installer/start evidence: hub/scripts/Install-DroverHub.ps1:13-25, hub/scripts/Install-Drover.ps1:88-112, README.md:24-28. Status includes the operation_id/generation/child.instance tuple and per-request result. Stop reads and preserves that observed tuple with its idempotency key; a stale rejection reports refresh/retry guidance, never silently retargets a later attempt. Export the clean baseline into a validated temporary immutable archive first (same archive path/type/size rules as #1); do not finalize its manifest/release or publish bootstrap/enrollment/active baseline or register/retarget the scheduled task until the probe succeeds. Invoke that immutable baseline with the installed interpreter, -m hub --schema-preflight --ledger <pinned-path> (argv array, release-only import root/CWD). Its native lock excludes a running dispatcher. Require exit 0 and strict integer ledger_schema == required_schema from the release-derived MIGRATIONS requirement; refuse malformed/missing fields, booleans, unequal fields or nonzero exit without altering an existing enrollment/scheduled task. Record both values and both manifest fields only after success; never derive the requirement from the observed ledger, a constant or a default. Enrollment must not create/initialize/migrate a missing or incompatible ledger. Document separate explicit legacy owner setup/migration before retrying enrollment. Document fallback validation against retained A with source still B and mandatory fresh child schema preflight/readiness.
- [ ] **Step 4: Run `powershell.exe -NoProfile -File tests/powershell/test_hub_launcher.ps1` again.** Expected: all focused cases pass with no skipped platform-independent tests. Check existing callers retain compatible defaults. For platform-specific cases record the platform and actual result separately.
- [ ] **Step 5: Review scope and hand off.** Run `git diff --check` and inspect changed paths against this task's JSON scope. Codex reports ready_to_commit and a one-sentence commit message without staging/committing; Drover makes the commit and gates the exact SHA. Do not call the task READY without the exact-SHA passing receipt.

### Task #10: Test detached-runner reporting in breakaway and fallback environments

**Size/risk:** S/low. **Dependencies:** none.

This task can run alongside #0; it owns no reload implementation files.

**Files:**

- Modify: `tests/test_runner.py`

**Interfaces:** Exercise the existing `drover.runner.detach(run_id, runs_dir, child_args, wait_s=DETACH_WAIT_S, nonce=None)` API. Do not change `drover/runner.py`. Main `drover/runner.py:486-527` reports true on its Windows fallback; `tests/test_runner.py:354-365` always requires false. The supplied Windows gate failed at that assertion; the OS-level cause of the first spawn's `OSError` has not been established. The JSON goal supplies the full access header and constraints.

- [ ] **Step 1: Capture the existing focused result and add deterministic branch tests.** Run `python -m unittest tests.test_runner.DetachTests -v` in the preinstalled environment and record platform/results. Add `test_windows_breakaway_success_reports_false`, `test_windows_breakaway_fallback_reports_true`, and `test_windows_fallback_spawn_failure_propagates`. Patch `IS_WINDOWS`, Windows constants (`create=True` where absent), `Popen`, status reads and clock with stdlib mocks; use temporary files, no real Windows process in mocked cases. Assert successful first spawn includes `CREATE_BREAKAWAY_FROM_JOB`, returns false and has no fallback environment marker. Force one first-spawn `OSError`; assert exactly one retry without that flag, `DROVER_DETACHED_IN_JOB=1`, and true reporting. Force the retry to raise and assert that error propagates. These tests may pass immediately because they pin supported current behavior; do not invent a red result.
- [ ] **Step 2: Repair the smoke test's environmental assumption.** Retain its prompt-return, eventual output, completed status and matching runner-PID assertions. Remove inherited `DROVER_DETACHED_IN_JOB` from the test environment. Require false on POSIX; on Windows require a boolean flag equal to final runner status. True describes an unsafe fallback and never satisfies reload survival. Keep all deterministic flag assertions; no blanket skip, unconditional flag override, production change, elevation or job manipulation. If actual reporting is inconsistent, stop with the production scope blocker.
- [ ] **Step 3: Verify both environments.** Run `python -m unittest tests.test_runner.DetachTests -v` on POSIX and on an authorized Windows worker under the failing gate's job conditions. All deterministic tests execute on both platforms. Preserve command/platform/results; a POSIX pass is not a Windows result. Task #11 additionally proves actual survival in a breakaway-capable Windows environment; a restricted environment must block reload with in-job workers. The full assembled exact-SHA Windows gate must pass before READY.
- [ ] **Step 4: Review scope and hand off.** Run `git diff --check` and inspect that only `tests/test_runner.py` changed. Codex reports ready_to_commit without staging/committing; Drover commits and gates the exact SHA. Do not call the existing research gate fixed from this plan alone.

### Task #11: Exercise successful replacement and rollback with detached runs

**Size/risk:** M/high. **Dependencies:** #3, #4, #5, #6, #7, #8, #9, #10.

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

Extend `test_completion_during_rollback_is_ingested_once` to assert source HEAD B is accepted for retained A and a fresh matching ledger_schema is mandatory before A's readiness. Add `test_delayed_controls_across_same_sha_retry_and_restart`: X controls never affect Y admission, including after replay; capture the durable stale_control result. Add `test_schema_mismatch_stops_before_store_and_commit` and retained manifest/interpreter refusal variants.

- [ ] **Step 1b: Add compatibility regressions.** Integrate required-schema refusal: immutable code requiring 4 cannot enroll/start with schema 3, even if enrollment/manifest claim 3; absent ledgers remain absent, incompatible ledger contents/schema/journal mode stay unchanged. Valid enrollment persists actual=required; baseline/candidate/crash/fallback Store connections all disable migration, including later-thread opens. These tests run with disposable state and never repair a ledger as a side effect.

- [ ] **Step 2: Run `python -m unittest tests.test_reload_integration -v`.** Expected initially: failure for missing interface/behavior, with no real machine mutation. Record the observed failure; do not accept a dependency-related skip as red.
- [ ] **Step 3: Implement the interfaces in this task's owned files.** Build one integrated fixture: enrolled A, recorded self-landed B, safe detached Windows worker plus WSL review, outstanding task gate and a full landing operation. Request reload, prove no replacement until gate receipt and whole landing finish, complete an existing worker during outage, then validate B and commit once with preserved run IDs. Next exercise import failure B/fallback A, stuck validation child, failing fallback and supervisor restart from interrupted durable transitions. Assert main is never reset, landed tasks stay landed, no ledger restore occurs, adoption/reaping happens exactly once and failed latch prevents endless restart. Include owner changing dispatch preference while draining and optional WSL listener unavailable. Tests must fail clearly, rather than silently claiming Windows behavior from a skipped run. In failed-B/fallback-A and unexpected committed-child exit scenarios, keep source at B, then separately at C/dirty, assert retained check_recovery starts A and never calls candidate eligibility or resets source. Corrupt retained manifest/schema/dependencies or change interpreter to assert refusal. Inject actual schema mismatch/read error before Store and stale/missing child ledger_schema during readiness: no commit, adoption before preflight or admission. Inject delayed cancel/retry targeting the observed operation_id/generation/instance from X after retry creates Y for the same target B, also across parent restart; assert Y remains fenced and durable rejection is visible. Cover expired handoff proof choosing retained recovery and exact duplicate controls returning the prior result without effects.
- [ ] **Step 4: Run `python -m unittest tests.test_reload_integration -v` again.** Expected: all focused cases pass with no skipped platform-independent tests. Check existing callers retain compatible defaults. For platform-specific cases record the platform and actual result separately.
- [ ] **Step 5: Review scope and hand off.** Run `git diff --check` and inspect changed paths against this task's JSON scope. Codex reports ready_to_commit and a one-sentence commit message without staging/committing; Drover makes the commit and gates the exact SHA. Do not call the task READY without the exact-SHA passing receipt.

## Final verification and rollout handoff

- [ ] Validate the task JSON before import: required fields/types, each goal header, only S/M sizes and low/high risks, earlier-index deps, and no pair of scope paths overlapping by equality or directory prefix.
- [ ] Resolve the supplied Windows detach gate failure through separately scoped #10 and rerun the full gate on the exact SHA. Keep truthful in-job reporting and #4's blocker. This docs-only research revision cannot repair `tests/test_runner.py` or supply a Windows pass; its targeted Linux result only verifies the POSIX path.
- [ ] On the assembled implementation, run `python -m unittest discover -s tests -t .` in the preinstalled hub test environment. Expected: full suite passes; new platform-independent reload tests execute with zero skips. Report Windows-only results separately; a Linux mock run does not establish Windows job survival.
- [ ] Obtain the required authorized Windows receipt: one Windows detached worker and one WSL detached review survive successful replacement and fallback with unchanged IDs; main stays landed; an unknown/in-job runner blocks. All test state is disposable and no live service is started.
- [ ] Review every requirement in the design against #0–#11 and every interface name against its consumer; verify required_schema is independently established at enrollment and migration stays disabled on every supervised Store connection. Treat missing ownership as a plan revision; a worker must not expand scope to fix it.
- [ ] After exact-SHA implementation gates/review, present the owner installation/migration rehearsal from #9. Installation, scheduled-task changes, enabling auto and live operation are explicit later owner actions. This research task performs none of them.

Research verification is limited to documentary consistency, source references, JSON validity/dependency/scope checks and docs-only changes; it is not an implementation gate receipt. Recommendations are pending owner review. The externally assigned research branch is retained; requested T-10 naming belongs to the orchestrator.
