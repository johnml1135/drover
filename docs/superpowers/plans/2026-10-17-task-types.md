# Configurable Task Types Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Drover task isolation/admission and worker constraints take precedence. Steps use checkbox syntax for tracking; Codex workers do not git add/commit.

**Goal:** One versioned declarative type contract drives tasks, lifecycles, agent execution and owner actions across the website and CLI.

**Architecture:** A hub-loaded YAML codec resolves trusted layers into immutable JSON snapshots. A constrained ledger kernel enforces approval, phases, receipts and reservations; shared action/form services serve web, CLI and grill rounds. Existing T-7 helpers become consumers, while repository adapters bind framework artifacts and imports.

**Tech Stack:** Python, SQLite, existing FastAPI/Jinja hub, stdlib worker/client modules; proposed hub-only PyYAML subject to D1 owner approval.

**Spec:** [2026-10-17-task-types-design.md](../specs/2026-10-17-task-types-design.md)

## Global Constraints

- No dependency is installed by this spec task; D1 approval is required before dependency implementation.
- Nothing loads configuration from a worker checkout.
- Source layers, parent hashes, framework mapping and lifecycle content contribute to canonical sorted JSON SHA-256 `type_version`; comments and YAML formatting do not.
- Only owner actions set approval; entering eligible requires current approval.
- Owner tasks never consume worker seats.
- No repo change reaches completed without queue-recorded landing of the exact gated/reviewed revision.
- Type-specific values live in `fields` JSON, never collide with core names.
- `compact_at_tokens: null` is mandatory while E5's no-compact rule applies.
- Drover neither vendors nor installs skills.
- Framework task lists import as proposed only.
- Each worker edits only its scope in its isolated task branch/worktree; never push/merge/deploy or install/start services. Check disk/admission before heavy verification and never add parallel builds. Codex returns ready_to_commit without git add/commit.
- Reconcile landed T-7 before implementation rollout. Use the exact next migration number then; retain kind/write/read_access and links while removing their closed type enum. Main/T-7 behavior references are the spec evidence table E1–E17.
- Keep `task_types.enabled=false` until compatibility/runtime checks pass and the owner activates. Recommendations D1–D11 are rollout decisions, not implicit approval from this research run.

## Review Focus

- A same-name extension, parent deletion or malformed reload must not change an old run; #2/#7 test inheritance and immutable replay.
- Renamed states or waiting-owner work must retain reservations and delivery evidence; artifact records must survive trusted recovery while repository changes retain SHA requirements; #8/#15/#20/#21 test claim/restart/parsing/landing.
- Shared bearer, supplied run IDs, forged UI headers or mirrored-WSL locality must not confer run/owner identity; #16/#17/#20/#22/#24/#25/#29 test issuance/delivery/revocation, cross-run rejection, upload races and owner pairing.
- Two channels or crash retries must not duplicate a grill round, answer or launch; #9/#18/#19/#27 test CAS, idempotency and draft recovery.
- Framework ambiguity, anonymous task rewording or checked boxes must not auto-approve or prematurely archive; #11/#12/#29 test conservative mapping/finalization.

## Delivery boundaries and execution order

The companion [task list](2026-10-17-task-types.tasks.json) is a JSON array in hub task format. Index labels below are zero-based and match `#n` dependency strings. Import remains proposed; the owner can review and activate each slice. Each JSON goal includes its own scope/access/worktree header, normative spec, interfaces and exact verification command.

Contracts (#0–#5), persistence/kernel (#6–#10), repo/execution consumers (#11–#21), owner channels (#22–#27), and tuning/rollout (#28–#29) are staged deliverables. Pure contracts/fixtures are useful independently; live configurable dispatch remains disabled until integration. No two task scopes overlap, including test files. Dependencies reflect interface consumption, not file conflicts. For example #4/#5 can run independently after their prerequisites; #11/#13 can run alongside ledger integration when admitted. D1 is an external owner decision: hold #1 and its dependents until approved; do not install to unblock it.

## File and interface map

| Boundary | Files / responsibility | Owner task |
| --- | --- | --- |
| Type contract/codec/registry | `drover/tasktypes/{schema,codec,registry,lifecycle}.py`, JSON-only contracts and hub-only parsing | #0/#1/#2/#4 |
| Standard policy data | `drover/task_types/`, `drover/lifecycles/`, `drover/task_templates/` | #3 |
| Trusted config | `drover/config.py`, `drover/repos.py`, examples; explicit keys survive defaults | #5 |
| Ledger persistence | schema/db, new task_records/workflow/actions modules, facade ops/states/board/CLI | #6/#7/#8/#10 |
| Owner work | `drover/owner_tasks.py`, `drover/owner_actions.py`, shared action handlers | #9 |
| Framework/task sources | framework adapters/finalization, importer and framework_tasks | #11/#12 |
| Execution | type routing, pipeline/access, runner record parser/recovery, run credentials/artifact client, brief/skills/launch, grill, outbox driver, dispatcher/port, landing | #13–#21 |
| Presentation/transport | owner auth, JSON form descriptors, Store/RegistryService, API, web, stdlib CLI | #22–#27 |
| Policy and rollout | tuner, end-to-end tests, operator documentation | #28/#29 |

All signatures use JSON dictionaries defined in #0 unless a connection, port or service object is explicit. Principal = authenticated role/identity/client/source, never an untrusted actor string. ActionReceipt contains task revision, event ID, resulting state/phase, action status/evidence and replay key. Source/path records use repo-relative normalized paths. TypeSnapshot contains resolved data/hash/provenance; TaskRecord includes pinned type hash and framework binding. Services receive the ledger connection and registry explicitly, avoiding import-time YAML or hub dependencies in worker/client modules.

## Tasks

### #0: Define the format-independent type and task contract

**Size/risk:** M/low. **Dependencies:** none. **Grounding:** spec E1, E2, E12.

**Files (exclusive scope):** `drover/tasktypes/__init__.py`, `drover/tasktypes/schema.py`, `tests/test_type_contract.py`.

**Interfaces:** Produce normalize_type(raw: dict) -> dict, validate_task(payload: dict, resolved_type: dict, existing: dict | None = None) -> dict and canonical_hash(value: dict) -> str in drover/tasktypes/schema.py. All inputs/outputs are JSON-compatible dictionaries; validation errors carry path/message pairs.

**Implementation:** Implement schema_version=1, the exact sections and field kinds in the spec, strict booleans/numbers, reserved core IDs, placeholders, create-only defaults, acceptance union, patch omitted/null semantics and kind/type alias conflict checks. Define TypedDict contracts TypeSnapshot, Principal, TaskRecord, ActionRequest, ActionReceipt, FormDescriptor and FrameworkBinding in the same module. Include fields_json decoding/encoding contracts, tunable routing allowlist and graph-independent workspace/delivery consistency checks. Normalize followups.propose_requires as destination-type-to-guard-list mapping; reject global lists/wildcards and keys outside allowed, and treat absent keys as no extra guards. Canonical hashes use sorted compact UTF-8 JSON with finite numbers only.

- [ ] Add failing tests: `test_defaults_and_patch`: false and [] remain explicit values; omitted defaults materialize once; null cannot delete a nonnullable required value; `test_aliases_and_unknowns`: conflicting kind/type, reserved custom IDs, unknown keys, bool-as-int and NaN fail with stable field paths; `test_hash_and_placeholders`: key order does not change hash; semantic field change does; unknown placeholders fail; follow-up guard mappings accept implementation:[grill-complete] with grill/docs:[], reject global arrays and keys outside allowed.
- [ ] Run `python3 -m unittest tests.test_type_contract -v`; confirm the new assertion fails for the absent behavior, not an environment/import setup error.
- [ ] Implement the interfaces and behavior above, keeping later consumers behind the activation flag.
- [ ] Rerun `python3 -m unittest tests.test_type_contract -v`; require exit 0 and all assertions passing.
- [ ] Inspect scope/diff and return ready_to_commit with evidence and a one-sentence subject; no git add/commit on Codex.

### #1: Add the bounded YAML codec and approved dependency declaration

**Size/risk:** M/low. **Dependencies:** #0. **Grounding:** spec E6, E11.

**Files (exclusive scope):** `drover/tasktypes/codec.py`, `hub/pyproject.toml`, `hub/uv.lock`, `tests/test_type_yaml.py`.

**Interfaces:** Consume normalize_type/canonical_hash from #0. Produce decode_yaml(text: str) -> dict in drover/tasktypes/codec.py; import PyYAML lazily inside this hub-only module.

**Implementation:** Before dependency edits, require the owner to approve design decision D1; report blocked if that approval is absent at execution. After approval, declare and lock an owner-approved PyYAML version in the hub environment using the established lock workflow without installing packages. Use a constrained SafeLoader: one mapping document, UTF-8 <=256 KiB, depth <=32, <=1000 combined field/state definitions, JSON scalar resolution. Reject duplicate keys/tags/anchors/aliases/merge keys/nonfinite values. Missing parser raises an actionable hub-environment error; importing other drover/tasktypes modules must not import yaml. Never vendor or handwrite a YAML parser.

- [ ] Add failing tests: `test_yaml_limits`: duplicate keys, alias expansion, tags, multi-doc input, excessive nesting and >256 KiB input fail before registry activation; `test_yaml_scalar_rules`: true/false are booleans; on/yes/date remain strings; non-string keys and nonfinite values fail; `test_parser_free_import`: subprocess with yaml unavailable imports worker/client contracts successfully; codec call explains missing hub dependency.
- [ ] Run `uv run --project hub --no-sync python -m unittest tests.test_type_yaml -v`; confirm the new assertion fails for the absent behavior, not an environment/import setup error.
- [ ] Implement the interfaces and behavior above, keeping later consumers behind the activation flag.
- [ ] Rerun `uv run --project hub --no-sync python -m unittest tests.test_type_yaml -v`; require exit 0 and all assertions passing.
- [ ] Inspect scope/diff and return ready_to_commit with evidence and a one-sentence subject; no git add/commit on Codex.

### #2: Resolve layered type registries into immutable snapshots

**Size/risk:** M/low. **Dependencies:** #0, #1. **Grounding:** spec E6, E12.

**Files (exclusive scope):** `drover/tasktypes/registry.py`, `tests/test_type_registry.py`.

**Interfaces:** Consume decode_yaml/normalize_type/canonical_hash. Produce build_registry(sources: list[dict], context: dict) -> dict, resolve_type(registry: dict, name: str) -> dict, and preview_reload(old: dict, new: dict) -> dict in drover/tasktypes/registry.py. Sources contain layer, path, text and approved repo identity; output includes snapshots, provenance and registry hash.

**Implementation:** Resolve shipped < owner < enabled registered-repo layer, builtin/name and lower/name references, one parent, depth<=8, map merge/array replacement and remove_fields. Reject duplicate names within a layer, cycles, missing parents, ambiguous same-name override and unknown schema versions. Resolve template/model references and embed lifecycle/template content plus framework binding before hashing. Return a complete candidate registry or errors; preview never mutates the active registry. A malformed later reload must preserve the last accepted snapshot.

- [ ] Add failing tests: `test_inheritance_and_precedence`: builtin/spec and lower/spec resolve deterministically; arrays replace; fields remove only explicitly; provenance lists parent hashes; `test_atomic_reload`: broken parent/cycle/duplicate/unknown placeholder returns diagnostics and leaves the previous registry usable; `test_semantic_version`: comment-only changes preserve hash; parent/template/lifecycle/framework changes alter hash.
- [ ] Run `uv run --project hub --no-sync python -m unittest tests.test_type_registry -v`; confirm the new assertion fails for the absent behavior, not an environment/import setup error.
- [ ] Implement the interfaces and behavior above, keeping later consumers behind the activation flag.
- [ ] Rerun `uv run --project hub --no-sync python -m unittest tests.test_type_registry -v`; require exit 0 and all assertions passing.
- [ ] Inspect scope/diff and return ready_to_commit with evidence and a one-sentence subject; no git add/commit on Codex.

### #3: Ship standard type, lifecycle and brief definitions

**Size/risk:** M/low. **Dependencies:** #0, #1, #2. **Grounding:** spec E3, E5, E6, E12.

**Files (exclusive scope):** `drover/task_types/`, `drover/lifecycles/`, `drover/task_templates/`, `tests/test_standard_types.py`.

**Interfaces:** Consume build_registry. Produce YAML research, investigation, spec, implementation, fix, review, docs, mechanical, grill, owner-answer; lifecycle YAML standard-v1, artifact-v1, owner-v1; role brief Markdown templates matching the design.

**Implementation:** Encode every default table row, required/custom fields, skills, routing effort/ladder aliases, acceptance defaults, follow-up allowlists and both form hints. Supply a minimal internal base if useful, mark it abstract so it cannot create tasks. Copy the complete standard transition adjacency from the cited main snapshot, with explicit actor permissions and kernel guards; artifact/owner variants use the spec graphs. Include the full api-spec worked extension and its complete creation payload as an in-memory test fixture, not as a shipped owner file; inherit required problem, collect it in web fields and CLI order, and use destination-specific implementation:[grill-complete], grill:[], docs:[] prerequisites. Deep means configured director model/high on worker admission, not director-seat privilege. Keep compact_at_tokens null.

- [ ] Add failing tests: `test_all_standard_types`: all ten concrete types resolve, have required fields/defaults and match access/gate/review/model choices in the design table; `test_standard_edges`: standard-v1 includes all and only the twelve statuses and edges of the cited main transition table; `test_worked_extension`: the full api-spec payload including title/goal/problem/target and nonempty framework artifact scope validates and materializes additive/[]/read ref; omitting inherited problem fails; both create forms include problem; page shows problem/target/compatibility/evidence and card target+compatibility; implementation guard is grill-complete while grill/docs guards are empty; custom type has no code enum dependency.
- [ ] Run `uv run --project hub --no-sync python -m unittest tests.test_standard_types -v`; confirm the new assertion fails for the absent behavior, not an environment/import setup error.
- [ ] Implement the interfaces and behavior above, keeping later consumers behind the activation flag.
- [ ] Rerun `uv run --project hub --no-sync python -m unittest tests.test_standard_types -v`; require exit 0 and all assertions passing.
- [ ] Inspect scope/diff and return ready_to_commit with evidence and a one-sentence subject; no git add/commit on Codex.

### #4: Validate lifecycles against immutable kernel requirements

**Size/risk:** M/low. **Dependencies:** #0, #3. **Grounding:** spec E3, E7, E8.

**Files (exclusive scope):** `drover/tasktypes/lifecycle.py`, `tests/test_type_lifecycle.py`.

**Interfaces:** Produce validate_lifecycle(document: dict, capabilities: dict) -> dict, phase_of(snapshot: dict, state: str) -> str, allowed_transition(snapshot: dict, state: str, transition_id: str, principal: dict, evidence: dict) -> dict in drover/tasktypes/lifecycle.py. No database or side effects.

**Implementation:** Normalize state phases/outcomes, stable transition IDs, actors, declarative guards and ordered exit/entry actions. Importable lifecycles retain proposed as a pending entry state; entering any pending phase clears approval. Recognize only the design action vocabulary. Enforce approval eligibility, owner-only submit/approval, dispatcher execution authority, graph reachability/terminal exits, required gate/review/landing paths and artifact completion evidence. Verify inherited/replaced graphs including renamed states. Reject bypasses by owner or external-event roles as well as by agents; YAML cannot weaken kernel invariants.

- [ ] Add failing tests: `test_renamed_states`: queued/building/shipped can map to eligible/running/completed with valid mandatory stages; `test_bypass_paths`: replacement edges skipping required gate/review/landing, missing proposed import entry, agent approval, unrecognized actions and unreachable mandatory states fail; `test_owner_and_artifact_graphs`: owner answers need no claim/run; artifact completion needs content hash; cancelled is not successful.
- [ ] Run `python3 -m unittest tests.test_type_lifecycle -v`; confirm the new assertion fails for the absent behavior, not an environment/import setup error.
- [ ] Implement the interfaces and behavior above, keeping later consumers behind the activation flag.
- [ ] Rerun `python3 -m unittest tests.test_type_lifecycle -v`; require exit 0 and all assertions passing.
- [ ] Inspect scope/diff and return ready_to_commit with evidence and a one-sentence subject; no git add/commit on Codex.

### #5: Register trusted type layers and repository framework settings

**Size/risk:** M/low. **Dependencies:** #0, #2. **Grounding:** spec E6, E8.

**Files (exclusive scope):** `drover/config.py`, `drover/repos.py`, `drover.example.toml`, `repos/acme-app.toml`, `tests/test_type_config.py`.

**Interfaces:** Produce registry_sources(cfg: dict, repo: dict | None) -> list[dict] in drover/repos.py; extend normalize/load outputs with task_types and spec_framework dictionaries and explicit_review_keys. Add task_types.enabled=false in global configuration.

**Implementation:** Add trusted shipped/owner/optional repo directory configuration and schema validation, resolving repo layer paths beside registered repo TOML and never from a worker CWD. Preserve explicit reviewer keys separately from filled defaults. Add spec_framework.kind auto|openspec|spec-kit|superpowers|none, optional root/version and safe artifact root validation. Document examples and paths without personal data. Use config home semantics, distinguish missing from malformed files, preserve existing seat/capacity configuration. Activation and registry writes remain owner-only at the later API layer.

- [ ] Add failing tests: `test_trusted_sources`: changing CWD or planting a worker .drover directory does not change type sources; enabled registered repo layer wins only for that repo; `test_framework_override`: invalid framework/root fails; explicit superpowers survives empty layout; ambiguity can be overridden; `test_explicit_review_keys`: normalized fallback model does not mask a type reviewer; explicit repo review model does.
- [ ] Run `python3 -m unittest tests.test_type_config -v`; confirm the new assertion fails for the absent behavior, not an environment/import setup error.
- [ ] Implement the interfaces and behavior above, keeping later consumers behind the activation flag.
- [ ] Rerun `python3 -m unittest tests.test_type_config -v`; require exit 0 and all assertions passing.
- [ ] Inspect scope/diff and return ready_to_commit with evidence and a one-sentence subject; no git add/commit on Codex.

### #6: Add transactional type, workflow and owner-work storage

**Size/risk:** M/high. **Dependencies:** #0, #3. **Grounding:** spec E1, E4, E12, E14.

**Files (exclusive scope):** `drover/ledger/schema.py`, `drover/ledger/db.py`, `tests/test_type_migration.py`.

**Interfaces:** Append the next unused migration after reconciled T-7. Produce type_versions, task type_version/fields_json/assignee/execution_phase/revision/parent_id/result_json/framework binding, run type_version/execution_json, scope_reservations, action_outbox, question_task_map, grill_sessions and grill_rounds as specified. SQL column contracts are documented beside the migration. Also produce run_credentials(id PK, token_hash UNIQUE, task_id FK, run_id FK, launch_nonce, scope, issued_at, expires_at, revoked_at, revoke_reason) and artifact_receipts(id PK, task_id FK, run_id FK, launch_nonce, credential_id FK, sha256, storage_key, request_key, created_at); enforce unique (run_id, launch_nonce, request_key).

**Implementation:** Reconcile landed T-7 first, then append migrations without changing applied entries. Rebuild tasks transactionally to remove fixed kind CHECK while preserving the compatibility column; preserve every ID/FK/index/trigger/link/question/event/sequence. Make type_versions immutable, outbox keys unique by transition event/action index, question mappings unique by question ID and round keys unique by grill/round number. Add constraints for assignee/phase/revision and JSON content; no dynamic snapshots generated in SQL. Provide migration support for legacy metadata to be populated by #7 before dispatch activation. Retain revoked credential metadata referenced by immutable append-only artifact receipts; store no plaintext secret. Existing legacy runs receive no inferred credentials or receipts.

- [ ] Add failing tests: `test_v3_upgrade`: a populated main-v3 fixture upgrades with identical IDs, policy/status/receipt data and append-only event triggers; FK/integrity checks pass; `test_t7_upgrade`: a populated T-7 fixture retains kind/write/read_access/task_links and permits new type names after rebuild; `test_migration_rollback`: injected failure rolls back fully; rerun succeeds; counters/child references/outbox uniqueness remain correct; `test_run_auth_storage`: token hashes are unique; credential/receipt task/run FKs and request-key uniqueness hold; revocation retains receipt references; legacy fixtures gain empty auth tables without fabricated identities.
- [ ] Run `python3 -m unittest tests.test_type_migration -v`; confirm the new assertion fails for the absent behavior, not an environment/import setup error.
- [ ] Implement the interfaces and behavior above, keeping later consumers behind the activation flag.
- [ ] Rerun `python3 -m unittest tests.test_type_migration -v`; require exit 0 and all assertions passing.
- [ ] Inspect scope/diff and return ready_to_commit with evidence and a one-sentence subject; no git add/commit on Codex.

### #7: Persist validated task snapshots and pin execution revisions

**Size/risk:** M/high. **Dependencies:** #0, #2, #4, #6. **Grounding:** spec E1, E2, E11, E12.

**Files (exclusive scope):** `drover/ledger/task_records.py`, `tests/test_type_records.py`.

**Interfaces:** Produce register_types(conn, registry: dict) -> None, create_typed(conn, payload: dict, principal: dict, registry: dict) -> dict, patch_typed(conn, task_id: str, patch: dict, principal: dict, expected_revision: int) -> dict, pin_execution(conn, task_id: str, policy: dict, execution: dict) -> dict, bootstrap_legacy(conn, registry: dict) -> dict in drover/ledger/task_records.py.

**Implementation:** Validate every mutation against its immutable type, save normalized custom fields/core aliases, materialize defaults once and preserve approval on narrowing only. Pending owner upgrade previews semantic/capability diff, revalidates fields and clears approval. Pin run type+policy+effective config before launch; no upgrades mid-run. Bootstrap existing task types and historical runs using explicitly marked legacy-v1 snapshots, not fabricated historical defaults. Mark active legacy runs for draining and expose activation_ready only after settlement. Deleted registry definitions must remain readable from stored snapshots.

- [ ] Add failing tests: `test_snapshot_isolation`: reload/delete type leaves task/run behavior and rendered schema unchanged; explicit pending upgrade changes hash and clears approval; `test_patch_revision_and_access`: stale patch conflicts; null/omitted differ; widening access requires owner and reapproval; worker cannot widen; `test_legacy_bootstrap`: historical run receipts/policy IDs survive, unavailable original config is marked unknown, active legacy lease prevents activation.
- [ ] Run `python3 -m unittest tests.test_type_records -v`; confirm the new assertion fails for the absent behavior, not an environment/import setup error.
- [ ] Implement the interfaces and behavior above, keeping later consumers behind the activation flag.
- [ ] Rerun `python3 -m unittest tests.test_type_records -v`; require exit 0 and all assertions passing.
- [ ] Inspect scope/diff and return ready_to_commit with evidence and a one-sentence subject; no git add/commit on Codex.

### #8: Implement guarded transitions, reservations and durable action intents

**Size/risk:** M/high. **Dependencies:** #4, #6, #7. **Grounding:** spec E3, E4, E7, E8.

**Files (exclusive scope):** `drover/ledger/workflow.py`, `drover/ledger/actions.py`, `tests/test_type_transitions.py`.

**Interfaces:** Produce transition(conn, task_id: str, transition_id: str, principal: dict, evidence: dict, expected_revision: int, request_key: str) -> dict in workflow.py; enqueue_actions(conn, event_id: int, actions: list[dict]) -> None and pending_actions(conn) -> list[dict] in actions.py. Return ActionReceipt dictionaries.

**Implementation:** In one transaction validate saved lifecycle/actor/approval/receipts, CAS task revision, persist state+phase+outcome, append audit event and outbox rows. Exit actions precede entry actions. Maintain durable scope reservations independent of task state; waiting-owner with unlanded work retains scope while process lease can end. Successful completion, not terminal state alone, satisfies blocks; waits-for remains advisory. Reserve/claim atomically with assignee=agent, preserve cycle checks and prevent raw client phase writes. Duplicate request keys return the original receipt, conflicting payloads return 409.

- [ ] Add failing tests: `test_transition_atomicity`: injected outbox failure leaves task/event/reservation unchanged; retry produces one transition and ordered exit/entry intents; `test_waiting_scope`: waiting-owner releases worker lease but keeps write scope; conflicting task cannot claim until safe completion/cancellation cleanup; `test_dependency_success`: answered/artifact-completed/landed success unblock; failed/cancelled do not; concurrent claims get one winner.
- [ ] Run `python3 -m unittest tests.test_type_transitions -v`; confirm the new assertion fails for the absent behavior, not an environment/import setup error.
- [ ] Implement the interfaces and behavior above, keeping later consumers behind the activation flag.
- [ ] Rerun `python3 -m unittest tests.test_type_transitions -v`; require exit 0 and all assertions passing.
- [ ] Inspect scope/diff and return ready_to_commit with evidence and a one-sentence subject; no git add/commit on Codex.

### #9: Promote questions to owner tasks and share owner action handlers

**Size/risk:** M/high. **Dependencies:** #7, #8. **Grounding:** spec E4, E9, E10.

**Files (exclusive scope):** `drover/owner_tasks.py`, `drover/owner_actions.py`, `tests/test_owner_actions.py`.

**Interfaces:** Produce request_owner_task(conn, requester_id: str, questions: list[dict], request_key: str) -> dict and migrate_questions(conn) -> dict in owner_tasks.py. Produce perform_action(conn, principal: dict, target: dict, action: str, payload: dict, expected_revision: int | str, request_key: str) -> dict and action_descriptors(conn, principal: dict, target: dict) -> list[dict] in owner_actions.py; target is {kind,id} for task/epic/hub/repo/garden; controller revision/hash supplies global CAS. Services register bounded handlers for repo/global targets.

**Implementation:** Make owner-answer tasks authoritative with structured numbered questions, stable option IDs, drafts and atomic submit. Create requester->answer blocks plus parent navigation and notification outbox; never add reverse dependency. Migrate old questions one-to-one and synchronize facade reads/writes, preserving answers/targets/options/times and exactly-once resume. Use one handler registry for approve/answer/edit/comment/hold/release/cancel/reorder/type upgrade/reload plus dispatch/epic/import/garden/policy controls through validated adapters. A comment cannot complete structured owner work. Answer removes only its hold and resumes only with current approval and all blockers clear. Cancellation remains a blocker with resolution reason.

- [ ] Add failing tests: `test_question_migration`: rerunning migration creates no duplicate owner task; answered/open historical rows retain semantics; facade answer invokes one action/event; `test_owner_answer_and_holds`: partial drafts do not unblock; final required answers do; other holds and absent approval prevent resume; cancellation does not unblock; `test_action_authority`: worker-supplied owner string cannot approve/answer/reload; two-channel stale submit conflicts; replayed request returns one receipt.
- [ ] Run `python3 -m unittest tests.test_owner_actions -v`; confirm the new assertion fails for the absent behavior, not an environment/import setup error.
- [ ] Implement the interfaces and behavior above, keeping later consumers behind the activation flag.
- [ ] Rerun `python3 -m unittest tests.test_owner_actions -v`; require exit 0 and all assertions passing.
- [ ] Inspect scope/diff and return ready_to_commit with evidence and a one-sentence subject; no git add/commit on Codex.

### #10: Integrate typed operations across the ledger facade and boards

**Size/risk:** M/high. **Dependencies:** #7, #8, #9. **Grounding:** spec E1, E3, E4, E12, E14.

**Files (exclusive scope):** `drover/ledger/ops.py`, `drover/ledger/states.py`, `drover/ledger/board.py`, `drover/ledger/cli.py`, `tests/test_ledger.py`, `tests/test_type_ledger_facade.py`.

**Interfaces:** Consume task_records, workflow and owner_actions. Existing create_task/update_task/approve/set_status/claim_next/start_run/ask_question/answer_question names stay compatible; wrappers pass authenticated service principals and use saved types. Expose lifecycle-aware ready_tasks and state/phase filters.

**Implementation:** Replace fixed-kind/status SQL assumptions with pinned type validation, execution phases, outcome-based dependencies and reservations; exclude owner tasks from claims. Retain standard transition compatibility while rejecting unsupported custom moves; migration of callers must not let actor strings create owner authority. Legacy dispatcher wrappers use an internal trusted principal supplied by the port/store boundary; public API input never supplies it. Route question APIs through owner-task facade. Dynamic board/CLI groups show custom states and completion kinds, retaining core chips and historical event views. Audit every status literal in owned files and explain intentional compatibility cases in tests.

- [ ] Add failing tests: `test_legacy_facade`: existing main and T-7 create/patch/deps/related/approval interfaces remain usable and keep source data; `test_custom_state_claim`: renamed eligible state is claimable, owner task is excluded, and artifact success releases a blocking dependent; `test_board_phases`: custom states appear in board/CLI; success/cancel/failure counts distinguish outcomes; no false landed metrics.
- [ ] Run `python3 -m unittest tests.test_type_ledger_facade -v tests.test_ledger`; confirm the new assertion fails for the absent behavior, not an environment/import setup error.
- [ ] Implement the interfaces and behavior above, keeping later consumers behind the activation flag.
- [ ] Rerun `python3 -m unittest tests.test_type_ledger_facade -v tests.test_ledger`; require exit 0 and all assertions passing.
- [ ] Inspect scope/diff and return ready_to_commit with evidence and a one-sentence subject; no git add/commit on Codex.

### #11: Detect spec frameworks and plan safe artifact finalization

**Size/risk:** S/low. **Dependencies:** #0, #5. **Grounding:** spec E5, E8; external framework sources in design.

**Files (exclusive scope):** `drover/frameworks.py`, `drover/framework_finalize.py`, `tests/test_spec_frameworks.py`.

**Interfaces:** Produce detect_framework(repo_snapshot: str, override: dict | None = None) -> dict and artifact_paths(binding: dict, task: dict) -> dict in frameworks.py. Produce finalization_task(binding: dict, source: dict, completed_tasks: list[dict]) -> dict | None in framework_finalize.py; returns a proposed task payload, never edits a checkout.

**Implementation:** Detect exact OpenSpec, Spec Kit, superpowers markers and none; multiple matches require override, persist detection evidence/SHA and explicit version. Support design/research/plan/task-source paths for every adapter and repo instructions. Explicitly bind Drover to superpowers when configured even on an empty layout. OpenSpec sync/archive is a scoped reviewed finalization commit after all change tasks land; Spec Kit retains artifacts and constitution rules; none uses owner-approved docs/specs/<slug>. Reject traversal/symlink escapes and missing required installed tooling with actionable owner work, never run/install a framework during detection.

- [ ] Add failing tests: `test_detection_and_override`: four layouts detect correctly; mixed markers block without override; explicit superpowers wins in an empty repo; `test_artifact_paths`: Spec Kit NNN-feature and OpenSpec capability paths resolve inside approved root; traversal/symlink escapes fail; `test_archive_is_proposed`: OpenSpec finalization requires all successful change tasks, creates a proposed scoped task and never mutates main.
- [ ] Run `python3 -m unittest tests.test_spec_frameworks -v`; confirm the new assertion fails for the absent behavior, not an environment/import setup error.
- [ ] Implement the interfaces and behavior above, keeping later consumers behind the activation flag.
- [ ] Rerun `python3 -m unittest tests.test_spec_frameworks -v`; require exit 0 and all assertions passing.
- [ ] Inspect scope/diff and return ready_to_commit with evidence and a one-sentence subject; no git add/commit on Codex.

### #12: Import typed framework task lists as proposed work

**Size/risk:** M/low. **Dependencies:** #7, #10, #11. **Grounding:** spec E2, E3, E12.

**Files (exclusive scope):** `drover/importer.py`, `drover/framework_tasks.py`, `tests/test_importer.py`, `tests/test_framework_tasks.py`.

**Interfaces:** Produce parse_framework_tasks(binding: dict, text: str, source: dict) -> list[dict] in framework_tasks.py; source includes repo/framework/change-or-feature ID/path/content hash. Existing run_import accepts typed tasks/fields plus framework source records and uses validate_task/create_typed.

**Implementation:** Recognize Spec Kit stable IDs and phase/[P]/dependency markers, OpenSpec tasks and superpowers tasks.json or stable plan items. Store source provenance and durable anonymous-item mappings; ambiguous moves/rewords become conflicts. Validate complete batches before one transaction commits, preserve fields, kind/type aliases, owner-edit conflicts and index deps. Every imported row remains proposed with no approved_at; checkmarks cannot imply completed or gated. Reject unknown types, contradictory dependencies and overlap flagged as parallel. Source checkboxes are updated only through proposed reconciliation commits after landing; archived items do not become ready.

- [ ] Add failing tests: `test_typed_batch_atomicity`: one unknown type/invalid field rolls back the batch; explicit false/custom fields survive and all imports stay proposed; `test_source_identity`: stable IDs reimport idempotently; owner edits and ambiguous anonymous moves produce conflicts; deletion never cancels live work; `test_framework_dependencies`: phase order/explicit deps survive, invalid cycles fail, [P] with overlapping scopes is reported, checkmarks never create gate evidence.
- [ ] Run `python3 -m unittest tests.test_framework_tasks -v tests.test_importer`; confirm the new assertion fails for the absent behavior, not an environment/import setup error.
- [ ] Implement the interfaces and behavior above, keeping later consumers behind the activation flag.
- [ ] Rerun `python3 -m unittest tests.test_framework_tasks -v tests.test_importer`; require exit 0 and all assertions passing.
- [ ] Inspect scope/diff and return ready_to_commit with evidence and a one-sentence subject; no git add/commit on Codex.

### #13: Apply type routing without bypassing seats and allowance

**Size/risk:** M/low. **Dependencies:** #0, #5, #7. **Grounding:** spec E6, E7, E8.

**Files (exclusive scope):** `drover/route.py`, `drover/task_routing.py`, `tests/test_task_routes.py`.

**Interfaces:** Produce resolve_route_defaults(task: dict, snapshot: dict, role: str, cfg: dict, policy: dict, repo: dict) -> dict in task_routing.py. Extend choose with an optional resolved_candidates argument while preserving existing callers; return seat/backend/model/effort or a structured temporary/permanent refusal.

**Implementation:** Implement global->type->allowed policy overlay->task precedence, with explicit repo reviewer settings between type and task reviewer defaults. Distinguish model auto from a pin, validate candidate/backend/effort combinations, use per-role escalation ladder and every-second-failure progression. Recheck seat/allowance/platform/resource admission on escalation. Temporary quota/seat shortage defers, permanent mismatch requests owner; do not replace pinned model. Preserve director-seat role constraints and Windows dotnet/WSL Codex rules. Resolve aliases from cfg, record chosen policy/type versions and rejection reasons.

- [ ] Add failing tests: `test_routing_precedence`: type defaults win over global, allowed overlay wins over type, explicit task pin wins; explicit repo reviewer wins over type while fallback does not; `test_admission_and_escalation`: no reserve/seat/platform bypass at any rung; temporary shortage defers and permanent mismatch explains needs-owner; `test_pin_and_role`: pinned model is not silently downgraded; spec worker at director model remains ordinary worker admission.
- [ ] Run `python3 -m unittest tests.test_task_routes -v`; confirm the new assertion fails for the absent behavior, not an environment/import setup error.
- [ ] Implement the interfaces and behavior above, keeping later consumers behind the activation flag.
- [ ] Rerun `python3 -m unittest tests.test_task_routes -v`; require exit 0 and all assertions passing.
- [ ] Inspect scope/diff and return ready_to_commit with evidence and a one-sentence subject; no git add/commit on Codex.

### #14: Apply typed access, worktree and gate policies to the pipeline

**Size/risk:** M/high. **Dependencies:** #5, #7, #8, #13. **Grounding:** spec E5, E7, E8, E12.

**Files (exclusive scope):** `drover/pipeline.py`, `drover/task_access.py`, `drover/task_context.py`, `tests/test_pipeline.py`, `tests/test_type_pipeline.py`.

**Interfaces:** Consume task snapshot capabilities and FrameworkBinding. Extend Pipeline.worktree/gate_plan/commit_ready to use resolved workspace/delivery policy; retain T-7 read preparation/verification API. task_context.check_kind becomes registry-backed compatibility validation without a fixed enum.

**Implementation:** Support new/existing/none worktree and branch policies; existing requires explicit clean/owned/adopted task isolation and never main. Resolve refs to immutable read snapshots and enforce sandbox ceiling; write=false cannot edit repo files. Spec-only restricts commits to approved framework/glossary/ADR artifacts, no-repo-change cannot commit. Compute gate maximum(type, explicit repo minimum, high-risk floor), scoped fallback and exact-SHA docs-only exemption evidence; actual code cannot request gate none or skip independent review. Do not change T-7 snapshot ownership and fail-closed verification semantics.

- [ ] Add failing tests: `test_workspace_policies`: none creates no worktree; existing refuses main/dirty-unowned; new is isolated; write with empty scope fails; `test_spec_and_read_access`: spec-only out-of-artifact diff fails before index changes; immutable read ref changes trigger verification refusal; `test_gate_floor`: code none is rejected, ambiguous scoped becomes full, high risk forces full, approved docs-only exemption records exact SHA.
- [ ] Run `python3 -m unittest tests.test_type_pipeline -v tests.test_pipeline`; confirm the new assertion fails for the absent behavior, not an environment/import setup error.
- [ ] Implement the interfaces and behavior above, keeping later consumers behind the activation flag.
- [ ] Rerun `python3 -m unittest tests.test_type_pipeline -v tests.test_pipeline`; require exit 0 and all assertions passing.
- [ ] Inspect scope/diff and return ready_to_commit with evidence and a one-sentence subject; no git add/commit on Codex.

### #15: Preserve artifact completion records through runner parsing and recovery

**Size/risk:** M/low. **Dependencies:** #0. **Grounding:** spec E16.

**Files (exclusive scope):** `drover/runner.py`, `tests/test_runner.py`, `tests/test_launch_runner.py`, `tests/test_artifact_records.py`.

**Interfaces:** Consume the format-independent completion protocol in the design; preserve runner._record_from(obj), parse_action_record(text) and scan_record(out_path) signatures. Produce normalized done records with artifact:{uri,sha256} retained through record.json and launch.RunnerHandle._record/_finish; launch.parse_action_record remains the shared alias, with no second parser.

**Implementation:** Extend runner._record_from to accept done with empty SHA only when artifact has a normalized hub:tasks/<task-id>/result.md URI and 64 lowercase hex sha256. SHA and artifact are mutually exclusive; malformed/ambiguous artifact records become blocked/invalid-artifact, and absent SHA plus absent artifact retains blocked/no-sha. Preserve existing repo done-with-SHA, ready_to_commit/commit_message and blocked behavior, scan bounds and trusted final assistant/result frames. Persist normalized metadata unchanged in record.json and verify launch revalidation and restart/adoption preserve it without editing launch.py. Parsing never grants delivery permission or claims persisted content: dispatcher consumes the run-authenticated artifact receipt and refuses artifact-only completion for repo-changing policies. Keep stdlib-only imports and existing no-record/rotation behavior.

- [ ] Add failing tests: `test_artifact_record_roundtrip`: done with empty SHA and valid artifact survives parse, final Claude/Codex frame scanning, atomic record.json persistence and launch.RunnerHandle._record/_finish revalidation with URI/hash unchanged; `test_artifact_recovery`: restart/adopt an exited fake runner with matching run identity and saved record; poll returns the same done/artifact without a second launch; launch.parse_action_record is runner.parse_action_record; `test_record_authority_and_legacy`: raw/tool artifact success is never trusted; missing SHA/artifact remains blocked/no-sha; malformed URI/hash or SHA+artifact is blocked/invalid-artifact; legacy done+SHA and ready_to_commit requirements remain intact.
- [ ] Run `python3 -m unittest tests.test_artifact_records tests.test_runner tests.test_launch_runner -v`; confirm the new assertion fails for the absent behavior, not an environment/import setup error.
- [ ] Implement the interfaces and behavior above; leave launch.py to #17 and delivery authorization to #20.
- [ ] Rerun `python3 -m unittest tests.test_artifact_records tests.test_runner tests.test_launch_runner -v`; require exit 0 and all assertions passing.
- [ ] Inspect scope/diff and return ready_to_commit with evidence and a one-sentence subject; no git add/commit on Codex.

### #16: Authenticate artifact uploads with revocable assigned-run credentials

**Size/risk:** M/high. **Dependencies:** #0, #6, #7. **Grounding:** spec E9, E17.

**Files (exclusive scope):** `drover/run_auth.py`, `drover/artifact_client.py`, `tests/test_run_auth.py`, `tests/test_artifact_client.py`.

**Interfaces:** Produce issue_run_credential(conn, run_id: int, launch_nonce: str, wall_deadline: str | None, now: str) -> dict, verify_run_credential(conn, token: str, task_id: str, run_id: int, now: str) -> dict, revalidate_run_principal(conn, principal: dict, now: str) -> None and revoke_run_credentials(conn, run_id: int, reason: str, now: str) -> None in drover/run_auth.py. RunAuthError.code is unauthorized (401) or forbidden (403). Issuance returns credential_id/token/expires_at/task_id/run_id/launch_nonce; verification returns the authenticated assigned-run Principal, never a caller-supplied actor. Produce upload_result(credential_file: str, result_file: str, request_key: str) -> dict and python -m drover.artifact_client upload <result-file> --request-key <key> in drover/artifact_client.py; CLI reads DROVER_RUN_CREDENTIAL_FILE and prints only artifact URI/hash/receipt. Control-file JSON keys are credential_id, token, expires_at, task_id, run_id, launch_nonce, artifact_endpoint; result_file=- reads UTF-8 stdin for read-only workers.

**Implementation:** Implement the design Assigned-run artifact authentication contract using stdlib secrets/hashlib/hmac and the #6 tables. Only the trusted in-process dispatcher calls issuance after claim/pin for no-repo-change; expose no HTTP mint/refresh endpoint. Generate drun_ plus 32 random bytes URL-safe; store only token hash and binding/scope/timestamps. Expire at issued+24h, shortened to the approved wall deadline when configured (None means no shorter deadline); use server time. Verification checks active assignment/lease, run nonce, scope, pinned delivery and expiry/revocation; IDs and nonces alone grant nothing. Revalidation repeats mutable checks inside the artifact persistence transaction. Retain revoked metadata for durable receipt validation; never store plaintext in SQLite/events. The client reads the protected launch control file, enforces fixed endpoint, 1 MiB UTF-8 limit, HTTPS across hosts or explicitly configured local Windows/WSL path, disallows redirects, sends bearer only in Authorization and redacts all errors. No owner/shared bearer fallback, YAML or hub imports. Launch file protection/delivery is #17; issuance/revocation call sites are #20; auth dependency is #22; persistence/API wiring is #24/#25.

- [ ] Add failing tests: `test_run_binding`: issued credential verifies only its active task/run/nonce and artifact:write scope; other task, another run of the same task, stale nonce and repo-changing policy fail forbidden; IDs without a token never authenticate; `test_expiry_and_revocation`: expiry is issued+24h without a configured wall deadline and min(wall deadline, issued+24h) otherwise; expired/unknown/revoked tokens fail unauthorized; lease loss fails forbidden; issuance refuses unclaimed or code-delivery runs; hashes/events contain no plaintext; `test_revalidation_race`: authenticated principal becomes unusable inside persistence transaction after cancellation/revocation/nonce change; retained revoked metadata can still identify a previously committed receipt without authorizing a new upload; `test_artifact_client_transport`: helper uploads bounded UTF-8 bytes from a file or stdin with private-file bearer and stable request key; redirects, cross-host plaintext, oversize content and missing credential file fail without logging token; CLI stdout contains only URI/hash/receipt; imports remain stdlib-only.
- [ ] Run `python3 -m unittest tests.test_run_auth tests.test_artifact_client -v`; confirm the new assertion fails for the absent behavior, not an environment/import setup error.
- [ ] Implement the interfaces and behavior above, keeping later consumers behind the activation flag.
- [ ] Rerun `python3 -m unittest tests.test_run_auth tests.test_artifact_client -v`; require exit 0 and all assertions passing.
- [ ] Inspect scope/diff and return ready_to_commit with evidence and a one-sentence subject; no git add/commit on Codex.

### #17: Assemble typed briefs, installed skills and bounded handoffs

**Size/risk:** M/high. **Dependencies:** #3, #7, #11, #13, #14, #15, #16. **Grounding:** spec E5, E12, E13.

**Files (exclusive scope):** `drover/brief.py`, `drover/launch.py`, `drover/pinned.md`, `drover/skills.py`, `tests/test_task_briefs.py`.

**Interfaces:** Produce resolve_skills(names: list[dict], seat_catalog: dict) -> dict in skills.py, returning content hashes/missing required/optional omissions. Extend brief.assemble/assemble_review with optional execution snapshot/context manifest; extend launch specs with budget/handoff ceilings and saved session metadata. Consume #16 issuance bundle as an ephemeral launch-only argument (never execution_json); extend launch spec/control path with credential_id and protected credential-file pointer, not token contents. Brief invokes python -m drover.artifact_client upload - --request-key <key> with stdin for read-only workers; result-file upload remains available.

**Implementation:** Render core/type field blocks, framework layout/instructions, allowed read directories, explicit skill invocation and correct repo/artifact action record from #15: repo writes require ready_to_commit/commit_message or done/SHA; no-repo-change uses done with empty SHA and artifact.uri/sha256 after authenticated upload. Resolve only allowlisted template variables; never execute expressions. Selected-seat installed catalog must contain required skills; optional omissions are visible. Bound includes/globs/bytes, keep pinned/task/acceptance/owner decisions/handoff mandatory, trim optional excerpts deterministically with omission manifest and fail if mandatory content exceeds budget. Use S/M worker 6000/15000 and reviewer 12000/30000 unless type override. Rotate at min(type threshold default 60000, reported harness ceiling), keep handoff<=1500 plus structured artifact refs, reject compaction. Keep legacy signatures callable and no YAML imports in WSL launch. After _fresh_run_dir and before spawning, write the one-time credential bundle to a private control file outside repo/artifacts; check POSIX 0600 or Windows owner ACL plus per-run sandbox read grant. Same-account ACLs alone do not isolate workers; refuse launch if another sandbox can read it or effective protection cannot be verified (including WSL mounts). Pass only DROVER_RUN_CREDENTIAL_FILE in Windows/WSL child env, grant that exact file for read-only artifact workers, and keep secrets out of argv/in.txt/briefs/records/logs. Cleanup file on definite launch failure and support #20 cleanup/recovery hooks. No new credential from adoption by ID; only same live run/nonce with matching unexpired protected file may resume.

- [ ] Add failing tests: `test_mandatory_context`: oversize mandatory content fails; optional excerpts trim with manifest; owner answers are not dropped; `test_skills_and_paths`: missing required selected-seat skill blocks; optional omission records; include cannot read unapproved repo or traverse; `test_rotation_snapshot`: threshold rotation preserves type/policy/session provenance and bounded handoff; compaction setting is rejected; briefs emit the exact delivery-specific completion shape and never instruct a repo-changing worker to finish with artifact only; `test_private_run_delivery`: fake Windows and WSL launches receive only credential-file pointer; token is absent from argv/brief/serialized execution/record/log output and other worker grants; unprotected file or broad sandbox read fails before spawn, definite failure removes file, lost-file adoption refuses, protected same-nonce adoption preserves identity.
- [ ] Run `python3 -m unittest tests.test_task_briefs -v`; confirm the new assertion fails for the absent behavior, not an environment/import setup error.
- [ ] Implement the interfaces and behavior above, keeping later consumers behind the activation flag.
- [ ] Rerun `python3 -m unittest tests.test_task_briefs -v`; require exit 0 and all assertions passing.
- [ ] Inspect scope/diff and return ready_to_commit with evidence and a one-sentence subject; no git add/commit on Codex.

### #18: Persist grill frontiers, owner rounds and landed decision edits

**Size/risk:** M/low. **Dependencies:** #7, #8, #9, #11, #17. **Grounding:** spec E4, E5, E12.

**Files (exclusive scope):** `drover/grill.py`, `tests/test_grill.py`.

**Interfaces:** Produce start_grill(conn, target: dict, skill: str, principal: dict, request_key: str) -> dict, publish_round(conn, grill_id: str, round_number: int, questions: list[dict], frontier: dict, execution: dict) -> dict, resume_grill(conn, grill_id: str) -> dict, and complete_grill(conn, grill_id: str, evidence: dict) -> dict in grill.py.

**Implementation:** Reference exactly one installed grilling/grill-with-docs/grill-me skill and target version. Persist stable numbered question/recommendation/rationale IDs, one owner-answer task per round, frontier, backend session and handoff. Release worker slot while waiting; no reciprocal dependency. Replay same round publication idempotently. Resume same backend session only with verified compatible snapshots, otherwise fresh session with prior structured answers. Changed target requests owner rebase/restart; default 20-round limit requests owner. Read-only grill proposes exact scoped docs edits for glossary/ADRs, waits on their landing without a cycle, and completes only with empty frontier/all answers/owner acceptance/landed edits. Enforce destination-specific grill-complete before proposing implementation follow-ups; grill/docs proposals stay possible before completion and explicit owner waiver is audited.

- [ ] Add failing tests: `test_round_recovery`: crash/retry publishes one owner task; partial draft stays waiting; structured final answer feeds one compatible resume or fresh handoff; `test_frontier_prerequisites`: nonempty frontier, missing owner acceptance or unlanded ADR prevents completion and implementation proposal, while prerequisite grill and docs proposals succeed; `test_target_and_cycles`: changed target pauses; round 21 requests owner; requester->answer/docs edges never create reverse cycle; waiver requires owner.
- [ ] Run `python3 -m unittest tests.test_grill -v`; confirm the new assertion fails for the absent behavior, not an environment/import setup error.
- [ ] Implement the interfaces and behavior above, keeping later consumers behind the activation flag.
- [ ] Rerun `python3 -m unittest tests.test_grill -v`; require exit 0 and all assertions passing.
- [ ] Inspect scope/diff and return ready_to_commit with evidence and a one-sentence subject; no git add/commit on Codex.

### #19: Execute workflow outbox actions with crash recovery

**Size/risk:** M/high. **Dependencies:** #8, #9, #14, #18. **Grounding:** spec E3, E4, E7.

**Files (exclusive scope):** `drover/workflow_runner.py`, `tests/test_workflow_runner.py`.

**Interfaces:** Produce drain_actions(port, handlers: dict, limit: int = 32) -> list[dict] and recover_action(port, action: dict, probes: dict) -> dict in workflow_runner.py. Handlers receive typed saved action payloads/idempotency keys and return evidence dictionaries; no YAML-specified code.

**Implementation:** Execute the bounded action vocabulary in exit-before-entry order, persist intent/start/result and advance only on required evidence. Map create worktree, reads, gate, review, landing, artifacts, owner tasks, notifications, worker resume and follow-up proposals through injected services. Durable inbox/SSE notification events are the baseline; external notification adapters are opt-in. Recover launch effects by nonce/PID and receipt hashes; uncertain non-idempotent effects request owner, never blindly rerun. The runner may not add capacity or bypass pinned admission. Child follow-ups remain proposed and check allowed types plus only the guard list selected by destination type; before grill completion, grill/docs proposals succeed and implementation returns a prerequisite conflict.

- [ ] Add failing tests: `test_crash_between_effect_and_ack`: recovery finds original nonce/receipt and writes one result without launching another worker; `test_action_order`: exit actions finish before entry actions; failed required action prevents phase advancement; retries preserve event/action key; `test_notifications_and_followups`: notification retry yields one inbox item; unauthorized follow-up type or implementation missing grill-complete refuses proposal; grill/docs proposals before completion remain proposed and permitted, and a valid exact-target grill result permits implementation.
- [ ] Run `python3 -m unittest tests.test_workflow_runner -v`; confirm the new assertion fails for the absent behavior, not an environment/import setup error.
- [ ] Implement the interfaces and behavior above, keeping later consumers behind the activation flag.
- [ ] Rerun `python3 -m unittest tests.test_workflow_runner -v`; require exit 0 and all assertions passing.
- [ ] Inspect scope/diff and return ready_to_commit with evidence and a one-sentence subject; no git add/commit on Codex.

### #20: Dispatch saved lifecycles and artifact work through the kernel

**Size/risk:** M/high. **Dependencies:** #10, #13, #14, #15, #16, #17, #18, #19. **Grounding:** spec E3, E5, E6, E7, E12.

**Files (exclusive scope):** `drover/dispatcher.py`, `drover/port.py`, `tests/test_dispatcher.py`, `tests/test_typed_dispatcher.py`.

**Interfaces:** Consume saved task/execution snapshots, route defaults and outbox handlers. Extend DirectPort/HttpPort contracts for type snapshots, phases, artifact submission and action intents with compatible defaults. Dispatcher tick selects by phase, pins execution, checks installed skills, and launches only eligible agent work. DirectPort exposes trusted issue_run_credential/revoke_run_credentials adapters from #16; HttpPort never gains HTTP credential issuance. Dispatcher passes the returned secret only to ephemeral launch delivery in #17.

**Implementation:** Wire claim/run/gate/review transitions through validated workflow actions. Gate lookup/worktree creation applies only when delivery requires it; artifact work consumes the preserved artifact completion record and can finish without repo gate or commit only after matching its URI/hash to this task's durable authenticated run receipt and required acceptance/review evidence. Reject artifact-only completion for commit+land/spec-only and retain SHA gate/review/landing requirements. Release process lease while owner task waits, retain write reservation, resume from exact snapshot/handoff. Preserve orphan recovery, backoffs, escalation and resource/allowance checks; inspect run evidence before recovery actions. Keep legacy runs on legacy path until drained and refuse enabled configurable dispatch before activation_ready. Audit all status literals in dispatcher; no custom state can accidentally bypass gate/review/landing. Issue only after atomic claim/pin and saved launch nonce, with the approved wall deadline; revoke on definite failed launch, cancellation, lease loss, rotation/owner wait and every terminal run, then remove control file. Restart probes live PID/nonce/lease and protected file/hash/expiry before adoption; missing/expired/uncertain identity revokes and enters normal stop/fresh-run recovery. Never mint from worker-supplied IDs. A receipt authenticated and committed before terminal revocation remains valid completion evidence bound to saved task/run/nonce/credential/hash; no new uploads are accepted after revocation.

- [ ] Add failing tests: `test_artifact_without_gate`: read-only research completes with durable artifact hash despite unknown repo code gate; dependent becomes eligible; forged/missing/mismatched artifact receipt or hash fails; artifact-only completion for commit+land/spec-only fails without bypassing SHA evidence; `test_custom_lifecycle_dispatch`: renamed eligible/running/gated labels execute with correct phases and evidence; owner task consumes no slot; `test_restart_waiting`: restart during owner wait/review/outbox effect keeps one run/result and blocks overlapping scope; legacy active lease prevents activation; `test_run_credential_lifetime`: claim/pin precedes issuance and delivery; failed launch, cancel, lease loss, rotation, owner wait and terminal outcomes each revoke/remove file; retry uses fresh credentials; restart with missing file or stale nonce cannot adopt; committed pre-revocation receipt still completes recovery once.
- [ ] Run `python3 -m unittest tests.test_typed_dispatcher -v tests.test_dispatcher`; confirm the new assertion fails for the absent behavior, not an environment/import setup error.
- [ ] Implement the interfaces and behavior above, keeping later consumers behind the activation flag.
- [ ] Rerun `python3 -m unittest tests.test_typed_dispatcher -v tests.test_dispatcher`; require exit 0 and all assertions passing.
- [ ] Inspect scope/diff and return ready_to_commit with evidence and a one-sentence subject; no git add/commit on Codex.

### #21: Land only eligible repository changes with versioned evidence

**Size/risk:** M/high. **Dependencies:** #8, #11, #14, #20. **Grounding:** spec E8, E15.

**Files (exclusive scope):** `drover/landing.py`, `hub/dispatch.py`, `tests/test_landing.py`, `tests/test_hub_dispatch.py`, `tests/test_type_landing.py`.

**Interfaces:** Consume phase/outcome/type snapshot, exact-SHA gate/review or approved exemption receipts. Landing candidate selection uses execution_phase=reviewed plus delivery mode commit+land/spec-only; completion goes through dispatcher-authorized workflow transition.

**Implementation:** Update hub.dispatch.HubPort signatures/principal forwarding and LandingService.tick phase-based selection together with landing.py. Exclude owner/artifact/no-repo-change work entirely; support renamed states and restrict spec-only paths. Keep queue-only fast-forward behavior, exact revision checks, tip gating and independent required review. Policy exemptions must be explicit signed-by-service event evidence for exact SHA, not free-form worker claims. Changed head invalidates gate/review; result never becomes completed early. OpenSpec finalization proposal/land verifies prerequisite change tasks; do not archive through an unreviewed post-land hook.

- [ ] Add failing tests: `test_delivery_filter`: only successful reviewed repo modes enter queue, including custom visible state names; owner/artifact tasks never do; `test_exact_revision`: moving head or mismatched snapshot/receipt prevents completion; actual code requires review/gate; declared docs exemption cannot cover code; `test_framework_finalization`: unlanded change tasks prevent archive finalization; finalization lands as its own gated reviewed commit.
- [ ] Run `uv run --project hub --no-sync python -m unittest tests.test_type_landing -v tests.test_landing tests.test_hub_dispatch`; confirm the new assertion fails for the absent behavior, not an environment/import setup error.
- [ ] Implement the interfaces and behavior above, keeping later consumers behind the activation flag.
- [ ] Rerun `uv run --project hub --no-sync python -m unittest tests.test_type_landing -v tests.test_landing tests.test_hub_dispatch`; require exit 0 and all assertions passing.
- [ ] Inspect scope/diff and return ready_to_commit with evidence and a one-sentence subject; no git add/commit on Codex.

### #22: Authenticate paired owner CLI clients independently of workers

**Size/risk:** M/high. **Dependencies:** #9, #16. **Grounding:** spec E9.

**Files (exclusive scope):** `hub/auth.py`, `hub/owner_auth.py`, `tests/test_owner_auth.py`.

**Interfaces:** Produce begin_pairing(store, client: dict) -> dict, approve_pairing(store, principal: dict, challenge_id: str, code: str) -> dict, redeem_pairing(store, challenge_id: str, secret: str) -> dict, verify_owner_credential(store, token: str) -> dict | None and revoke_credential(store, principal: dict, credential_id: str) -> dict in hub/owner_auth.py. Extend require_api with structured principal support while preserving a token principal compatibility adapter. Add require_run_artifact(request) -> Principal using #16 verify_run_credential with endpoint task/run bindings. This dedicated dependency accepts only drun_ credentials; require_api/require_ui reject run credentials on general and owner routes.

**Implementation:** Implement five-minute rate-limited device challenges bound to client/code and random one-time secret; only existing authenticated UI owner with CSRF can approve pairing. Redeem separately approved challenge to an eight-hour hashed revocable owner credential; no refresh initially. Worker bearer cannot approve/mint/redeem as owner or override principal. Revocation and expiry work on both channels; audit IDs not tokens. Use owner-only state for hashes; client credential storage is #27. Preserve existing trusted Host/port/Tailscale checks, CSRF and known mirrored-WSL limitation; do not treat forged headers or local API bearer as owner. Keep run and owner credential namespaces distinct. Missing/unknown/expired/revoked run token returns 401; credential bound to another task/run/nonce or wrong delivery/lease returns 403. A shared bearer, paired owner token, forged actor or supplied run ID cannot satisfy require_run_artifact. Never fall back from a rejected drun_ token to UI/locality/shared authority or echo a token in errors.

- [ ] Add failing tests: `test_worker_cannot_pair_as_owner`: worker bearer, forged ui prefix/Origin/Tailscale header and unapproved challenge cannot acquire owner authority; `test_pairing_lifetime`: correct UI approval+secret redeems once; expiry at five minutes/eight hours and revocation reject use; `test_csrf_and_client_binding`: cross-origin pairing approval and wrong code/client secret fail; rate limiting does not leak credential contents; `test_run_auth_separation`: valid assigned-run token authenticates artifact endpoint only; wrong task, second run of same task, stale nonce and code delivery fail 403; expired/revoked fail 401; shared bearer and paired owner token fail artifact auth; drun_ token cannot read/mutate general or owner routes; no UI/loopback fallback.
- [ ] Run `uv run --project hub --no-sync python -m unittest tests.test_owner_auth -v`; confirm the new assertion fails for the absent behavior, not an environment/import setup error.
- [ ] Implement the interfaces and behavior above, keeping later consumers behind the activation flag.
- [ ] Rerun `uv run --project hub --no-sync python -m unittest tests.test_owner_auth -v`; require exit 0 and all assertions passing.
- [ ] Inspect scope/diff and return ready_to_commit with evidence and a one-sentence subject; no git add/commit on Codex.

### #23: Define shared field forms and card/page action descriptors

**Size/risk:** S/low. **Dependencies:** #0, #7, #9. **Grounding:** spec E10, E13.

**Files (exclusive scope):** `drover/task_forms.py`, `tests/test_task_forms.py`.

**Interfaces:** Produce task_view(task: dict, snapshot: dict, actions: list[dict]) -> dict and form_descriptor(snapshot: dict, purpose: str, values: dict) -> dict in task_forms.py. Output JSON-only descriptors: type_version, revision, core/custom ordered card/page fields, values, questions, validation paths and permitted actions; renderers do not own behavior.

**Implementation:** Use pinned type fields and create/edit/answer hints; derive supported web/CLI widget/order defaults. Reject unsupported widgets, hidden required inputs and invalid question option/recommendation IDs. Preserve typed false/empty/list values, T-7 core type/access/repo/connections/acceptance/run/event fields, and action schemas from the owner service. Escape at rendering boundary, never trust YAML HTML or evaluate expressions. Include generic descriptors for existing owner controls so terminal and website capabilities match.

- [ ] Add failing tests: `test_channel_schema_parity`: web and CLI orders may differ but required field IDs/types/action payloads are identical; `test_pinned_views`: type reload does not change old card/page fields; false/[] display correctly and type-specific visibility is obeyed; `test_unsafe_hints`: unsupported widget or hidden required answer fails configuration; recommendations must reference actual option IDs.
- [ ] Run `python3 -m unittest tests.test_task_forms -v`; confirm the new assertion fails for the absent behavior, not an environment/import setup error.
- [ ] Implement the interfaces and behavior above, keeping later consumers behind the activation flag.
- [ ] Rerun `python3 -m unittest tests.test_task_forms -v`; require exit 0 and all assertions passing.
- [ ] Inspect scope/diff and return ready_to_commit with evidence and a one-sentence subject; no git add/commit on Codex.

### #24: Adapt hub storage and inbox to saved types and shared actions

**Size/risk:** M/high. **Dependencies:** #2, #7, #9, #10, #16, #18, #22, #23. **Grounding:** spec E2, E4, E9, E10, E13.

**Files (exclusive scope):** `hub/store.py`, `hub/type_service.py`, `tests/test_type_store.py`.

**Interfaces:** Produce hub/type_service.py RegistryService(store, cfg) with validate_sources(sources)->dict, preview_reload(sources)->dict, activate(principal,sources,expected_hash)->dict, registry(repo)->dict and snapshot(hash)->dict. Extend Store/UiView with typed task descriptors, action_descriptors and perform_action adapters; publish after commit. Produce Store.persist_run_artifact(principal: dict, content: bytes, expected_hash: str, request_key: str) -> dict, returning server-derived URI/hash/receipt ID. Consume #16 revalidate_run_principal inside the same ledger transaction that commits the receipt.

**Implementation:** Maintain one active immutable registry and owner-only activation; register snapshots before use, leave old active on invalid reload. Connect ledger facade/owner actions to existing UI/store controls without duplicating business rules. Inbox includes owner tasks/grill rounds with task-specific forms and deduplicated legacy questions/tuner/garden items. Store artifacts and manifests under hub-managed task records with content hashes; successful completion verifies persisted content. Sync question facade once and notify via durable outbox/inbox plus existing event bus. Preserve source/owner-edit behavior and monotonic revision errors. Bound content to 1 MiB UTF-8; compute/compare hash and derive storage key without accepting client paths. Stage immutable content under hub control before committing receipt; no receipt references unpersisted bytes. Revalidate authenticated credential ID, assignment/lease/nonce and policy inside transaction, then persist task/run/nonce/credential/hash binding and unique request key. Same key+bytes replays, changed bytes conflicts; cancellation/revocation winning transaction prevents receipt/event. Retain committed receipt after token revocation for dispatcher recovery; safely ignore/clean unreferenced staged bytes after rollback.

- [ ] Add failing tests: `test_store_reload_and_history`: invalid activation leaves previous registry; old task snapshots/artifacts remain readable after source deletion; `test_unified_inbox`: one migrated question appears once; one grill round contains all numbered questions and final submit invokes one shared action; `test_post_commit_notifications`: transaction rollback emits no inbox/SSE success; retry yields one durable artifact/event/notification; `test_artifact_receipt_atomicity`: same run/key/bytes returns one durable receipt; changed bytes conflicts; wrong hash or >1 MiB/non-UTF-8 fails; revoke/cancel before receipt transaction yields no receipt/success event; persisted pre-revocation receipt remains readable and references existing immutable bytes.
- [ ] Run `uv run --project hub --no-sync python -m unittest tests.test_type_store -v`; confirm the new assertion fails for the absent behavior, not an environment/import setup error.
- [ ] Implement the interfaces and behavior above, keeping later consumers behind the activation flag.
- [ ] Rerun `uv run --project hub --no-sync python -m unittest tests.test_type_store -v`; require exit 0 and all assertions passing.
- [ ] Inspect scope/diff and return ready_to_commit with evidence and a one-sentence subject; no git add/commit on Codex.

### #25: Expose typed tasks, owner actions, pairing and registry APIs

**Size/risk:** M/high. **Dependencies:** #5, #12, #16, #20, #21, #22, #23, #24. **Grounding:** spec E2, E9, E10, E13.

**Files (exclusive scope):** `hub/api.py`, `hub/app.py`, `hub/type_api.py`, `hub/owner_api.py`, `tests/test_type_api.py`.

**Interfaces:** Produce /api/v1/task-types (catalog/validate/preview/activate), /tasks create/patch/show with type/version/fields/revision, /tasks/{id}/actions/{action}, /inbox, /grills, /tasks/{id}/runs/{run_id}/artifact submission and /owner-clients pairing/revoke routes. Existing approve/questions/dispatch/import endpoints delegate to the same authenticated services; pair-request/redeem router has explicit rate limits, not inherited worker-owner authority.

**Implementation:** Wire RegistryService and handlers at app creation; register routers with correct dependencies. TaskIn/TaskPatch accept custom types and fields with kind compatibility, expected_revision and request_key; no fixed Literal enum. Use 422 field errors, 409 stale/version/prerequisite conflicts and 403 authority failures consistently. Return shared card/page/form/action descriptors for web and CLI. Facade answers and approvals call perform_action; protect registry/activation/settings that enable types and all owner-reserved events against worker tokens. Activation checks legacy drain and runtime readiness. Retain existing authenticated API behavior for worker reads/proposals and expose no owner secrets. Artifact submission binds authenticated assigned-run identity to task/run and no-repo-change policy, persists content and computed hash via the artifact action service, returns URI/hash/run receipt idempotently, and rejects cross-task or hash-mismatched claims; client URI never chooses a filesystem path. Wire #22 require_run_artifact on /tasks/{id}/runs/{run_id}/artifact, not require_api. Pass only trusted principal plus bounded bytes/hash/request key to #24 Store.persist_run_artifact; store revalidates within receipt transaction. No credential mint/renew endpoint. Return 401 for missing/unknown/expired/revoked credential and 403 for cross-binding/policy failures; no shared-bearer or owner-token fallback.

- [ ] Add failing tests: `test_create_patch_and_facade`: custom type creates/patches through same validator as import; kind compatibility and legacy questions work; response has pinned form/version; `test_owner_api_authority`: worker cannot approve/answer/reload/activate/forge reserved events/pair as owner; paired owner can perform allowed actions; artifact submission by a different task/run or with a mismatched hash fails, while assigned-run retry returns one durable receipt; `test_conflicts_and_activation`: web/API race returns 409; invalid field 422; incomplete legacy drain blocks activation and old registry stays live; `test_assigned_run_upload_authority`: run A token uploads/retries for A only; run B or another run of the same task, forged nonce/IDs, shared bearer and owner token fail; expired/revoked fail 401; drun_ on owner/general API fails; cancellation/upload race leaves no unauthorized receipt; post-exit receipt recovery succeeds without reopening upload authority.
- [ ] Run `uv run --project hub --no-sync python -m unittest tests.test_type_api -v`; confirm the new assertion fails for the absent behavior, not an environment/import setup error.
- [ ] Implement the interfaces and behavior above, keeping later consumers behind the activation flag.
- [ ] Rerun `uv run --project hub --no-sync python -m unittest tests.test_type_api -v`; require exit 0 and all assertions passing.
- [ ] Inspect scope/diff and return ready_to_commit with evidence and a one-sentence subject; no git add/commit on Codex.

### #26: Render typed creation, cards, pages and owner inbox on the website

**Size/risk:** M/low. **Dependencies:** #23, #24, #25. **Grounding:** spec E9, E10, E13.

**Files (exclusive scope):** `hub/web.py`, `hub/task_views.py`, `hub/templates/task.html`, `hub/templates/_task_card.html`, `hub/templates/inbox.html`, `hub/templates/plan.html`, `hub/templates/task_create.html`, `hub/templates/_type_fields.html`, `hub/templates/_owner_form.html`, `hub/templates/owner_pairing.html`, `tests/test_type_web.py`.

**Interfaces:** Consume task_view/form_descriptor, RegistryService and perform_action; keep existing T-7 safe Markdown/repo-state probes. Web routes render descriptors and convert form transport values to typed action payloads; they do not implement actions independently.

**Implementation:** Add type selection and schema-driven create/edit forms, ordered custom card/page fields and full owner task/grill answer forms. Keep T-7 core chips, description, branch/worktree state, links, run/event views. Show pinned type/version and recommended options, retain submitted values/errors and server-side drafts. Provide explicit final round submit, owner acceptance/waiver controls and browser pairing approval details. Every owner action uses shared handlers with authenticated principal/expected_revision/request_key and web CSRF. Unsupported schema fields cannot disappear; escape labels/values/links and use conservative Markdown. No service startup or browser tooling installation is required.

- [ ] Add failing tests: `test_dynamic_create_and_views`: owner extension renders required enum/list fields on create and proper visibility on card/page without editing a kind menu; `test_owner_round_and_conflict`: draft saves without completing; final explicit submit matches API action result; stale revision reloads committed answer; `test_web_safety_and_pairing`: malicious type labels/Markdown/URI values are escaped/rejected; pairing page shows client/code and requires owner+CSRF.
- [ ] Run `uv run --project hub --no-sync python -m unittest tests.test_type_web -v`; confirm the new assertion fails for the absent behavior, not an environment/import setup error.
- [ ] Implement the interfaces and behavior above, keeping later consumers behind the activation flag.
- [ ] Rerun `uv run --project hub --no-sync python -m unittest tests.test_type_web -v`; require exit 0 and all assertions passing.
- [ ] Inspect scope/diff and return ready_to_commit with evidence and a one-sentence subject; no git add/commit on Codex.

### #27: Add the authenticated interactive owner CLI with web parity

**Size/risk:** M/high. **Dependencies:** #22, #23, #25, #26. **Grounding:** spec E9, E10, E11.

**Files (exclusive scope):** `drover/owner_client.py`, `drover/owner_cli.py`, `drover/__main__.py`, `tests/test_owner_cli.py`.

**Interfaces:** Produce OwnerClient(base_url: str, credential_store) with JSON HTTP methods using urllib and typed errors in owner_client.py; owner_cli.main(argv: list[str] | None = None) -> int implements login/inbox/show/create/edit/answer/approve/action/grill. Top-level __main__ delegates while preserving init/config.

**Implementation:** Implement browser/device pairing with secure private 0600 or Windows owner-ACL credential storage; no token reuse in worker config/env, no forged UI headers, reject remote plaintext credential transport. Render descriptor cards/pages as terminal text and all owner actions through API, never direct SQLite. Sequential grill/backlog rounds show one question/recommendation at a time; Enter accepts recommendation only at explicit TTY prompt. Save draft after each answer, explicit final submit, Ctrl-C/restart continuation, stale-answer 409 display. Non-TTY requires explicit JSON payload and never accepts defaults. Generic action covers dispatch/epic/import/garden/policy/waiver/revoke and type controls with the same capability descriptors. Preserve stdlib-only client imports without yaml/FastAPI.

- [ ] Add failing tests: `test_interactive_grill`: scripted TTY accepts one explicit recommended answer, saves drafts, Ctrl-C resumes next unanswered question and final submit completes once; `test_nontty_and_race`: non-TTY without payload submits nothing; payload validation errors and 409 show saved answer without overwrite; `test_auth_and_parity`: private credentials required; expiry/revoke asks login; all API action descriptors are invokable; no yaml or hub imports in client subprocess.
- [ ] Run `uv run --project hub --no-sync python -m unittest tests.test_owner_cli -v`; confirm the new assertion fails for the absent behavior, not an environment/import setup error.
- [ ] Implement the interfaces and behavior above, keeping later consumers behind the activation flag.
- [ ] Rerun `uv run --project hub --no-sync python -m unittest tests.test_owner_cli -v`; require exit 0 and all assertions passing.
- [ ] Inspect scope/diff and return ready_to_commit with evidence and a one-sentence subject; no git add/commit on Codex.

### #28: Tune type routing with replayable policy overlays

**Size/risk:** M/low. **Dependencies:** #7, #13, #20. **Grounding:** spec E11.

**Files (exclusive scope):** `drover/tuner.py`, `tests/test_tuner.py`, `tests/test_type_tuner.py`.

**Interfaces:** Extend routing statistics/overlay keys to (type_version, role, size), retaining existing policy version writes. Produce effective_type_policy(type_snapshot: dict, policy: dict, role: str, size: str) -> dict in tuner.py for diagnostic/pin/rollback consumers.

**Implementation:** Only adjust model preference/candidate weights/effort within the type routing tunable allowlist and approved ladder; no type YAML edits. Save new policy_version for each applied overlay and original execution_json for replay; new runs can adopt policy, existing backend sessions retain pinned execution. Add type-scoped cold priors and artifact-success metrics separated from cost-per-landed; do not pool unrelated types. Attempts to change access/lifecycle/assignee/skills/acceptance/gate/review/landing/framework or add unapproved model become owner proposals. Expose effective diff/pin/rollback data for generic actions without extending API scope.

- [ ] Add failing tests: `test_overlay_limits`: in-ladder model preference creates new policy, forbidden access/gate/model additions propose owner work and leave type snapshot unchanged; `test_run_replay`: policy update affects new runs only; prior execution snapshot reproduces original route; pin/rollback selects recorded policy; `test_metric_partition`: artifact success counts for its type and never divides into landed cost; unseen type uses configured priors.
- [ ] Run `python3 -m unittest tests.test_type_tuner -v tests.test_tuner`; confirm the new assertion fails for the absent behavior, not an environment/import setup error.
- [ ] Implement the interfaces and behavior above, keeping later consumers behind the activation flag.
- [ ] Rerun `python3 -m unittest tests.test_type_tuner -v tests.test_tuner`; require exit 0 and all assertions passing.
- [ ] Inspect scope/diff and return ready_to_commit with evidence and a one-sentence subject; no git add/commit on Codex.

### #29: Verify compatibility rollout and document owner operations

**Size/risk:** M/high. **Dependencies:** #12, #15, #16, #17, #20, #21, #22, #24, #25, #26, #27, #28. **Grounding:** spec E1.

**Files (exclusive scope):** `tests/test_task_types_e2e.py`, `docs/task-types.md`, `README.md`, `hub/scripts/README-INSTALL.md`.

**Interfaces:** Consume shipped registry, migration/bootstrap, hub action routes and owner CLI. Produce end-to-end fixtures for main-v3 and reconciled T-7 plus owner operating documentation; no production file refactors.

**Implementation:** Exercise opt-in activation, typed create/import, web/CLI answers, grill prerequisite, snapshot reload, scoped spec landing and framework finalization with fake runners/git fixtures. Document decision D1 prerequisite, hub-only parser installation by owner, parser-free WSL client, registry precedence/extends/errors, type upgrade/rollback, credential pairing/revocation, questions compatibility and framework source imports. Provide reproducible Windows hub and WSL verification commands using already-authorized environments, no automatic install/start/build. Keep enabled=false until exact deployment gates and legacy drain pass; explain backup/rollback and T-b extension seams. Run repository regression suite once on the complete feature in an admitted worker, no extra parallel builds. Document run-credential scope, private launch file protection, 24h/wall expiry, revocation events and lost-file recovery separately from owner pairing. Fake Windows/WSL end-to-end flow must cover trusted issuance -> protected file -> stdlib helper -> dedicated auth -> atomic immutable receipt -> parser/launch recovery -> dispatcher completion after terminal revocation, with cross-run rejection and no token disclosure.

- [ ] Add failing tests: `test_spec_grill_owner_flow`: worked extension including inherited problem creates through both forms; before grill completion implementation proposal fails but grill/docs proposals succeed; custom spec -> grill -> owner round via CLI/web -> docs follow-up landed -> implementation proposed, with no reciprocal deps or auto-approval; `test_replay_restart_and_frameworks`: registry reload/restart preserves old snapshots and one question/action; OpenSpec archive is a separate reviewed commit and Spec Kit import stays proposed; authenticated artifact upload -> trusted runner frame -> record.json -> launch restart -> dispatcher completion retains URI/hash and unblocks once, while repo-changing artifact-only output fails; `test_main_t7_compatibility`: main/T-7 fixtures preserve data and existing APIs; worker bearer still cannot act as owner; activation refuses legacy live runs; `test_run_identity_e2e`: assigned credential succeeds from protected Windows/WSL launch fixture; replacing endpoint task/run (including same-task retry), stale nonce, shared/owner bearer, expiry or revocation rejects without receipt; normal terminal revocation preserves prior receipt for exact-run recovery; lost credential file forces fresh run, not ID-only adoption.
- [ ] Run `uv run --project hub --no-sync python -m unittest tests.test_task_types_e2e -v`; confirm the new assertion fails for the absent behavior, not an environment/import setup error.
- [ ] Implement the interfaces and behavior above, keeping later consumers behind the activation flag.
- [ ] Rerun `uv run --project hub --no-sync python -m unittest tests.test_task_types_e2e -v`; require exit 0 and all assertions passing.
- [ ] Inspect scope/diff and return ready_to_commit with evidence and a one-sentence subject; no git add/commit on Codex.

## Final rollout gate

- [ ] In #29 run the full existing suite in an admitted environment: `uv run --project hub --no-sync python -m unittest discover -s tests -v`; record platform and output. No new service or package install is implicit.
- [ ] Owner validates the authorized locked hub environment on Windows and the parser-free CLI/worker environment on WSL using the same focused suite/stdlib import checks. Missing platform access is an explicit deployment prerequisite, not a claimed pass.
- [ ] Verify D1–D11 choices, backups, legacy drain, active registry diff, paired-client and run-credential revocation, private launch delivery, cross-run rejection and disabled feature state before owner activation. Enable only through owner action; rollback selects previous registry/policy for new tasks and preserves existing snapshots.
- [ ] Pass actual gate/review evidence for the exact implementation SHA into the landing queue; this research task provides no gate receipt for future code.

## Plan self-review

Coverage: registry/format/example/defaults #0–#5; state/assignee/storage/migration/audit #6–#10; framework layouts/imports #11–#12; routing/access/context/skills/gates/landing #13–#21; owner authentication/forms/web/CLI/action parity #22–#27; tuning and cross-platform compatibility #28–#29. Artifact completion parsing/recovery is owned by #15; run credentials/client by #16, launch delivery by #17, lifetime/dispatch authorization by #20, auth separation by #22 and atomic persistence/API by #24/#25; inherited required fields and destination-specific follow-up prerequisites are exercised by #0/#3/#18/#19/#29. Every Review Focus item is assigned concrete assertions above. Task scopes are disjoint; each dependency points backward and signatures match their producer. The owner review gate precedes execution; this task delivers the proposed design/plan/tasks only.
