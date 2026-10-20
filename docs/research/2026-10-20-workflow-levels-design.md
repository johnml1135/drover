Adopt repo-versioned YAML workflows with a small execution interface. Build **L5, the approved implementation-contract flow, first**; introduce cheaper levels after measuring that baseline.

The evidence supports explicit contracts, bounded work, independent tests, and mechanical enforcement. It does **not** establish an optimal model split or guarantee a particular defect reduction. [TDFlow](https://arxiv.org/abs/2510.23761) particularly supports separating test generation from implementation while identifying test quality as a bottleneck.

**1. Configuration**

Keep root `drover.toml`; add `.drover/workflows/` for policy and definitions. Do not introduce a second repo configuration entry point at `.drover/config.toml`: Drover already uses that location for an application configuration overlay.

Proposed additions for Drover itself—all referenced files below are NEW except `drover.toml` and `test.ps1`:

```toml
[workflows]
policy = ".drover/workflows/policy.yaml"

[scripts]
test = "scripts/drover/test.ps1"
docs = "scripts/drover/docs.ps1"
impact = "scripts/drover/impact.ps1"
architecture = "scripts/drover/architecture.ps1"
reproduce = "scripts/drover/reproduce.ps1"
```

Policy example:

```yaml
schema_version: 1
definitions:
  - docs.yaml
  - mechanical.yaml
  - fix.yaml
  - feature.yaml
  - contract.yaml
  - major.yaml
enabled: [contract]
default: contract

references:
  architecture: ARCHITECTURE.md
  adr_index: docs/adr/README.md
  research_index: docs/research/README.md

hooks:
  detect: impact
  architecture: architecture

owner_signals:
  - behaviour
  - data_model
  - main_api
  - new_module
  - dependency_direction
  - policy_change

selector:
  rules:
    - id: documentation
      when: {docs_only: true, intent_complete: true}
      workflow: docs
    - id: structural
      when: {signals_any: [significant_refactor, high_risk]}
      workflow: contract
    - id: major-change
      when: {signals_any: [cross_repo, major]}
      workflow: major
  classifier: {tier: small, min_confidence: 0.85}
  large_triggers:
    - significant_refactor
    - high_risk
    - cross_repo
    - major
    - classifier_abstained
    - repeated_design_hold
```

Exact policy keys are those shown. `when` additionally permits `kind_in`, `risk_in`, `signals_none`, `max_files`, and `max_lines`. Predicates combine with AND; unknown facts never satisfy predicates requiring their absence. Every matching rule contributes a minimum level; choose the highest.

Each definition requires `schema_version`, `id`, positive integer `revision`, unique integer `level`, nonempty `rationale`, nonempty repo-relative `research` references, and `phases`. Phases are exactly `prepare`, `task`, and `finish`; each contains ordered stages. `task` repeats sequentially for each contract task.

Stage keys are `op`, `tier`, and optional `when`, `action`, `template`. Operations are registered primitives, never arbitrary expressions or shell commands. Within a phase, operation names are unique. Example:

```yaml
schema_version: 1
id: contract
revision: 1
level: 5
rationale: "Settle architecture before delegating bounded implementation."
research:
  - docs/research/reports/2026-10-20-a-planning.md
  - docs/research/reports/2026-10-20-b-tdd.md
phases:
  prepare:
    - {op: plan, tier: large, template: .drover/templates/implementation-contract.md}
    - {op: validate_contract, tier: script}
    - {op: plan_check, tier: medium}
    - {op: detect, tier: script, action: impact}
    - {op: owner, tier: script, when: owner_required}
  task:
    - {op: characterize, tier: medium, when: deletes}
    - {op: baseline, tier: script, action: test, when: deletes}
    - {op: test_write, tier: medium, when: behaviour_changes}
    - {op: red, tier: script, action: reproduce, when: behaviour_changes}
    - {op: test_check, tier: medium, when: tests_written}
    - {op: freeze, tier: script}
    - {op: implement, tier: medium}
    - {op: scope, tier: script}
    - {op: reclassify, tier: script, action: impact}
    - {op: architecture, tier: script, action: architecture}
    - {op: checks, tier: script, action: test}
  finish:
    - {op: review, tier: medium}
```

`when` uses only `always`—the default—or the four conditions demonstrated above. Additional operations needed by lower levels are `edit` and `transform`; major work uses planning to propose dependent child tasks.

Use YAML for workflows because the owner requested it and its ordered structures are readable. Keep existing TOML configuration. **PyYAML 6.0.3 is already a hub dependency** in [hub/pyproject.toml](../../hub/pyproject.toml:11).

Extend the single loader, [repos.load](../../drover/repos.py:229), to read the policy, definitions, templates, and references from the **same resolved commit**. Return normalized JSON plus content hashes. Only the hub parses YAML; workers consume snapshots.

Reject unknown keys, duplicate IDs, missing references/actions, traversal, symlinks escaping the repo, unsupported operations, and invalid stage ordering. Preserve the existing bounded YAML decoder’s protections. Machine overrides retain existing behavior, but cannot override workflow definitions or approval requirements.

**2. Six workflow levels**

S = script; s = small; M = medium; L = large. All levels retain actual-diff scope checking, reclassification, architecture enforcement, exact-commit checks, independent review, and normal merge-queue/full-CI requirements.

| Level | Selector conditions | Stages and tiers | Gates and human checkpoints |
|---|---|---|---|
| **L1 Documentation** | Proven docs-only paths; settled intent; no consequential decision signals | Edit s → docs checks S → review s | Docs classification must hold on actual diff. Decision-bearing documentation still reaches owner. |
| **L2 Mechanical** | Repeatable transformation; declared behavior preservation; bounded scope | Transform S, or edit s → equivalence/checks S → review s | Require reproducibility or explicit invariant evidence. Unexpected semantic changes promote the work. |
| **L3 Narrow fix** | Reproducible defect within an existing module; no structural change | Brief s → separate tests M → test check M → red/freeze S → implement M → checks S → review M | Owner settles changed or ambiguous behavior; existing approved intent can satisfy this checkpoint. |
| **L4 Contained feature** | Clear requirements within existing modules and dependency direction | Contract M → plan check M → separate tests M → red/freeze S → implement M task-by-task → checks S → review M | Owner approves user-facing behavior and any model/API decisions before implementation. |
| **L5 Contract — build first** | Significant refactor, high risk, deletion, architectural uncertainty, or unresolved classification | **L planner → M plan check → characterization before deletion → separate M test writer/checker → frozen tests S → M implementation task-by-task → scope/architecture/checks S → M review** | Owner approves consequential decisions and significant architecture before coding. |
| **L6 Major change** | New project/subsystem, major migration, or coordinated multi-repo work | Research s → architecture/decomposition L → plan check M → owner → dependent L5 tasks → integration S → review L | Owner approves architecture, compatibility, migration, and dependency order. Each child writes one repo. |

L5’s planner writes the supplied contract format, copied verbatim into the versioned template. A JSON sidecar mirrors its decisions, tasks, write sets, tests, and verification mapping; both artifacts are hashed and checked together.

Characterization tests pass on the unchanged base. New behavior tests fail for the expected reason before implementation. Import errors, timeouts, and unrelated failures do not establish red evidence.

Freeze approved tests and test-control files by hash. Implementers cannot alter them; a test-change request returns to the independent test writer/checker and renews the evidence.

**3. Selection and reclassification**

1. Collect structured intent declarations, scope, task hints, and repo-hook findings.
2. Apply deterministic rules and mandatory floors.
3. For unresolved cases, ask one small classifier for `{workflow, signals, confidence, evidence, abstain}`. It cannot lower a deterministic floor.
4. Invoke large judgment only for configured triggers. Low confidence or malformed classification becomes `classifier_abstained`; unavailable capacity waits through normal routing.
5. Before activation of cheaper levels, selections below L5 execute L5 and retain both desired and applied selections. An unavailable higher level waits for the owner.

This follows the routing pattern of distinct categories and progressively more expensive judgment; the thresholds remain Drover hypotheses requiring evaluation. [Anthropic’s routing guidance](https://www.anthropic.com/engineering/building-effective-agents)

Reclassify after every task implementation, before committing, and before merge. Inspect the complete base-to-candidate diff plus dirty/untracked changes; treat renames as deletion and addition. Include previously committed work from reused runs.

Promotion stops dependent stages and requires missing planning, tests, or approvals. Never silently downgrade within a run. Scope expansion must acquire a compatible claim before work resumes.

Persist an append-only selection record containing:

`run_id`, `workflow_id`, `workflow_revision`, `snapshot_hash`, `base_sha`, `candidate_tree`, `rule_ids`, `facts`, `classifier_model`, `classifier_confidence`, `prompt_hash`, `rationale`, `research_refs`, `owner_override`, and `supersedes`.

Pin policy to the approved starting revision. Candidate policy edits are reviewed for future runs; they cannot weaken their own checks.

Interpret **70/20/10 initially as orchestration decisions**: scripts/small/large. Report medium implementation work, tokens, cost, and wall time separately.

**4. Decisions reserved for the owner**

| Change | Owner decides | Repo detection hook |
|---|---|---|
| User-facing behavior | Outcomes, edge cases, acceptance | Mandatory impact declaration; configured UI/CLI/behavior-spec comparisons |
| Data models, including internal domain models | Meaning, ownership, invariants; persistence/serialization compatibility where applicable | Model/schema manifests and migration comparisons |
| Main APIs | Surface, semantics, compatibility | Export/API/schema snapshots |
| New modules/projects | Responsibility, location, interfaces, tradeoffs | Module map, package/project manifests, new subsystem detection |
| Dependency direction | Allowed imports, layers, ownership changes | Dependency-graph comparison and architecture rules |
| Workflow/check policy | Changed authority or verification requirements | Policy, script, template, and test-control diffs |

A “new module” means a named subsystem with an interface or architectural dependency—not every helper file.

All detection knowledge lives in configured repo scripts. Invoke them through existing PowerShell execution. Supply JSON input via `DROVER_WORKFLOW_INPUT` containing `phase`, `task`, `base_sha`, `head_sha`, `paths`, and `contract`. Scripts write `DROVER_WORKFLOW_OUTPUT` containing:

```json
{
  "signals": {"data_model": true, "main_api": "unknown"},
  "findings": [{"signal": "data_model", "path": "...", "summary": "..."}],
  "violations": [{"rule": "...", "path": "...", "message": "..."}]
}
```

Malformed output, missing required hooks, or execution failure prevents advancement. Model declarations supplement detection; scripts cannot reliably infer all behavior changes.

Reuse [owner questions](../../drover/ledger/ops.py:815), the [API answer route](../../hub/api.py:345), and Inbox. Phase 4 Waiting parks the task and releases capacity. Deduplicate by cause and decision hash. Questions show the proposal, alternatives, recommendation, affected surfaces, and artifact links.

Phase 5d authority records the owner’s answer. Reuse an existing approval when it settles the same decision; changed decisions invalidate that approval.

NEW root `ARCHITECTURE.md` template:

```markdown
# Architecture
## Purpose and system context
## Module map
Module | Paths | Responsibility | Consumers | Allowed dependencies
## Invariants
Invariant | Owning module | Enforcement script/test
## Data models
Model | Meaning | Owner | Canonical source | Compatibility/migration rules
## Main APIs
Surface | Contract source | Consumers | Stability rules
## Cross-cutting flows
Transactions, errors, concurrency, authorization
## Decisions
ADR index; accepted decisions; superseded decisions
## Maintenance
Responsible owner; last reviewed revision; change triggers
```

Keep it concise and stable, following the architecture-map approach. [ARCHITECTURE.md guidance](https://matklad.github.io/2021/02/06/ARCHITECTURE.md.html) Planners propose updates; implementers include them in the same change; reviewers check consistency; people accept consequential decisions and ADRs.

**5. Research that survives**

Create:

```text
docs/research/
  README.md
  reports/2026-10-20-{a-planning,b-tdd,c-workflows,
                     d-coordination,e-architecture,f-workflows-config}.md
  decisions/YYYY-MM-DD-<workflow-decision>.md
  evaluations/YYYY-MM-DD-<cohort>.md
  evaluations/YYYY-MM-DD-<cohort>.json
```

Preserve the supplied reports, citations, dates, and limitations. The index records research ID, question, evidence type, findings, limitations, affected workflows, and review date.

Each workflow’s `research` references support its `rationale`. Decision records add alternatives, expected benefit, measurable hypothesis, thresholds, and superseded decisions. Policy snapshots preserve these references and source hashes.

Aggregate by level **and revision**: defect origin and detection stage, unknown origins explicitly marked, first-pass acceptance, substantive HOLD rounds, test revisions, scope violations, promotions, integration failures, escaped defects, owner wait, cost, and latency. Exclude infrastructure failures and red-base waits from defect/retry counts.

Generate cohort reports from ledger data. Tuning proposes policy changes with evidence; owner-reviewed commits activate them.

**6. Fit with today’s code**

Reuse:

- [scope_files overlap claims](../../drover/ledger/ops.py:516), with contract write sets supplying precise scopes.
- [commit_ready scope enforcement](../../drover/pipeline.py:80), strengthened for accumulated diffs and frozen artifacts.
- [docs-only classification](../../drover/ci.py:121), including symlink restrictions.
- Existing CI runner, receipts, review, merge queue, read-only reports, and owner questions.
- Seven [task kinds](../../drover/task_context.py:6), retained as intent/access categories. Workflow level is a separate concept from model escalation level.

**Do not base execution on dormant `drover/tasktypes`.** Its forms, custom fields, and custom lifecycle machinery exceed this requirement and overlap phases 4–5d. Extract its strict YAML codec, then retire the unused schema/package and obsolete tests. Record this supersession against the older [task-types specification](../../docs/superpowers/specs/2026-10-17-task-types-design.md:45); preserve unrelated future requirements.

Delete replaced direct worker→checks→review scheduling and the [brief assumption that task approval alone settles design](../../drover/brief.py:18). Replace them with snapshot-driven stages, retaining one execution path.

**7. Workflow levels: implementation contract**

**Base:** `dc487ccf95657ed24f7c8a93928fabbdc1a32643`. Rebase runtime tasks after in-flight phases land.

**Goal:** Execute configurable, evidence-backed workflows, initially L5.
**Non-goals:** arbitrary workflow graphs, custom task forms, autonomous policy weakening.

**Architecture decisions:** NEW `drover/workflow/` owns pure policy and stage decisions. `repos.py` owns loading; lifecycle owns transitions; router owns assignments; ledger owns persistence; dispatcher executes stage requests.

NEW public interfaces:

```python
normalize_policy(raw: dict, definitions: dict[str, dict]) -> dict
select(policy: dict, facts: dict, classifier: dict | None = None) -> dict
next_stage(instance: dict, evidence: list[dict]) -> dict
```

`next_stage` returns a stage request, missing-evidence cause, owner-wait cause, or completion. It performs no SQL or launches.

NEW ledger records: immutable policy snapshots; per-task workflow instances holding snapshot/base/contract/cursor/revision; stage evidence linked to runs; selection history. Phase 5 lifecycle advances the cursor transactionally with accepted evidence.

**Characterization before deletion:** pin commit-scoped loading, machine merging, docs/symlink behavior, scope refusal before index changes, owner-only answers, restart adoption, and exact-SHA checks/review through public interfaces. Deliberately drop dormant type/form semantics and duplicated scheduling.

For tasks below, **all unlinked paths and symbols are NEW**. Each task has a ≤400-line implementation budget; retirement is a sequence of deletion-only patches of that size. Shared read set: this design, the supplied reports/template, GLOSSARY, ADRs, and landed phase contracts.

**Task 1: Policy loading**

- Depends: none. Write: `drover/workflow/{__init__,codec,policy}.py`, `drover/repos.py`, `tests/test_workflow_config.py`. Read: existing codec and loader.
- Delete: duplicate parser behavior after extraction.
- Change: implement `normalize_policy`; extend existing loader without another file-reading path.
- Failing first: `WorkflowConfig.test_commit_snapshot` asserts dirty edits cannot change a snapshot; malformed definitions fail. Command: `python -m unittest tests.test_workflow_config -v`; initially missing module, then PASS.

**Task 2: Selection**

- Depends: 1. Write: `drover/workflow/select.py`, `tests/test_workflow_select.py`. Delete: none.
- Change: implement `select` with floors, abstention, activation, and promotion rules.
- Failing first: `Selection.test_docs_cannot_lower_api_change`; assert API impact survives a docs match. Run `python -m unittest tests.test_workflow_select -v`; initially missing selector, then PASS.

**Task 3: Durable evidence**

- Depends: 1; landed 5c. Write: `drover/ledger/workflows.py`, existing [schema](../../drover/ledger/schema.py:120), `tests/test_workflow_storage.py`. Read: landed ledger/authority interfaces.
- Delete: none. Add typed workflow commands/queries through 5c’s ledger interface.
- Failing first: `Storage.test_restart_preserves_snapshot_and_rejects_stale_revision`; assert replay idempotence and revision rejection. Run corresponding unittest module; initially absent commands, then PASS.

**Task 4: Contract and stage decisions**

- Depends: 1–3. Write: `drover/workflow/stages.py`, `.drover/templates/implementation-contract.md`, `tests/test_workflow_stages.py`. Delete: none.
- Change: implement `next_stage`; enforce characterization, independent test roles, meaningful red, frozen hashes, and ordered tasks.
- Failing first: `Stages.test_implementation_requires_accepted_test_evidence`; reject missing/stale evidence. Run corresponding unittest module; initially missing evaluator, then PASS.

**Task 5: Runtime integration**

- Depends: 4; phases 4, 5, 5b, 5d, 5c. Write: existing [dispatcher](../../drover/dispatcher.py:803), pipeline, brief, and [router](../../drover/route.py:234); `tests/test_workflow_runtime.py`.
- Delete: replaced direct scheduling and duplicate routing decisions.
- Change: consume stage requests using landed lifecycle, Waiting, authority, and routing interfaces; pass requested stage tier to the router.
- Failing first: `Runtime.test_owner_wait_releases_capacity_and_resumes_once`; run corresponding unittest module, initially direct worker launch, then PASS.

**Task 6: Diff and architecture enforcement**

- Depends: 5. Write: pipeline, CI; `scripts/drover/{impact,architecture,reproduce}.ps1`; `tests/test_workflow_gates.py`.
- Delete: divergent scope/classification logic.
- Change: enforce complete-diff scope, frozen artifacts, promotion, and hook evidence before advancement.
- Failing first: `Gates.test_committed_rename_and_test_runner_change_are_detected`; assert refusal before commit/merge. Run corresponding unittest module; initially missed violation, then PASS.

**Task 7: Presets, research, outcomes**

- Depends: 6. Write: `.drover/workflows/*.yaml`, `docs/research/**`, `ARCHITECTURE.md`, `docs/adr/README.md`, `drover.toml`, [GLOSSARY.md](../../GLOSSARY.md:1), `drover/workflow/outcomes.py`, `tests/test_workflow_presets.py`.
- Delete: none.
- Change: author six complete definitions, activate only L5; add `summarize_outcomes(records: list[dict]) -> dict`.
- Failing first: `Presets.test_all_definitions_have_resolvable_evidence_and_infra_is_excluded`; run corresponding module, initially missing definitions, then PASS.

**Task 8: Retirement and migration rehearsal**

- Depends: 7. Write/delete: old tasktypes files/tests; `tests/test_workflow_migration.py`; older specification’s supersession notice.
- Delete in bounded patches: dormant task validators, type normalization, then unused helpers/package. Move codec tests before removing their old imports.
- Failing first: `Migration.test_old_runs_resume_without_tasktypes`; assert preserved runs and no retired-package dependency. Run corresponding module; initially retired dependency/package remains, then PASS.

**Migration:** use the next schema version after 5c. Rehearse against a copy of a real ledger; verify foreign keys, waits, runs, and replay. Existing runs receive equivalent legacy checkpoints; new opted-in work uses L5. Rollback disables new admissions and restores the backed-up ledger with matching binaries.

**Verification:** task tests map to loading, selection, persistence, ordering, authority, scope, and migration acceptance. Run `python -m unittest discover -s tests -t .` through `scripts/drover/test.ps1` on Windows and WSL; obtain independent medium review and conformance review.

**Sequencing:** prepare policy/research independently. Integrate after **4 → 5 → 5b → 5d → 5c**. Waiting owns pauses, lifecycle owns transitions, router owns assignments, authority owns approvals, ledger owns tables/notifications. Re-survey shared files before execution.

**8. Owner decisions**

- **Supersede dormant task-type execution machinery?** Default: yes; retain seven kinds and defer custom forms/lifecycles.
- **What does 70/20/10 measure?** Default: orchestration decisions, with implementation spend reported separately.
- **May tuning activate cheaper workflows automatically?** Default: no; owner-reviewed policy commits.
- **Does an existing approved decision satisfy a checkpoint?** Default: yes, when its decision scope remains unchanged.
