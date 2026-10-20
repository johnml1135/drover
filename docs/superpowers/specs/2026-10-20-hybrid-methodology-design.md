**Drover’s hybrid design methodology — 2026-10-20**

The three loops share artifacts and events: the person owns product behavior; the person and a large model settle architecture; Drover implements within those decisions.

This design replaces the single-format assumption and mirrored JSON contract in the earlier tranche. The workflow selector, escalation rules, and outstanding [tranche-1 review findings](<scratchpad/sol/workflow-t1-check.md>) remain applicable. Phase 3b’s `.drover/` layout supersedes earlier configuration paths.

**1. Artifact model**

Artifacts describe meaning; adapters determine representation. Several artifacts may occupy one document.

Every artifact has:

`id`, `change_id`, `kind`, `title`, `owner`, `source={path,selector}`, `sha256`, `approval`, `delivery`, `links`, `supersedes`, `fields`, `findings`.

Identity is repository-scoped and stable across renames, rebases, and handoffs. IDs are UUIDs, except numbered decisions may use `ADR-NNNN`. A revision is `(id, sha256)`; hashing covers original source-file bytes. Sharing a document conservatively invalidates all its extracted artifacts when that document changes.

| Kind | Additional fields |
|---|---|
| Intent | Problem, desired outcome, acceptance criteria, examples, non-goals, appetite |
| Decision | Choice, alternatives, rationale, consequences, scope, enforcement |
| Contract | Intent/decision references, behavior, interfaces, invariants, permitted changes, task references, verification mapping, risks |
| Task | Dependencies, read/write sets, deletions, changes, verification commands and expected results |
| Evidence | Subject artifact/revision, base and tested commit/tree, producer, method, result, exit code, findings, output references/digests |

Approval and delivery are separate axes:

- `approval: draft | accepted | superseded | rejected`
- `delivery: in-flight | implemented`

Acceptance never proves implementation. An accepted ADR may describe pending work. Evidence acceptance means the observation is valid; its result can still be failure. Supersession identifies the replacement without deleting history.

In-flight sources remain on the work branch. Accepted ADRs reach main immediately through the owner decision action. Living behavior specs and architecture updates reach main with their implementation. Historical sources enter the archive.

The ledger stores projections, event delivery, and runtime coordination. It is never the sole repository of design state. A contract’s task entries may execute as steps within one tracked Task, preserving the glossary’s one-repo, one-squash unit.

**2. Branch metadata and recovery**

Path: `.drover/changes/<change-id>.yaml`.

The following is the exact closed schema in YAML type notation. Quoted values denote types; angle-bracket keys denote maps. All fixed keys are required; unknown keys fail validation.

```yaml
schema_version: 1
change_id: UUID
title: nonempty-string
owner: nonempty-string
phase: "intent | design | ready | building | review | merged | abandoned"
base:
  created: OID
  integrated: OID
  adr_checked: OID
adapter:
  name: "superpowers | plain"
  version: "integer >= 1"
workflow: "null | {id: slug, revision: integer >= 1, snapshot_hash: H}"
write_set: "unique list[path-or-glob]"
event_heads: "unique list[UUID]"
artifacts:
  "<artifact-id>":
    kind: "intent | decision | contract | task | evidence"
    path: path
    selector: "string | null"
    sha256: H
    approval: "draft | accepted | superseded | rejected"
    delivery: "in-flight | implemented"
spec_bases:
  "<living-spec-path>":
    sha256: "H | null"
    delta: artifact-id
```

`OID` is a full Git object ID; `H` is lowercase SHA-256. Paths are canonical repository-relative POSIX paths without traversal. Globs support `*`, `?`, and `**`; exclusions are outside version 1. `null` spec hashes mean creation. Artifact IDs and delta references must resolve.

Example, with illustrative source hash:

```yaml
schema_version: 1
change_id: f565c3d8-5a11-45ad-b744-9f00c409c105
title: Improve owner questions
owner: johnm
phase: intent
base:
  created: 3cead8d580bd404a06b63b442cf7be337c150825
  integrated: 3cead8d580bd404a06b63b442cf7be337c150825
  adr_checked: 3cead8d580bd404a06b63b442cf7be337c150825
adapter: {name: superpowers, version: 1}
workflow: null
write_set: [drover/decisions.py, tests/test_decisions.py]
event_heads: []
artifacts:
  550e8400-e29b-41d4-a716-446655440000:
    kind: intent
    path: docs/superpowers/specs/2026-10-20-owner-questions-design.md
    selector: null
    sha256: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
    approval: draft
    delivery: in-flight
spec_bases: {}
```

Each transition commits source changes, metadata, and an immutable event under `.drover/events/<change-id>/<event-id>.yaml`. Trailers provide discovery:

```text
Drover-Change: <change-id>
Drover-Event: <event-id>
Drover-Transition: contract-ready
```

Events contain causal parent IDs. Concurrent incompatible transitions become conflicts; timestamps never choose a winner. Trailers and worker-authored approval fields confer no authority.

Tests and reviews produce evidence **after** their subject commit exists. The hub persists these records under `refs/drover/records/<change-id>`, bound to the exact subject commit, without changing its tested tree. Only authority-validated hub records establish approvals.

Rebuild procedure:

1. Enumerate registered branch refs, main, archive manifests, and record refs; pin observed tips while indexing.
2. Read committed metadata and referenced blobs; validate hashes and adapter versions.
3. Deduplicate events by ID and replay their causal graph.
4. Reconstruct artifact revisions, questions, decisions, phase, and evidence references.
5. Reconcile branch manifests with main archives; surface divergent heads or missing records.
6. Publish the regenerated active-design index.

Rebuild restores history without redelivering acknowledged effects. Execution resumes only after evidence and authority validation. Leases and running processes are reacquired through normal adoption. Backups must include record refs.

**3. Adapter interface and configuration**

All interfaces below are NEW. Adapters are pure: they consume committed blobs and return metadata or proposed byte edits. They perform no filesystem writes, Git operations, launches, approvals, or SQL.

```python
from typing import Literal, Mapping, Protocol

Kind = Literal["intent", "decision", "contract", "task", "evidence"]
Blobs = Mapping[str, bytes]
Location = tuple[str, str | None]       # path, heading selector
Edit = tuple[str, str | None, bytes]    # path, expected old hash, new bytes
Artifact = dict[str, object]

class ArtifactError(ValueError):
    def __init__(self, path: str, message: str): ...

class Adapter(Protocol):
    def locate(
        self, kind: Kind, metadata: dict, blobs: Blobs
    ) -> tuple[Location, ...]: ...

    def read(
        self, kind: Kind, location: Location,
        metadata: dict, blobs: Blobs
    ) -> Artifact: ...

    def write(
        self, artifact: Artifact, *, blobs: Blobs,
        expected_sha256: str | None
    ) -> Edit: ...

    def template(
        self, kind: Kind, *, level: int, blobs: Blobs
    ) -> bytes: ...

def get_adapter(config: dict) -> Adapter: ...
```

These operations support every `Kind`. `locate` returns sorted, explicit bindings; ambiguity fails. `read` returns source findings for missing semantic fields. Stage validation decides whether those findings prevent advancement.

`write` replaces only the selected authored region, preserving surrounding bytes, comments, fences, and line endings. A stale expected hash fails. The calling command commits edits, metadata, and events atomically. Templates create new documents; they never reformat existing ones.

Superpowers mapping:

| Source | Extraction |
|---|---|
| `docs/superpowers/specs/YYYY-MM-DD-<slug>-design.md` | Intent from `Problem and intent`, `Problem, intent and boundaries`, or explicit Problem/Outcome/Acceptance/Non-goals sections |
| Same design document | Contract behavior, Interfaces, Invariants, Requirements, Acceptance criteria |
| `docs/superpowers/plans/YYYY-MM-DD-<slug>.md` | L5 contract from Goal, Architecture decisions, Characterization, Tasks, Migration, Verification |
| Plan’s `Task N` sections | Dependencies, Files/Write set, Read set, Interfaces, Delete, Change, red/pass checks |
| `docs/adr/*.md` | Decisions from front matter and MADR sections |
| `.drover/evidence/<change-id>/*.yaml` | Structured evidence |

The supplied `plan-template.md` is Drover’s superpowers L5 plan profile. Native superpowers Files/Interfaces/Step headings remain readable. Missing required information prevents readiness. No duplicated authored JSON contract is maintained.

The plain adapter uses `.drover/changes/<id>/intent.md`, `contract.md`, and `tasks.md`, with literal field headings. It shares MADR decisions and structured evidence. Missing configuration selects plain; an unknown configured adapter fails.

Proposed config keys:

```toml
schema_version = 1

[design]
adapter = "superpowers"
adapter_version = 1
intake_skill = "grill-me"
adr_dir = "docs/adr"
accepted_specs = "docs/specs"
archive = "docs/superpowers/archive"
adr_mode = "shadow"                 # off | shadow | blocking
routine_batch_minutes = 30

[design.templates]
contract = ".drover/templates/implementation-contract.md"

[workflows]
policy = ".drover/workflows/policy.yaml"
```

`design.templates` permits exactly the five artifact kinds as optional path overrides. Defaults are versioned adapter templates. Plain’s default archive is `.drover/archive`.

The existing repo loader owns loading and provenance after Phase 3b. Snapshot hashes include adapter identity/version, config, templates, and referenced source bytes. Workflows remain `.drover/workflows/*.yaml`; scripts remain `.drover/scripts/*.ps1`. Machine config cannot override design or workflow policy.

**4. ADR metadata and impact checks**

MADR-compatible front matter:

```yaml
---
schema_version: 1
id: ADR-0004
status: proposed
date: "2026-10-20"
decision-makers: [johnm]
consulted: []
informed: []
applies_to: ["drover/**", "hub/**", ".drover/**"]
enforcement: null
supersedes: []
links: []
---
```

Required fields are those shown except `consulted` and `informed`, which default empty. Status is `proposed | accepted | rejected | deprecated | superseded`. `supersedes` contains ADR IDs; cycles and missing targets fail. Links are `{rel: nonempty-string, target: nonempty-string}`.

Enforcement is either `null` or:

```yaml
enforcement:
  script: .drover/scripts/check-boundaries.ps1
  timeout_s: 120
```

The timeout is a positive integer. The script must be a committed regular `.ps1` file under `.drover/scripts/`. Legacy ADRs remain readable; missing scope means conservative applicability, never automatic clearance.

After rebase:

1. Preserve previous `adr_checked=B`; record new integrated main `M`.
2. Find ADR additions, modifications, renames, status changes, and supersessions between B and M. Inspect old and new scopes. Unexplained deletion is unresolved.
3. Intersect scope with declared writes and actual `M..candidate` paths, including both rename sides. An unprovable glob intersection remains a candidate.
4. No intersection produces `not_applicable`.
5. Named enforcement runs the **M-pinned script** against the candidate tree. Exit 0 means `passed`; exit 1 means `failed`; timeout, unavailable script, or other execution error means `not_checked`.
6. Otherwise ask a small model using ADR text, diff summary, and relevant hunks. Require `affected | not_affected | unsure`, rationale, and revision-qualified `file:line` evidence. Malformed, unsupported, or truncated judgments become `unsure`.
7. Route affected work to a larger reviewer or implementer; route unsure work to the owner. Deterministic failures require correction or an owner-approved replacement decision.

Each result records ADR/script hashes, B/M/candidate, matched paths, method, judgment, conformance, evidence, model/prompt identity, duration, tokens, cost, and proposed action. Model `not_affected` is triage, never an executable pass; skipped judgment is `not_checked`.

Advance `adr_checked` only after complete durable recording, retaining unresolved work. Rewritten main history or missing B requires a conservative full ADR inventory. Ordinary architecture checks still cover unchanged ADRs.

Shadow mode records findings and would-be actions without stopping implementation. Metrics separate applicability, coverage, unchecked cases, script failures, model judgments, reviewed false alerts, sampled missed impacts, owner interruptions, cost, latency, and rework. Blocking activation requires owner approval.

**5. Events and owner questions**

Every event has:

`schema_version`, `event_id`, `change_id`, `parents`, `name`, `producer_loop`, `actor`, `artifact_revisions`, `base_oid`, `subject_oid`, `occurred_at`, `payload`.

| Event | Producer → consumer; payload |
|---|---|
| `intent-ready` | Product → design; intent revision, accepted examples, open questions |
| `contract-ready` | Design → implementation; contract/tasks, write claims, accepted decision references, verification mapping |
| `decision-needed` | Any loop → owner; question packet below |
| `decision-made` | Owner → requesting loops; selected option, rationale, exact decision scope, invalidated revisions |
| `built` | Implementation → design/product; diff, actual writes, evidence, deviations, risks |
| `accepted` | Owner or delegated gate → dependent loops; subject revision, acceptance scope and evidence |
| `rejected` | Reviewer/owner → responsible loop; failed criteria, evidence, required rework |
| `rebased` | Implementation → design; previous/new main, candidate, ADR results |
| `merged` | Merge machinery → all loops; main commit, archive and spec-sync references |

Operational opening, start, and abandonment events maintain the phase cursor without requiring a model.

The hub ingests committed events, projects the active-design index, and updates task pages. Actionable events enter the Inbox. Immediate blockers notify the owner at once; routine questions form a 30-minute digest. Deduplication uses cause plus decision-scope hash. Delivery acknowledgments survive replay.

Owner packet:

```yaml
question_id: UUID
question: nonempty-string
options: [{id: slug, description: nonempty-string, impact: nonempty-string}]
recommended: option-id
evidence: [{revision: OID, path: path, line: positive-integer, finding: string}]
impact: nonempty-string
deadline: "RFC3339 timestamp | null"
delay_consequence: nonempty-string
can_continue: boolean
decision_scope_hash: H
```

Send immediately when work cannot continue within its contract or the deadline precedes the next digest. Defaults are recommendations, never answers by timeout. Pause only affected dependencies.

User behavior, data models, main APIs, new architectural modules/projects, and dependency direction require the person. Local reversible choices within accepted constraints stay with Drover.

`grill-me` is the default product intake; its conversation is captured as intent through the adapter. No skill installation is implicit.

Measure autonomous resolution and the 70/20/10 orchestration distribution separately from implementation tokens, spend, and quality.

**6. Workflow levels**

| Level | Required artifacts and treatment |
|---|---|
| L1 Documentation | Settled intent reference, bounded task, evidence; decisions only when consequential |
| L2 Mechanical | Intent, task, behavior-preservation invariant and reproducibility/equivalence evidence |
| L3 Narrow fix | Intent/reproduction, brief contract, task, independent tests, meaningful red, frozen tests, checks/review evidence |
| L4 Contained feature | Accepted intent, behavior/interface contract, checked task plan, applicable owner decisions, independent tests and evidence |
| L5 Contract | Large-model superpowers plan-profile contract, checked tasks, characterization before deletion, independent tests, frozen hashes, implementation and review evidence |
| L6 Major | Intent, architecture decisions, decomposition/migration contract, dependent one-repo L5 work, integration evidence |

Artifacts can share files or reference accepted main sources. L1/L2 do not require five documents.

Retain highest matching selector floors, script-first classification, small-model abstention, configured large judgments, and promotion without silent downgrade. Initially cheaper desired selections execute enabled L5; an unavailable higher level waits for owner activation. Actual-diff scope, reclassification, architecture checks, independent review, and normal full CI remain universal.

**7. Living specs and landing**

Default main layout:

```text
docs/specs/<capability>.md
ARCHITECTURE.md
docs/adr/*.md
```

Living specs describe accepted implemented behavior. ADRs separately describe accepted decisions, including pending implementation; `ARCHITECTURE.md` distinguishes current structure from planned changes.

Branch spec deltas are contract artifacts with stable requirement IDs and explicit additions, modifications, and removals. Metadata records each living spec’s starting hash.

Before final review/full CI, prepare one candidate containing code, tests, synchronized specs, architecture-map updates, and archived source documents. Preserve archived authored bytes. Move metadata to `.drover/archive/<change-id>.yaml`, recording source destinations and evidence record refs.

Main reachability establishes implementation after compare-and-swap succeeds. Post-test evidence stays on record refs, avoiding a self-referential tested commit.

Two branches changing one spec use a three-way comparison against recorded hashes. Disjoint requirement changes may combine mechanically and receive review. Overlapping or contradictory changes return to the product/design loop; changed behavior requires the person. Never use last-writer-wins. Re-run affected tests, review, and CI after reconciliation.

**8. Draft MADR**

Proposed NEW file: `docs/adr/0004-hybrid-design-methodology.md`; reserve the next available number when accepting.

```markdown
---
schema_version: 1
id: ADR-0004
status: proposed
date: "2026-10-20"
decision-makers: [johnm]
applies_to: ["drover/**", "hub/**", ".drover/**", "docs/**"]
enforcement: null
supersedes: []
links:
  - rel: methodology
    target: docs/superpowers/specs/2026-10-20-hybrid-methodology-design.md
---

# Use branch-carried design artifacts with format adapters

## Context and Problem Statement

Drover must coordinate continuing product, architecture, and implementation
work without imposing one document format or confusing approved decisions
with implemented behavior. The owner approved the hybrid direction on
2026-10-20; this record awaits acceptance of its wording.

## Decision Drivers

Preserve human decision rights, repository conventions, recoverable handoffs,
early verification, and economical orchestration.

## Considered Options

* One mandatory format: simple extraction, costly repository migration.
* Independent framework-specific workflows: native conventions, duplicated gates.
* Shared artifact model with adapters: common gates and preserved authored text.

## Decision Outcome

Choose the shared model: intent, decision, contract, task, and evidence.
Use superpowers first and plain as fallback.

Carry in-flight work on branches. Publish accepted decisions directly to main
through owner actions. Synchronize living behavior specs when implementation
lands. Generate the design index and event stream from Git records.

Check newly changed ADRs after rebase using scope, scripts, then model triage.
Begin in shadow mode. Keep owner questions concise and urgency-aware.

## Consequences

Adapters require versioning and conformance tests. Recovery requires event and
evidence refs as well as branches. Missing metadata prevents readiness.
Acceptance and implementation remain distinct.
```

The linked methodology design is NEW and would contain this design after owner acceptance. The decision supersedes the earlier single-format proposal, not an existing accepted ADR.

**9. Build tranches**

**A — now:** pure artifact model, metadata/events, adapters, templates, ADR candidate selection. Separate branch; no runtime imports or protected-area changes.

**B — after Phase 3b, independently of Phase 4:** extend the single loader, pin adapter/template snapshots, prepare inactive presets, research preservation, and corrected pure selector/outcomes work. Resolve all outstanding tranche-review findings. No workflow activation.

**C — after 4 → 5 → 5b → 5d → 5c:** integrate Waiting, lifecycle, routing, authority, ledger projections/notifications, dispatcher, pipeline, hub, and landing. Add shadow ADR execution and atomic spec finalization. Replace superseded execution paths.

**D — measured rollout:** activate L5, evaluate shadow outcomes, then owner-approved blocking checks and cheaper workflows.

**Tranche A: implementation contract**

Base: `3cead8d580bd404a06b63b442cf7be337c150825`.

Goal: provide testable pure artifact tooling. Non-goals: configuration loading, Git transport, SQL, launches, model calls, activation, and changes/imports involving dispatcher, pipeline, ledger, landing, hub, or routing.

Architecture decisions: NEW `drover/artifacts/` owns normalized artifacts and adapters. It depends on stdlib plus lazy hub-only YAML. Future workflow code consumes it. This replaces the proposed workflow-specific decoder extraction with one shared decoder.

Characterization: preserve [decoder scalar/security behavior](<../../../drover/tasktypes/codec.py:15>), [parser-free imports and bounds](<../../../tests/test_type_yaml.py:12>), and existing task-type exception behavior. No live behavior is deliberately dropped.

Each task includes tests within approximately 400 changed lines. Paths and symbols below are NEW unless linked. Brace lists enumerate exact files.

**Task 1: Shared model and decoder**

- Depends: none.
- Write: `drover/artifacts/{__init__,model,codec}.py`, `tests/test_artifact_model.py`; modify existing [tasktypes codec](<../../../drover/tasktypes/codec.py:15>).
- Read: linked codec and YAML tests above.
- Delete: original generic decoder body; retain a compatibility wrapper.
- Change: add `decode_yaml(text: str) -> dict`, `validate_artifact(raw: dict) -> dict`, and `ArtifactError`. Preserve 256-KiB/depth/security limits; keep the 1,000 field/state restriction only in the tasktypes wrapper.
- Failing first: `Codec.test_limit_separation`: 1,001 fields decode generically but fail through the legacy wrapper. Also pin duplicate-key rejection and parser-free import.
- Red/pass: `python3 -m unittest tests.test_artifact_model tests.test_type_yaml -v`; initially behavioral assertions fail after importable stubs; finally `OK`.

**Task 2: Metadata and event projection**

- Depends: 1.
- Write: `drover/artifacts/{metadata,events}.py`, `tests/test_artifact_metadata.py`.
- Read: Task 1 files and sections 2/5.
- Delete: none.
- Change: add `validate_change(raw: dict) -> dict`, `validate_event(raw: dict) -> dict`, `project(events: list[dict]) -> dict`. Projection maps change IDs to phase, causal heads, and questions; it confers no permissions.
- Failing first: `Metadata.test_replay`: duplicate question events yield one question; conflicting transitions raise `ArtifactError`. Reject unknown keys, dangling deltas, malformed hashes, and traversal.
- Red/pass: `python3 -m unittest tests.test_artifact_metadata -v`; missing validation assertions first, then `OK`.

**Task 3: Adapter interface and plain adapter**

- Depends: 2.
- Write: `drover/artifacts/{adapter,plain}.py`, `tests/test_artifact_plain.py`, `.drover/templates/plain/{intent,decision,contract,task,evidence}.md`.
- Read: predecessor files and section 3.
- Delete: none.
- Change: implement the stated interface/factory and literal heading extraction.
- Failing first: `Plain.test_preserving_edit`: edit one selected section in CRLF text containing comments and fenced headings; assert every unrelated byte survives. Stale hashes and ambiguous selectors fail.
- Red/pass: `python3 -m unittest tests.test_artifact_plain -v`; preservation assertions fail before implementation, then `OK`.

**Task 4: Superpowers adapter**

- Depends: 3.
- Write: `drover/artifacts/superpowers.py`, `tests/test_artifact_superpowers.py`, `.drover/templates/superpowers/{intent,decision,task,evidence}.md`, `.drover/templates/implementation-contract.md`.
- Read: predecessor files; [existing spec](<../../../docs/superpowers/specs/2026-10-17-task-types-design.md:7>); [mandatory plan template](<scratchpad/sol/plan-template.md:1>).
- Delete: none.
- Change: implement section-3 mappings; copy the supplied contract template verbatim.
- Failing first: `Superpowers.test_contract_tasks`: extract task dependencies/write sets from a concrete template fixture; absent write sets produce findings. Assert untouched-byte preservation, stable IDs after heading moves, and exact template bytes.
- Red/pass: `python3 -m unittest tests.test_artifact_superpowers -v`; mapping assertions fail first, then `OK`.

**Task 5: ADR parsing and candidate planning**

- Depends: 2.
- Write: `drover/artifacts/adr.py`, `tests/test_artifact_adr.py`.
- Read: section 4 and [existing ADR format](<../../../docs/adr/0002-windows-home-wsl-worker-pool.md:1>).
- Delete: none.
- Change: add `parse_adr(data: bytes, *, path: str) -> dict` and `impact_candidates(old: dict[str, bytes], new: dict[str, bytes], write_set: list[str]) -> list[dict]`. Return ADR IDs, old/new hashes, matched scopes, required method, and reason; execute nothing.
- Failing first: `ADR.test_changed_scope`: changing `drover/**` to `hub/**` still selects a branch writing `drover/decisions.py`. Cover supersession, legacy metadata, deletion, malformed enforcement, and uncertain glob overlap.
- Red/pass: `python3 -m unittest tests.test_artifact_adr -v`; missed-candidate assertions first, then `OK`.

Migration: no ledger changes. Repoint the dormant decoder wrapper atomically; rollback restores that wrapper/body. A copied ledger remains byte-identical.

Verification: named tests above cover model compatibility, recovery, preservation, extraction, and ADR selection. Run `python3 -m unittest discover -s tests -t .`; after 3b, use `pwsh -NoLogo -NoProfile -NonInteractive -File .drover/scripts/test.ps1` on Windows and WSL. No tests were run during this read-only design.

Risks: legacy heading ambiguity, retention of record refs, and overlapping ownership with the earlier decoder task. Resolve by explicit findings, retained refs, and replacing that task before implementation.

**10. Owner question**

**What permits ADR checks to become blocking?** Recommended default: explicit per-repo owner activation after reviewing a labeled shadow cohort, including sampled `not_affected` cases. Activate deterministic enforcement first; keep model judgments advisory until their missed-impact and interruption rates are acceptable. Deadline: before blocking rollout. Delay leaves shadow mode active and does not block tranche A.