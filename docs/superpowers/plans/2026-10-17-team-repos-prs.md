# Team Repositories and PRs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver team repository work through owner-approved GitHub PR effects and verified human merges while preserving solo local delivery.

**Architecture:** T9 YAML types/lifecycles describe PR and owner work. A Windows hub adapter owns gh, durable observations, approval-bound outbox effects and delivery receipts; workers produce code and drafts. Repo policy selects local or GitHub delivery without changing old task snapshots.

**Tech Stack:** Existing Python/stdlib worker contracts, T9 hub YAML codec, SQLite ledger, installed Windows gh, existing hub API/templates and owner CLI. No new dependency.

**Spec:** [docs/superpowers/specs/2026-10-17-team-repos-prs-design.md](../specs/2026-10-17-team-repos-prs-design.md)

## Global Constraints

- Types, fields, states and forms are T-9 configuration; code supplies bounded GitHub effects, evidence and permission enforcement.
- No automatic merge, force push, shared-branch rewrite or automatic branch deletion.
- Solo snapshots do not change.
- Missing required installed skills stops with owner work; never install automatically.
- No public webhook endpoint or new service is required.
- The spec's D1-D11 recommendations are planning assumptions. Configuration activation, Windows auth/identity validation and pilot publishing need owner policy approval; this plan is not that approval.
- T9 design has landed, but all tasks require its implementation contracts first. Reconcile T9 plan tasks #0-29, especially kernel #8, pipeline #14, recovery #15/#17, owner actions #9, workflow runner #19, landing #21, hub/UI/CLI #24-27. Do not run against only the current fixed-kind code.
- T-2 controls solo origin sync; T-8 item 7 supplies approved commit identity before publishing. Neither dependency permits changing their files outside a task's scope.
- All future workers stay in their task worktrees. Codex reports ready_to_commit without git add/commit; Drover checks scope, commits and gates the exact SHA. No worker publishes or merges.

## Review Focus

1. An external write succeeds just before a crash: #10 tests one recovered receipt or an owner hold, never a duplicate; #22 repeats through the hub.
2. PR head/base/rules or comments change after approval: #8 invalidates readiness, #9 rejects stale approval, #10 checks immediately before mutation, #12 redrafts stale inline findings.
3. No required-check data, stale CI or inaccessible rules: #8 treats unknown as blocking, separately tests explicit empty exemption and eligible human approval; #18 holds merged-unverified.
4. Owner pauses and competing writers: #3 reserves the parent/child union, #17 releases seats but not scopes, #20 tests CLI/web decisions and canceled owner tasks.
5. Existing solo config, disabled example or headless wizard rerun: #1/#16/#18/#21/#22 preserve local delivery/legacy explicit push and refuse team main pushes or placeholder dispatch.

## File and interface map

Each task below owns its complete exact file list; no file or directory overlaps another task. Existing T9 files are extended only after its tasks land. Interfaces use the #0 TypedDicts and T9 canonical task/action dictionaries. Interfaces are named here and repeated in each JSON goal so a separately dispatched worker has its own contract.

- `drover/prs/contracts.py`: canonical data; `ledger/prs.py`: durable PR state; `prs/reservations.py`: delegation.
- `prs/github.py`: bounded gh transport; `prs/sync.py`: observations, CI/rules and polling; `prs/approvals.py`: draft authority; `prs/effects.py`: approved outbox mutations.
- `prs/open_pr.py`, `reviews.py`, `responses.py`, `docket.py`: task-specific preparation, each without publishing authority.
- `task_types/`, `lifecycles/`, `task_templates/`: exact new PR definitions; kernel files enforce bounded capabilities, never skill/type-name branches.
- Existing pipeline/dispatcher/runner/launch/landing integrate saved policy and receipts; hub PR service/API and templates consume ledger projections; wizard emits disabled-first registration.

## Scheduling and external prerequisites

Indices are zero-based and identical to the `.tasks.json` companion; `#n` always refers to that file, never a live task ID. Import as proposed only. All list dependencies are earlier indices and represent completed/landed contracts; external T9/T-2/T-8 prerequisites are recorded in each goal rather than invented index references.

After #0, config/storage/gh/parser can proceed independently. Definitions, drafting modules, reservation work, pipeline and onboarding form additional parallel branches once their listed contracts land. Admission decides actual concurrency; this plan starts no workers or builds. Join integration in #17-20; #22 verifies the integrated result. There are 23 S/M tasks. Non-overlapping paths make parallelism possible, not permission to ignore dependencies.

Verification commands use the existing test environment. The hub commands below use `uv run --project hub --no-sync`; if unavailable, record a blocker instead of syncing or installing. Any SQLite test fixture must use native temporary storage (WSL scratch outside mounted drives). Windows-only auth/identity and human pilot checks remain owner rollout work. No live GitHub tests are part of worker gates.

---
### Task #0: Define canonical PR, publication and receipt contracts

**Size / risk:** M / low. **Dependencies:** none in this list; external prerequisites above.

**Files:** `drover/prs/__init__.py`, `drover/prs/contracts.py`, `tests/test_pr_contracts.py`.

**Interfaces:** Produce TypedDicts PRRef, PRSnapshot, PublicationContext, PublicationDraft, PublicationReceipt, PRProjection, PollState and Delegation; functions validate_pr_ref(value: dict, registered: dict) -> PRRef, validate_pr_snapshot(value: dict, registered: dict) -> PRSnapshot, validate_drafts(value: object) -> list[PublicationDraft], action_hash(draft: dict) -> str. Use JSON-compatible dictionaries throughout; T9 canonical_hash supplies stable hashing.

- [ ] **Pin the tests:** test_snapshot_identity: valid pr_ref round-trips; missing/null/malformed/unregistered pr_ref and extra snapshot keys fail; snapshots with the same number in different registered repositories retain distinct identities. test_refs: pre-creation PublicationContext permits null pr_snapshot; positive PR number with registered host/repository succeeds; wrong host, unregistered repository, number=0 and bool-as-int fail. test_hash: reordered JSON keys hash equally; changed body, target, expected head/base or policy changes hash; NaN fails. test_drafts: UTF-8 bytes over 65536, merge/force actions, unknown authority keys and malformed inline coordinates fail before persistence.
- [ ] **Observe the failure:** run `python3 -m unittest tests.test_pr_contracts -v`; confirm the named missing behavior fails, not an unrelated environment error.
- [ ] **Implement:** Fix keys shared by all later slices: PRRef host/repository/number; snapshot pr_ref/head_sha/base_sha/state/draft/merged_at/merge_sha/author_login/mergeable/unanswered_feedback/checks/reviews/threads/requirements_hash/observed_at/generation; draft id/version/task_id/run_id/action/target/expected_head/expected_base/remote_old_sha/payload/type_version/repo_policy_hash/source_artifact_hash/action_hash. PublicationContext has registered target, observed head/base/remote-old SHA and optional pr_snapshot (null before PR creation); approval preflight never requires a PR number for the initial push/create. Server assigns IDs, versions and evidence. Action enum is push-branch/create-pr/submit-review/reply-comment only. PRSnapshot.pr_ref is a required non-null PRRef validated against the registry; only observation fields may be unknown. validate_pr_snapshot rejects missing/malformed identity and extra keys. Snapshot unknowns remain explicit. Proposal payload totals at most 64 KiB UTF-8, no authority flags, shell commands or credentials. Define projection and poll/delegation fields exactly as the design storage and reservation sections require; reject extra keys.
- [ ] **Verify:** Run python3 -m unittest tests.test_pr_contracts -v; exit 0 with the named assertions passing in the existing authorized environment.
- [ ] **Hand off:** review only the listed scope diff and report ready_to_commit with a one-sentence subject; Drover commits/gates. Never self-publish or self-merge.

### Task #1: Normalize collaboration and GitHub repository policy

**Size / risk:** M / low. **Dependencies:** #0; external prerequisites above.

**Files:** `drover/repos.py`, `tests/test_pr_repo_config.py`.

**Interfaces:** Produce normalized collaboration and github tables through existing normalize(raw, name=None); expose effective_delivery(task: dict, repo: dict) -> dict in drover/repos.py. Return mode, local_gate, agent_review, human requirements and pinned policy hash; never mutate a running task snapshot.

- [ ] **Pin the tests:** test_legacy_solo: old configs produce identical landing/run/gate/review values, including explicit push=true. test_team_defaults: team with local_gate=off and no commands normalizes; team push=true or landing.mode=local fails. test_policy: wildcard trust, invalid refs, unregistered hosts and unknown GitHub keys fail; disabled placeholders remain non-dispatchable; policy hash changes for semantic safety settings.
- [ ] **Observe the failure:** run `python3 -m unittest tests.test_pr_repo_config -v`; confirm the named missing behavior fails, not an unrelated environment error.
- [ ] **Implement:** Implement the exact TOML keys/defaults in the design. Missing collaboration stays solo and preserves explicitly configured legacy pushes. Team derives landing.mode=pr and rejects main push, conflicting explicit modes, invalid identities/refs, unknown keys and unbounded trust. trusted_types are exact type hashes and trusted_actions a bounded list. Disabled examples normalize without opening placeholder paths and cannot dispatch. Team local gate off accepts no local commands; solo validation remains unchanged. Compare current safety ceiling against pinned policy without broadening approvals; changes require explicit migration/reapproval.
- [ ] **Verify:** Run python3 -m unittest tests.test_pr_repo_config -v; exit 0 with the named assertions passing in the existing authorized environment.
- [ ] **Hand off:** review only the listed scope diff and report ready_to_commit with a one-sentence subject; Drover commits/gates. Never self-publish or self-merge.

### Task #2: Persist PR records, observations, publications and poll state

**Size / risk:** M / high. **Dependencies:** #0; external prerequisites above.

**Files:** `drover/ledger/schema.py`, `drover/ledger/prs.py`, `tests/test_pr_storage.py`.

**Interfaces:** Produce link_pr(conn, task_id: str, ref: PRRef, relation: str, round_id: str | None = None) -> dict; save_observation(conn, ref: PRRef, snapshot: PRSnapshot, expected_revision: int) -> dict; save_publication(conn, draft: PublicationDraft, request_key: str) -> dict; projection(conn, task_id: str) -> PRProjection | None; due_polls(conn, now: str) -> list[dict]. Caller owns transaction; errors follow T9 conflict contracts. save_observation requires ref == snapshot["pr_ref"] before any write; persist that identity in snapshot JSON.

- [ ] **Pin the tests:** test_snapshot_target: mismatched ref and snapshot.pr_ref rejects before any observation/event/cursor write; matching identity survives storage and projection round-trip. test_migrate: pre-PR T9 fixture migrates twice with stable IDs and PRAGMA foreign_key_check empty plus integrity_check=ok. test_uniques: second primary for a PR, duplicate action key with changed bytes and stale expected_revision conflict; identical observation replay adds no event. test_atomic_poll: transaction rollback retains prior snapshot/cursors after a failed page; immutable publication/receipt history rejects update/delete.
- [ ] **Observe the failure:** run `python3 -m unittest tests.test_pr_storage -v`; confirm the named missing behavior fails, not an unrelated environment error.
- [ ] **Implement:** Append next unused migration after landed T9; never edit applied entries. Add the five tables pr_records, task_pr_links, pr_observations, pr_publications and pr_poll_state from the design, indexes and unique primary-task/action/request/observation identities. Keep immutable draft versions and append-only approval/effect receipts in pr_publications records; mutable latest projections/poll cursors are separate. Record source run/task provenance, policy hashes and host budget without secrets. Reuse T9 events/outbox and owner-task mappings rather than a second queue. Preserve all legacy task IDs/events/receipts.
- [ ] **Verify:** Run python3 -m unittest tests.test_pr_storage -v; exit 0 with the named assertions passing in the existing authorized environment.
- [ ] **Hand off:** review only the listed scope diff and report ready_to_commit with a one-sentence subject; Drover commits/gates. Never self-publish or self-merge.

### Task #3: Delegate one reserved PR fix scope to an isolated response child

**Size / risk:** M / high. **Dependencies:** #0, #2; external prerequisites above.

**Files:** `drover/prs/reservations.py`, `tests/test_pr_reservations.py`.

**Interfaces:** Produce delegate_scope(conn, parent_id: str, child_id: str, paths: list[str], expected_revision: int, approval: dict) -> Delegation; return_scope(conn, delegation_id: str, evidence: dict) -> dict; active_writer(conn, pr_ref: PRRef) -> str | None. Adapt T9 reservation operations inside these transactions; do not add a second reservation database.

- [ ] **Pin the tests:** test_delegate: overlapping unrelated task stays excluded while parent waits and child writes; child cannot claim paths outside approved subset. test_race: two children racing produce exactly one active writer; stale parent revision and wrong PR head fail. test_return: failed publication retains delegation; verified effect or safe cancellation returns it once; restart retains union reservation.
- [ ] **Observe the failure:** run `python3 -m unittest tests.test_pr_reservations -v`; confirm the named missing behavior fails, not an unrelated environment error.
- [ ] **Implement:** Atomically suspend parent writes and delegate only an owner-approved subset to one child; reserve union against unrelated tasks even during owner/external waits. Require fresh owned child worktree/branch from the observed PR head. Never authorize touching the parent worktree or shared branch directly. Return scope only on verified publication or safe cancellation cleanup; preserve parent reservation until final merge/cleanup. Make repeated requests idempotent and refuse active second writers or wider scope.
- [ ] **Verify:** Run python3 -m unittest tests.test_pr_reservations -v; exit 0 with the named assertions passing in the existing authorized environment.
- [ ] **Hand off:** review only the listed scope diff and report ready_to_commit with a one-sentence subject; Drover commits/gates. Never self-publish or self-merge.

### Task #4: Extend the T9 kernel for guarded external delivery

**Size / risk:** M / high. **Dependencies:** #0, #2, #3; external prerequisites above.

**Files:** `drover/tasktypes/schema.py`, `drover/tasktypes/lifecycle.py`, `drover/ledger/workflow.py`, `drover/ledger/actions.py`, `tests/test_pr_kernel.py`.

**Interfaces:** Extend T9 normalize_type, lifecycle validation and guarded transitions. Produce validate_pr_evidence(task: dict, target_state: str, evidence: dict) -> None in drover/ledger/workflow.py; register observe-pr/draft-publication/publish-approved action descriptors in drover/ledger/actions.py. Consume reservation APIs from #3 and receipt dictionaries from #0.

- [ ] **Pin the tests:** test_graphs: arbitrary executors/effects, missing publication/merge guards and direct running->landed bypass paths fail; standard-v1 remains valid. test_receipts: local receipt or artifact cannot complete github-pr; stale CI/review fails; verified squash merge records distinct task head and merge SHA. test_rounds: pr-update success never inserts task_landed or completes primary; owner merge submit without external evidence fails; service wait holds scope but consumes no seat.
- [ ] **Observe the failure:** run `python3 -m unittest tests.test_pr_kernel -v`; confirm the named missing behavior fails, not an unrelated environment error.
- [ ] **Implement:** Allow executor=github-sync with no LLM routing, github-pr and pr-update delivery, and bounded service-managed external waits. States still map to existing fixed phases; phase alone cannot imply a worker run. Completion of primary github-pr requires a queue-recorded verified merge receipt plus required evidence; auxiliary pr-update completes only its response round and never task_landed. Merge-unverified stays blocked. Human reviews cannot be replaced by agent LAND. Extend action guards to validate current approval, type/repo policy, head/base, artifact and effect receipts. Add special owner merge lifecycle actor rules: only adapter evidence completes open->merged; owner button cannot. Retain all solo and T9 bypass protections.
- [ ] **Verify:** Run python3 -m unittest tests.test_pr_kernel -v; exit 0 with the named assertions passing in the existing authorized environment.
- [ ] **Hand off:** review only the listed scope diff and report ready_to_commit with a one-sentence subject; Drover commits/gates. Never self-publish or self-merge.

### Task #5: Ship agent PR type, lifecycle and brief definitions

**Size / risk:** M / low. **Dependencies:** #4; external prerequisites above.

**Files:** `drover/task_types/open-pr.yaml`, `drover/task_types/review-pr.yaml`, `drover/task_types/respond-to-review.yaml`, `drover/task_types/pr-docket.yaml`, `drover/task_types/sync.yaml`, `drover/lifecycles/team-pr-v1.yaml`, `drover/lifecycles/pr-action-v1.yaml`, `drover/lifecycles/pr-update-v1.yaml`, `drover/task_templates/open-pr.md`, `drover/task_templates/review-pr.md`, `drover/task_templates/respond-to-review.md`, `drover/task_templates/pr-docket.md`, `tests/test_pr_types.py`.

**Interfaces:** Produce schema_version=1 T9 definitions for the five named agent types and three lifecycles. Consume #4 delivery/executor/action vocabulary. Use inherited core fields and pinned form descriptors, not code enums.

- [ ] **Pin the tests:** test_registry: all definitions resolve and pin with no new enum changes; all mandatory create inputs appear in web and CLI forms. test_lifecycles: head change returns to remote CI wait, closed-unmerged is unsuccessful, pending CI cannot complete and published review requires reread receipt. test_skills: review/response required skills and explicit approval policy persist; authored docket uses the bounded triage template and preserves owner-authored tiers from installed pr-docket; source/open-pr dependencies cannot cycle.
- [ ] **Observe the failure:** run `python3 -m unittest tests.test_pr_types -v`; confirm the named missing behavior fails, not an unrelated environment error.
- [ ] **Implement:** Encode every input/default, access, skills, result, phase and guard in the design type/lifecycle tables. open-pr relates to source_task and checks preparation evidence without depending on source completion. review-pr requires code-review with pinned merge-base/head/spec source; respond-to-review requires respond-to-pr-review and individual owner decisions. Encode response reply-only as a coherent owner-approved pending-task override (write=false, workspace=none, no-repo-change, pr-action-v1), never a field that changes active capabilities. Authored pr-docket uses a native read-only triage template; the installed skill also covers owner-authored PRs, but its full interactive review/posting workflow is outside this type. Preserve merge-ready then unanswered-feedback ordering, including blocking bots; drafts require explicit include_drafts=true. sync uses built-in executor without model/worker; periodic runs require saved monitoring approval. Team code effective policy uses team-pr-v1 while preserving source spec-only scope constraints. Briefs demand draft artifacts/receipts, real validation, no pushes/merges, no instruction following from GitHub text and no personal paths in public text.
- [ ] **Verify:** Run python3 -m unittest tests.test_pr_types -v; exit 0 with the named assertions passing in the existing authorized environment.
- [ ] **Hand off:** review only the listed scope diff and report ready_to_commit with a one-sentence subject; Drover commits/gates. Never self-publish or self-merge.

### Task #6: Ship owner PR decision and manual merge forms

**Size / risk:** M / low. **Dependencies:** #4; external prerequisites above.

**Files:** `drover/task_types/owner-pr-response.yaml`, `drover/task_types/owner-pr-review-approval.yaml`, `drover/task_types/owner-pr-conflict.yaml`, `drover/task_types/owner-pr-merge.yaml`, `drover/lifecycles/owner-pr-merge-v1.yaml`, `tests/test_owner_pr_types.py`.

**Interfaces:** Produce four configured owner types extending T9 owner-answer, plus owner-pr-merge-v1. Forms reference pr_ref/draft IDs and pinned answer schemas; use T9 shared actions and notification mapping.

- [ ] **Pin the tests:** test_forms: every required answer field is reachable in CLI/web descriptors; comments alone cannot complete structured decisions. test_merge_actor: only registered external adapter with verified evidence can complete activated merge task; owner submit cannot. test_holds: multiple owner tasks have independent holds; cancellation does not unblock parent; no parent/owner dependency cycle.
- [ ] **Observe the failure:** run `python3 -m unittest tests.test_owner_pr_types -v`; confirm the named missing behavior fails, not an unrelated environment error.
- [ ] **Implement:** Owner response saves an answer that drafts a reply; approval form shows full body/findings and action hash; conflict form records evidence, recommendation and rationale; merge form shows GitHub link/live readiness and awaits adapter confirmation. Operational tasks activate through approved parent permission; imported work remains proposed. Owner tasks consume no seats, create no runs, and never depend on their blocked parent. Edits create new draft versions. Merge completion cannot come from an owner checkbox or fabricated SHA.
- [ ] **Verify:** Run python3 -m unittest tests.test_owner_pr_types -v; exit 0 with the named assertions passing in the existing authorized environment.
- [ ] **Hand off:** review only the listed scope diff and report ready_to_commit with a one-sentence subject; Drover commits/gates. Never self-publish or self-merge.

### Task #7: Implement bounded hub gh reads and normalized snapshots

**Size / risk:** M / low. **Dependencies:** #0; external prerequisites above.

**Files:** `drover/prs/github.py`, `tests/test_pr_github.py`.

**Interfaces:** Produce GitHubClient(run, executable: str, host: str); read_pr(ref: PRRef, poll_state: PollState) -> tuple[PRSnapshot, PollState]; read_auth_binding() -> dict; preflight(draft: PublicationDraft) -> PublicationContext; mutate(draft: PublicationDraft) -> PublicationReceipt. mutate is transport only, called solely by #10 after approval; no generic endpoint/body from task text.

- [ ] **Pin the tests:** test_snapshot_identity: read_pr returns snapshot.pr_ref equal to the requested registered ref; missing or different response identity, including a different repo with the same PR number, rejects the whole generation. test_pagination: nested multi-page threads/reviews and status/check sources all appear once; second-page error discards new cursors. test_transport: hostile quotes/newlines/dollar syntax are JSON bytes, never shell; REST parameters retain explicit GET; disallowed GraphQL mutation is rejected. test_errors: 401/403/404, timeout, primary/secondary limits, Retry-After, 304 and null mergeability remain distinct normalized outcomes; changed ending head rejects snapshot.
- [ ] **Observe the failure:** run `python3 -m unittest tests.test_pr_github -v`; confirm the named missing behavior fails, not an unrelated environment error.
- [ ] **Implement:** Use hub Windows gh with explicit host/repo and argv/JSON stdin, fixed REST GET endpoints and GraphQL query allowlist; paginate all outer/nested collections. Normalize PR identity into required snapshot.pr_ref (host/repository/number), validate it against the requested ref and registry, then normalize head/base, author_login, mergeable, unanswered_feedback (comment ID, author kind, blocking flag, oldest unanswered timestamp), issue/inline comments/reviews/checks/rules/merge metadata and unknown permissions. Unanswered status requires observed reply/decision evidence; uncertain history remains unknown. Read beginning/end identity/head; reject mixed generations, bounded output/timeouts, malformed JSON and partial pagination. Return headers/budget/ETag state, do not persist secrets. Handle human/agent attribution without guessing. Mutation endpoint allowlist supports only four action kinds; no merge/resolve/dismiss. No dependency, token copying or shell interpolation. Provide fake-run injection for every call.
- [ ] **Verify:** Run python3 -m unittest tests.test_pr_github -v; exit 0 with the named assertions passing in the existing authorized environment.
- [ ] **Hand off:** review only the listed scope diff and report ready_to_commit with a one-sentence subject; Drover commits/gates. Never self-publish or self-merge.

### Task #8: Reconcile PR state, required CI, reviews and rate-aware polls

**Size / risk:** M / high. **Dependencies:** #1, #2, #4, #7; external prerequisites above.

**Files:** `drover/prs/sync.py`, `tests/test_pr_sync.py`.

**Interfaces:** Produce observe_due(conn, client_factory, now: str, shadow: bool = False) -> list[dict]; reconcile_pr(conn, snapshot: PRSnapshot, expected_revision: int) -> dict; evaluate_readiness(snapshot: PRSnapshot, repo_policy: dict) -> dict. Use #2 storage, #7 client and #4 guarded adapter transitions; inject clock/jitter. reconcile_pr resolves the canonical PR record and linked tasks from snapshot["pr_ref"]; never infer identity from head SHA, PR number alone or caller state.

- [ ] **Pin the tests:** test_snapshot_routing: snapshots sharing a PR number and head SHA across registered repositories update only their own records/linked tasks via snapshot.pr_ref; replay remains idempotent. test_gate_matrix: pending/missing/canceled/stale checks, unreadable requirements and empty sets block; explicit empty exemption is recorded; human approval succeeds, agent approval alone fails. test_observation: edited/deleted comments, review dismissal, new head, queue group, squash merge, close/reopen and duplicate pages yield the specified single transitions/round requests. test_backoff: fake clock verifies intervals/cap/jitter, host budget and stale UI snapshot; restart or explicit refresh cannot bypass Retry-After; partial poll and shadow mode never advance workflow.
- [ ] **Observe the failure:** run `python3 -m unittest tests.test_pr_sync -v`; confirm the named missing behavior fails, not an unrelated environment error.
- [ ] **Implement:** Coalesce linked tasks per PR, persist 30/120/600s intervals +/-20% jitter and exponential failures capped at 30m. Honor actual host budgets/reset/Retry-After and do not let refresh clear backoff. Require every applicable required check plus configured extras at current tested revision/app; missing/unknown/empty rules are not green. Empty requires explicit exemption; retain neutral/skipped server-rule semantics. Require eligible non-agent human approval plus repo counts/codeowner/thread rules. Invalidate readiness on head/base/rule/review changes; track merge queues/group SHA. Persist observations/cursors transactionally and deduplicate round/owner proposals. Record squash/rebase merges with separate head and merge SHA, merged-unverified if evidence missing, closed-unmerged unsuccessful; never resurrect terminal tasks. Shadow writes only projections/history, no tasks/actions/transitions.
- [ ] **Verify:** Run python3 -m unittest tests.test_pr_sync -v; exit 0 with the named assertions passing in the existing authorized environment.
- [ ] **Hand off:** review only the listed scope diff and report ready_to_commit with a one-sentence subject; Drover commits/gates. Never self-publish or self-merge.

### Task #9: Bind exact publication drafts to authenticated owner approvals

**Size / risk:** M / high. **Dependencies:** #1, #2, #4, #6; external prerequisites above.

**Files:** `drover/prs/approvals.py`, `drover/owner_actions.py`, `tests/test_pr_approvals.py`.

**Interfaces:** Produce store_draft(conn, task: dict, proposal: dict, source: dict) -> PublicationDraft; authorize_publication(conn, principal: dict, draft_id: str, expected_revision: int, request_key: str) -> dict; require_current_approval(conn, draft: PublicationDraft, live: PublicationContext) -> dict. Extend existing perform_action registry; no new auth principal or worker approval path.

- [ ] **Pin the tests:** test_authority: worker/run principals cannot approve; authenticated owner can approve exact draft once; edited body/head/base/policy produces 409. test_trust: exact permitted type/action yields policy receipt; name/wildcard, merge, stronger skill approval, owner hold and disabled repo reject direct publication. test_decisions: reply answer creates draft without post; conflicting review decision removes only its hold; two concurrent approvals preserve one action intent.
- [ ] **Observe the failure:** run `python3 -m unittest tests.test_pr_approvals -v`; confirm the named missing behavior fails, not an unrelated environment error.
- [ ] **Implement:** Hub assigns draft IDs/hashes and binds assigned task/run/artifact/commit evidence. Always draft push/create/review/reply; default always-ask uses owner tasks. Trusted mode authorizes exact type-hash/action grants with audited policy receipt, only if stricter task/skill requirements permit. Bind body, refs, head/base/remote-old, policy/type version and task revision; edits or scope/target changes invalidate approval. Single-comment skill decisions stay separate. Cancel/hold blocks effects. Worker bearer/run artifacts cannot approve themselves, change repo config or mint owner authority. Use existing owner-auth/CSRF/CAS/idempotency service; no network mutation here.
- [ ] **Verify:** Run python3 -m unittest tests.test_pr_approvals -v; exit 0 with the named assertions passing in the existing authorized environment.
- [ ] **Hand off:** review only the listed scope diff and report ready_to_commit with a one-sentence subject; Drover commits/gates. Never self-publish or self-merge.

### Task #10: Execute approved effects with non-force Git and crash reconciliation

**Size / risk:** M / high. **Dependencies:** #3, #7, #8, #9; external prerequisites above.

**Files:** `drover/prs/effects.py`, `drover/workflow_runner.py`, `tests/test_pr_effects.py`.

**Interfaces:** Produce execute_publication(conn, draft_id: str, client: GitHubClient, git_run) -> PublicationReceipt; recover_publication(conn, draft_id: str, client: GitHubClient, git_run) -> dict. Register publish-approved in T9 workflow_runner using its durable outbox/request keys. Mutations happen only through this trusted executor.

- [ ] **Pin the tests:** test_push: temporary remotes prove ordinary ff succeeds while default branch, foreign pushurl, divergent history, advanced remote and missing identity fail without force or history changes. test_approval_race: changed live head/base/comment/open state or canceled task blocks write; malicious public text preserves exact newlines. test_crash_each_effect: crash after successful push/create/review/reply recovers one receipt without duplicate effects; ambiguous or multiple matches become needs-owner.
- [ ] **Observe the failure:** run `python3 -m unittest tests.test_pr_effects -v`; confirm the named missing behavior fails, not an unrelated environment error.
- [ ] **Implement:** Persist intent, reread PR/ref/rules and validate current approval immediately before each effect; write exact JSON text or explicit reviewed SHA/ref. Verify configured fetch and push URL host/repository and reject target default/protected refs, foreign fork writes, missing approved commit identity and remote-old/head mismatch. Require fast-forward ancestry and use standalone non-force push; no implicit push defaults or published-history rebase. Never invoke merge/resolve/dismiss/delete. Reread results and store external IDs/hash/URLs. Reconcile crashes after write by marker/content/head/thread identity or remote SHA; exactly one provable match records receipt, otherwise owner hold with no retry. A partially completed push/create sequence revalidates remaining effects.
- [ ] **Verify:** Run python3 -m unittest tests.test_pr_effects -v; exit 0 with the named assertions passing in the existing authorized environment.
- [ ] **Hand off:** review only the listed scope diff and report ready_to_commit with a one-sentence subject; Drover commits/gates. Never self-publish or self-merge.

### Task #11: Draft and link task-branch pull requests

**Size / risk:** M / low. **Dependencies:** #8, #9; external prerequisites above.

**Files:** `drover/prs/open_pr.py`, `tests/test_open_pr.py`.

**Interfaces:** Produce prepare_open_pr(conn, task: dict, source_task: dict, snapshot: dict) -> list[PublicationDraft]. Return ordered push-branch and create-pr drafts using #9; after receipt use #2 link_pr for one primary relation and complete only the open-pr auxiliary task.

- [ ] **Pin the tests:** test_body: observed test evidence and task/acceptance marker are present; unrun tests are identified accurately; caller text cannot select a new repo or ref. test_link: one primary plus one open-pr auxiliary link is created; completing open-pr does not complete primary or unblock its implementation dependents. test_recovery: matching marker/head/base reuses one PR receipt; multiple matches or moved head require owner decision.
- [ ] **Observe the failure:** run `python3 -m unittest tests.test_open_pr -v`; confirm the named missing behavior fails, not an unrelated environment error.
- [ ] **Implement:** Validate approved source preparation and registered remote/base/head; derive title/body from task goal, acceptance, diff and observed validation with stable task marker and draft=true default. Existing matching PR is adopted only with unambiguous identity/owner permission, never duplicated on restart. Initial push and create have separate receipts and previews; no source-completion dependency. Keep source primary waiting for GitHub merge. Do not claim checks that did not run or expose private paths; no GitHub writes from drafting.
- [ ] **Verify:** Run python3 -m unittest tests.test_open_pr -v; exit 0 with the named assertions passing in the existing authorized environment.
- [ ] **Hand off:** review only the listed scope diff and report ready_to_commit with a one-sentence subject; Drover commits/gates. Never self-publish or self-merge.

### Task #12: Turn pinned PR reviews into draft findings and review actions

**Size / risk:** M / low. **Dependencies:** #8, #9; external prerequisites above.

**Files:** `drover/prs/reviews.py`, `tests/test_review_pr.py`.

**Interfaces:** Produce review_context(snapshot: PRSnapshot, spec_source: dict) -> dict; prepare_review(conn, task: dict, artifact: dict, snapshot: PRSnapshot) -> PublicationDraft. Context identifies exact merge-base/head and immutable read snapshot for required code-review.

- [ ] **Pin the tests:** test_context: fork immutable snapshot and exact fixed point/spec reach skill brief; missing skill/spec produces owner work and no post. test_draft: approved full findings/event/positions bind artifact/head/base; new head or hunk makes original approval unusable. test_no_self_approve: owner-authored PR cannot generate an approving owner review; analysis completion without publication receipt cannot mark review-pr published.
- [ ] **Observe the failure:** run `python3 -m unittest tests.test_review_pr -v`; confirm the named missing behavior fails, not an unrelated environment error.
- [ ] **Implement:** Use installed code-review for standards/spec analysis and its admission-approved review subagents; verify major claims against pinned source. No write adoption needed for fork reads. Missing skill or ambiguous spec source becomes owner work. Normalize full findings, review event and inline positions from accepted T9 artifact; source hash, head/base and finding IDs persist. Formal owner review of an owned PR cannot self-approve. Require explicit review approval when skill requires it even under trusted policy. A stale hunk/head forces redraft, not silent re-anchoring; receipt reread is required for auxiliary success.
- [ ] **Verify:** Run python3 -m unittest tests.test_review_pr -v; exit 0 with the named assertions passing in the existing authorized environment.
- [ ] **Hand off:** review only the listed scope diff and report ready_to_commit with a one-sentence subject; Drover commits/gates. Never self-publish or self-merge.

### Task #13: Prepare individual review responses and scoped PR update rounds

**Size / risk:** M / low. **Dependencies:** #3, #8, #9; external prerequisites above.

**Files:** `drover/prs/responses.py`, `tests/test_respond_to_review.py`.

**Interfaces:** Produce response_context(task: dict, snapshot: PRSnapshot) -> dict; prepare_response(conn, task: dict, worker_record: dict, snapshot: PRSnapshot) -> list[PublicationDraft]. Consume validated pr_drafts proposals from #0; dispatcher supplies preserved records after #15 lands.

- [ ] **Pin the tests:** test_intake: all three comment sources are handled once, including issue-comment pushback and thread replies; classification covers each requested ID. test_scope: two fixes cannot exceed delegated subset; foreign head/branch advance or conflicting reviewer decisions hold for owner with no rewrite. test_round: reply-only needs no SHA, code update waits for CI and per-thread receipts, ambiguous comment stays pending; completed response leaves primary awaiting merge.
- [ ] **Observe the failure:** run `python3 -m unittest tests.test_respond_to_review -v`; confirm the named missing behavior fails, not an unrelated environment error.
- [ ] **Implement:** Fetch inline threads, formal reviews and issue comments via normalized snapshot; classify every requested comment fix/clarify/dispute/reply-only with verified rationale. Invoke installed respond-to-pr-review; translate individual decision/push pauses into durable owner work. Approved fixes use delegated subset and fresh owned child branch from PR head; preserve shared history, no direct worker push. Require local minima/optional review evidence for commit, approved ff push then current CI, and individually approved replies. Do not resolve threads automatically. Reply-only requires an approved pending-task override to write=false/workspace=none/no-repo-change/pr-action-v1 before launch; an active write task cannot switch policy in place or complete without a commit. If intake discovers reply-only after launch, propose the properly configured continuation. No fake commit. New comments/head during active work create pending next-round proposals; response success never completes primary.
- [ ] **Verify:** Run python3 -m unittest tests.test_respond_to_review -v; exit 0 with the named assertions passing in the existing authorized environment.
- [ ] **Hand off:** review only the listed scope diff and report ready_to_commit with a one-sentence subject; Drover commits/gates. Never self-publish or self-merge.

### Task #14: Rank owner-authored PR work with bounded docket triage

**Size / risk:** S / low. **Dependencies:** #8; external prerequisites above.

**Files:** `drover/prs/docket.py`, `tests/test_pr_docket.py`.

**Interfaces:** Produce rank_authored_prs(snapshots: list[PRSnapshot], owner_binding: dict, include_drafts: bool = False) -> list[dict]. Result is T9 result artifact with ordered PR refs, reasons and proposed next actions; no state-changing GitHub call.

- [ ] **Pin the tests:** test_snapshot_identity: same-number/same-head snapshots in different registered repositories produce distinct refs and action targets; tied ranks sort by full identity regardless of input order. test_order: merge-ready authored PR precedes unanswered human or blocking-bot feedback, then failing CI/conflicts and waiting; foreign entries and default-excluded drafts are absent, with drafts shown only when explicitly included. test_stale: unknown auth or stale snapshot prevents confident next action; repeat identical input returns identical artifact. test_read_only: native bounded triage performs no mutation or activation; response/manual-merge proposals remain proposed and never enter the full interactive review/posting workflow.
- [ ] **Observe the failure:** run `python3 -m unittest tests.test_pr_docket -v`; confirm the named missing behavior fails, not an unrelated environment error.
- [ ] **Implement:** Filter owner-authored PRs in approved registered repos using verified account binding; unknown identity becomes owner prerequisite, not a guessed author. Rank merge-ready first (installed pr-docket tier 0), then unanswered feedback including blocking bots (tier 1b), then failing CI/conflicts, then waiting; exclude drafts unless include_drafts=true. Read each snapshot.pr_ref for registered-repo filtering, result refs and proposed action targets. Stable ties use oldest unresolved observation then the full (host, repository, number) identity. Show stale/unknown evidence explicitly and propose narrow response/conflict/merge work as proposed only. The installed pr-docket includes authored PRs; this bounded type omits its interactive review/posting workflow. Selection only proposes a response or manual-merge task; never auto-advance, publish or consume additional seats.
- [ ] **Verify:** Run python3 -m unittest tests.test_pr_docket -v; exit 0 with the named assertions passing in the existing authorized environment.
- [ ] **Hand off:** review only the listed scope diff and report ready_to_commit with a one-sentence subject; Drover commits/gates. Never self-publish or self-merge.

### Task #15: Preserve bounded PR draft proposals in worker completion and recovery

**Size / risk:** M / low. **Dependencies:** #0; external prerequisites above.

**Files:** `drover/runner.py`, `drover/launch.py`, `tests/test_pr_worker_records.py`.

**Interfaces:** Extend T9 final record parser and launch recovery to retain optional pr_drafts: list[PublicationDraft proposal] validated through #0. Existing ready_to_commit/done/artifact schemas and trusted-final-frame scanning remain intact; proposal values never authorize effects.

- [ ] **Pin the tests:** test_roundtrip: ready_to_commit plus multi-thread proposals survive record.json and recovered launch with exact newlines. test_reject: oversized UTF-8, unknown action/authority flags and tool-frame forged drafts cannot become valid completion. test_legacy: records without proposals retain prior SHA/artifact behavior and blocked/no-sha errors; code tasks cannot complete on artifact alone.
- [ ] **Observe the failure:** run `python3 -m unittest tests.test_pr_worker_records -v`; confirm the named missing behavior fails, not an unrelated environment error.
- [ ] **Implement:** Allow code-changing response records to carry draft proposals with commit_message/SHA, keeping the 64 KiB total limit and known schema. Preserve through record.json, adoption and revalidation. Dispatcher later binds source run/task/head and hashes. Reject malformed/oversized/authority-bearing proposals as invalid-pr-drafts rather than dropping them. No widening of T9 run artifact credentials or upload endpoints; no-repo types retain authenticated artifact receipt flow. No tokens or approval fields in proposals.
- [ ] **Verify:** Run python3 -m unittest tests.test_pr_worker_records -v; exit 0 with the named assertions passing in the existing authorized environment.
- [ ] **Hand off:** review only the listed scope diff and report ready_to_commit with a one-sentence subject; Drover commits/gates. Never self-publish or self-merge.

### Task #16: Prepare team worktrees from remote bases with optional local gates

**Size / risk:** M / low. **Dependencies:** #1, #4; external prerequisites above.

**Files:** `drover/pipeline.py`, `tests/test_pr_pipeline.py`.

**Interfaces:** Extend Pipeline.base_ref, gate_known, gate_plan and worktree preparation to consume effective_delivery from #1 and T9 pinned task policy. Add Pipeline.delivery_plan(task: dict) -> dict, returning the #1 normalized mode/local gate/review requirements plus fetched base SHA.

- [ ] **Pin the tests:** test_remote_base: remote ahead plus unpushed local main produces team branch at fetched remote SHA while local main/index/worktree remain unchanged; solo regression still includes local landings. test_gate: local off with unknown command does not prompt; explicit full/high-risk floor requires real command and failure cannot masquerade as remote CI success. test_isolation: disabled machine placeholder never opens a path; foreign parent worktree and stale PR head cannot be adopted as response workspace.
- [ ] **Observe the failure:** run `python3 -m unittest tests.test_pr_pipeline -v`; confirm the named missing behavior fails, not an unrelated environment error.
- [ ] **Implement:** Team creation fetches approved remote-tracking target and branches from that SHA without origin.sync advancing owner local main; solo retains current sync/base behavior. Validate disabled registration before touching placeholder paths. Team local gate off returns explicit local exemption and never asks unknown-local-gate question; scoped/full and explicit task/high-risk minima still require actual commands and exact commit evidence. Reuse scoped commit and clean owned worktree checks. Response worktrees start from verified PR head and own branch, not another task's worktree. No push/main write or credentials.
- [ ] **Verify:** Run python3 -m unittest tests.test_pr_pipeline -v; exit 0 with the named assertions passing in the existing authorized environment.
- [ ] **Hand off:** review only the listed scope diff and report ready_to_commit with a one-sentence subject; Drover commits/gates. Never self-publish or self-merge.

### Task #17: Dispatch PR actions and service waits without granting worker publication

**Size / risk:** M / high. **Dependencies:** #5, #8, #10, #11, #12, #13, #14, #15, #16; external prerequisites above.

**Files:** `drover/dispatcher.py`, `drover/port.py`, `drover/pinned.md`, `tests/test_pr_dispatcher.py`.

**Interfaces:** Integrate T9 DirectPort/action runner with prepared PR results, #8 reconcile, #10 effect handler and #11-14 draft/triage services. Use Pipeline.delivery_plan and type executor/capability descriptors; do not switch on literal task names or launch workers merely from execution_phase.

- [ ] **Pin the tests:** test_flow: team local-off/agent-review-off prepares publication then waits; CI and human evidence cannot be replaced by worker done/LAND. test_waits: sync/owner/external waits consume no worker slot or fake run; recovery preserves reservation, assigned proposal provenance and one action intent. test_solo: existing SHA/local-gate/reviewer/landing route is unchanged; missing skill and malformed drafts stop rather than silently skip.
- [ ] **Observe the failure:** run `python3 -m unittest tests.test_pr_dispatcher -v`; confirm the named missing behavior fails, not an unrelated environment error.
- [ ] **Implement:** Keep primary github-pr prepared and pending until external CI/human/merge receipts. Route optional local stages accurately; independent agent review is optional for team while mandatory human review remains. Persist validated worker proposals bound to assigned run/head; no-repo draft artifacts require T9 authenticated receipts. Built-in sync uses no model/run/seat. Owner and external waits release worker leases while retaining/delegating reservations. Recovery resumes pending outbox/evidence without duplicate workers or publications. Update pinned text to distinguish trusted hub team delivery from local solo queue; workers still cannot push, merge or touch main. Missing required skill creates owner prerequisite, never installation.
- [ ] **Verify:** Run python3 -m unittest tests.test_pr_dispatcher -v; exit 0 with the named assertions passing in the existing authorized environment.
- [ ] **Hand off:** review only the listed scope diff and report ready_to_commit with a one-sentence subject; Drover commits/gates. Never self-publish or self-merge.

### Task #18: Separate team delivery from local landing and record verified merges

**Size / risk:** M / high. **Dependencies:** #8, #10, #16; external prerequisites above.

**Files:** `drover/landing.py`, `hub/dispatch.py`, `tests/test_pr_landing.py`.

**Interfaces:** Produce record_pr_delivery(conn, task_id: str, receipt: dict) -> dict in drover/landing.py, using #4 kernel receipt validation. LandingService selects resolved delivery capabilities and runs bounded due PR observation every <=5s through existing loop timing; local _land_repo is solo-only. Retain a separate persisted/monotonic solo next-run deadline at the existing 120s cadence instead of running solo gates every polling tick.

- [ ] **Pin the tests:** test_no_main: team queue, CLI/direct run_batch and paused/disabled paths never call local ff, batch gate or main push; registered identity prevents omitted-name bypass. test_merge: open PR is unfinished, squash/rebase merge records distinct SHA once, merged-unverified holds, closed unmerged is unsuccessful. test_solo_regression: run existing landing and hub dispatch tests unchanged, including checked-out main CAS and legacy explicit push cases; fake-clock assertions prove <=5s PR scheduling does not increase the 120s solo landing cadence.
- [ ] **Observe the failure:** run `uv run --project hub --no-sync python -m unittest tests.test_pr_landing tests.test_landing tests.test_hub_dispatch -v`; confirm the named missing behavior fails, not an unrelated environment error.
- [ ] **Implement:** Team tasks never call build_tip/run_batch/local_fast_forward/origin.push_main. Guard local CLI/direct queue entry points by registered repo identity/current-or-pinned mode, so omission of repo_name cannot bypass team policy. Observe verified GitHub merge and insert external delivery evidence with task head + merge SHA; never fabricate local batch receipt or mark open/closed-unmerged landed. Preserve current solo batch/full-gate/explicit push behavior and T-2 sync. Dispatch pause stops publications but approved monitoring can continue; disabled GitHub cannot fall back locally. Keep one existing service thread; schedule bounded ticks instead of new service, honor due/backoff times.
- [ ] **Verify:** Run uv run --project hub --no-sync python -m unittest tests.test_pr_landing tests.test_landing tests.test_hub_dispatch -v; exit 0 with the named assertions passing in the existing authorized environment.
- [ ] **Hand off:** review only the listed scope diff and report ready_to_commit with a one-sentence subject; Drover commits/gates. Never self-publish or self-merge.

### Task #19: Expose durable PR projections and owner actions through the hub

**Size / risk:** M / high. **Dependencies:** #8, #9, #10; external prerequisites above.

**Files:** `hub/pr_service.py`, `hub/pr_api.py`, `hub/app.py`, `hub/store.py`, `tests/test_pr_hub_api.py`.

**Interfaces:** Produce PRService(store).task_projection(task_id: str) -> PRProjection | None; request_refresh(pr_ref: PRRef, principal: dict, expected_revision: int) -> dict; register read/projection and bounded-refresh routes in hub/pr_api.py and app.py. Reuse T9 perform_action endpoints for approvals; do not invent a separate owner-auth channel.

- [ ] **Pin the tests:** test_projection: card/page/API use the same stored head/state/CI/review/stale/action data and perform zero gh calls during GET. test_push_main: owner and non-owner calls cannot push a team default branch; solo owner route still works. test_auth_cas: bearer/run credentials cannot approve; owner stale revision conflicts; repeated refresh deduplicates without changing Retry-After.
- [ ] **Observe the failure:** run `uv run --project hub --no-sync python -m unittest tests.test_pr_hub_api -v`; confirm the named missing behavior fails, not an unrelated environment error.
- [ ] **Implement:** Attach ledger-only PR projection and action descriptors to task_detail/card/inbox. Add GET /api/tasks/{tid}/pr and owner-action refresh scheduling with CAS/idempotency; no GitHub I/O while rendering or handling a GET. Refresh cannot clear rate backoff or approve monitoring/publication. Guard existing Store.push_main against team and omit its team action from landing view; keep solo behavior. All action mutations use existing owner principal/CSRF and T9 service, while worker/run credentials remain non-owner. Validate derived links/Markdown and avoid secret exposure. Return 403 unauthorized, 409 stale revision and 422 malformed values consistently.
- [ ] **Verify:** Run uv run --project hub --no-sync python -m unittest tests.test_pr_hub_api -v; exit 0 with the named assertions passing in the existing authorized environment.
- [ ] **Hand off:** review only the listed scope diff and report ready_to_commit with a one-sentence subject; Drover commits/gates. Never self-publish or self-merge.

### Task #20: Render PR state, exact drafts and manual owner merge work

**Size / risk:** M / low. **Dependencies:** #6, #19; external prerequisites above.

**Files:** `hub/task_views.py`, `hub/templates/_task_card.html`, `hub/templates/task.html`, `hub/templates/_pr_status.html`, `tests/test_pr_web.py`, `tests/test_pr_cli_parity.py`.

**Interfaces:** Consume #19 task PRProjection and T9 FormDescriptor/action descriptors. Add shared _pr_status.html partial and safe task view fields; use existing owner_cli/client generic show/answer/action behavior without editing their files.

- [ ] **Pin the tests:** test_render: open, queued, merged-unverified, stale outage and closed states render accurately without live fetch; malicious body/URL cannot inject HTML or credentials. test_owner_forms: full review/reply and exact commit range are visible before explicit approval; editing creates a new draft and old action cannot post. test_cli_parity: existing CLI show/answer/action fixtures and web descriptors expose identical values/actions/errors for all four owner types; manual merge confirmation cannot spoof receipt.
- [ ] **Observe the failure:** run `uv run --project hub --no-sync python -m unittest tests.test_pr_web tests.test_pr_cli_parity -v`; confirm the named missing behavior fails, not an unrelated environment error.
- [ ] **Implement:** Card shows safe PR link/number, draft/open/queued/merged/closed, CI and human-review state, next owner action and last-sync staleness. Page shows head/base/merge SHA, checks/reviews/thread links, linked primary/round/owner tasks and exact immutable drafts/approval/error history. Owner merge button/link opens GitHub; only observed merge completes. Edits invalidate existing draft approvals. Preserve local branch/worktree display and type forms. Render hostile Markdown safely and allow derived links only to registered GitHub host. Existing generic CLI descriptors must expose the same complete text, decisions, draft hashes and CAS conflicts.
- [ ] **Verify:** Run uv run --project hub --no-sync python -m unittest tests.test_pr_web tests.test_pr_cli_parity -v; exit 0 with the named assertions passing in the existing authorized environment.
- [ ] **Hand off:** review only the listed scope diff and report ready_to_commit with a one-sentence subject; Drover commits/gates. Never self-publish or self-merge.

### Task #21: Onboard solo or team repositories and ship a disabled machine example

**Size / risk:** M / low. **Dependencies:** #1; external prerequisites above.

**Files:** `drover/wizard.py`, `repos/machine.toml`, `tests/test_pr_wizard.py`.

**Interfaces:** Extend q_repos, answer-file parsing and repo_toml with the #1 normalized collaboration/github contract; retain existing wizard preview/confirmation/write behavior. Produce only disabled shipped example repos/machine.toml, not a private registration.

- [ ] **Pin the tests:** test_answers: old headless answers retain solo defaults; team local-off answers need no local command and never enable main push. test_rerun: mode, target, trust, checks and platform command overrides survive preview/reload; mode migration is never inferred from repo name. test_machine: example is valid disabled TOML with exact placeholders and no personal paths; enablement fails until completed; mocked Windows auth probe never uses WSL credentials.
- [ ] **Observe the failure:** run `python3 -m unittest tests.test_pr_wizard -v`; confirm the named missing behavior fails, not an unrelated environment error.
- [ ] **Implement:** Ask solo|team before gate/push. Explain solo local ff/push-off default and team PR/human merge, then collect registered host/repository/target, optional local gate, human minimum and publication policy. Team skips push-landed-work question. Defaults/legacy answer files stay solo; rerun preserves current mode/remote/base/trust/checks/platform overrides unless explicitly changed. Mode switch with unsettled tasks requires explicit migration choice before activation. machine example uses only design placeholders, sillsdev/machine, enabled=false, team/pr and no guessed branch/checks/credentials/identity. Owner must replace all placeholders and approve read-only Windows auth/rule probe before enabling. Do not run probes or contact GitHub from tests.
- [ ] **Verify:** Run python3 -m unittest tests.test_pr_wizard -v; exit 0 with the named assertions passing in the existing authorized environment.
- [ ] **Hand off:** review only the listed scope diff and report ready_to_commit with a one-sentence subject; Drover commits/gates. Never self-publish or self-merge.

### Task #22: Verify complete PR workflows and document guarded rollout

**Size / risk:** M / high. **Dependencies:** #5, #6, #17, #18, #20, #21; external prerequisites above.

**Files:** `docs/team-repos.md`, `tests/test_pr_workflows.py`.

**Interfaces:** Use public interfaces from #0-21, fake gh/clock and local temporary Git remotes in a T9 hub fixture. Produce owner operating guide docs/team-repos.md including mapping to design D1-D11 and external rollout prerequisites.

- [ ] **Pin the tests:** test_lifecycle_e2e: primary remains unfinished after open-pr or response completion; only verified merged receipt releases implementation dependents. test_recovery_e2e: restarts across push/create/review/reply plus pagination/backoff produce no duplicate effects or owner tasks; canceled/foreign-head work cannot publish. test_solo_e2e: legacy solo task still lands locally with push off, explicit push remains compatible; every design decision has an implementation/test or named rollout prerequisite.
- [ ] **Observe the failure:** run `uv run --project hub --no-sync python -m unittest tests.test_pr_workflows -v`; confirm the named missing behavior fails, not an unrelated environment error.
- [ ] **Implement:** Exercise solo default and explicit-push compatibility, team source->open-pr->CI->human-review->manual-merge, separate review/response rounds, owner decisions and restart at each effect boundary. Demonstrate default main never pushed, shared history never rewritten, unknown gate/rules never green and dependencies wait for primary merge. Include rate-limited stale polling, hostile comments, scope delegation, fork read/write split, merge queue, squash/rebase, cancel/reopen and disabled-mode recovery. Guide covers shadow mode, explicit migration of idle tasks, Windows gh/account/identity/rules check, approved pilot, rollback holds and no local fallback. No live GitHub writes or installations. Record Windows/pilot prerequisites honestly as pending owner rollout checks.
- [ ] **Verify:** Run uv run --project hub --no-sync python -m unittest tests.test_pr_workflows -v; exit 0 with the named assertions passing in the existing authorized environment.
- [ ] **Hand off:** review only the listed scope diff and report ready_to_commit with a one-sentence subject; Drover commits/gates. Never self-publish or self-merge.

## Rollout and handoff

- [ ] Finish #22 fixture gates and independent review before activation; a test receipt applies only to its gated SHA.
- [ ] Owner reviews D1-D11, replaces machine placeholders, verifies the Windows gh account, approved commit identity, target branch, check sources and human-review rules.
- [ ] Observe shadow projections first; no publication, owner-task creation or task advancement in shadow mode.
- [ ] Preview/reapprove any idle legacy task migration; drain active tasks on pinned old policy. Never change route by editing TOML alone.
- [ ] Owner explicitly authorizes one pilot PR and every outward effect under policy; confirm human merge, resulting receipt and dependency release before expanding.
- [ ] To disable, stop new publications and reconcile any in-flight effects; retain history/holds, never fall back to local team landing or down-migrate destructively.

The companion task JSON is the import source. This task produces documentation only; it does not authorize execution of this plan. Suggested execution is admission-controlled independent lanes after prerequisite contracts land, followed by the integrated #22 gate and owner rollout review.
