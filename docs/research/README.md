# Research

Drover is meant to be a research-based optimization tool: every workflow it runs should say why it exists, and that reasoning should outlive the conversation that produced it. This folder holds that research.

## Rules
- One file per topic, named `YYYY-MM-DD-<topic>.md`. Reports cite a source for each claim and end with "Implications for Drover".
- A workflow definition (`.drover/workflows/*.yaml`) lists the research files that justify it in `research:`. A change to a workflow cites new or existing research.
- Outcome data per workflow level (HOLD rounds, where each defect was introduced and where it was caught, cost) is added here as dated reports, so thresholds change on evidence.
- Research is not policy. Accepted decisions go to `docs/adr/`; the research is linked from them.

## Index (2026-10-20)
| File | Question |
|---|---|
| 2026-10-20-plan-depth.md | What a complete implementation plan contains (Spec Kit, OpenSpec, superpowers, Kiro, Aider, BMAD, Taskmaster) |
| 2026-10-20-test-first-pipeline.md | Red/green/refactor with separate test-writer and implementer agents; frozen tests; mutation testing |
| 2026-10-20-agent-workflows-and-quality-process.md | Gas Town, Claude agent teams, MetaGPT, Factory; TSP/PSP, Fagan, poka-yoke; architecture fitness checks |
| 2026-10-20-parallel-change-coordination.md | Write claims, leases, merge queues, expand/contract, large-scale changes |
| 2026-10-20-owner-decisions-and-architecture-md.md | What a person decides (behaviour, data models, main APIs, modules); ARCHITECTURE.md; detecting those changes by script |
| 2026-10-20-configurable-workflow-levels.md | Drover today; selectable workflow levels by change class |
| 2026-10-20-design-in-branches-and-format-adapters.md | Designs in branches (Oxide RFD, KEP, Rust RFC, OpenSpec, Spec Kit); one artifact model with per-format adapters |
| 2026-10-20-adr-impact-checks.md | Checking whether new ADRs affect code: front matter, fitness functions, adrkit/ADRian, model accuracy |
| 2026-10-20-three-loops-and-owner-interrupts.md | Product, design and implementation loops; when and how to ask the owner |
| 2026-10-20-design-implementation-interface.md | What the design-to-implementation handoff should be: executable skeletons, tests as spec, symbol anchors, converging reviews |
| 2026-10-20-script-gates-and-model-cascades.md | Moving a pipeline toward 70/20/10: plan linters, scope gates, test-tamper and deleted-behaviour detectors, cascades |
| 2026-10-20-process-self-audit.md | Audit of the 2026-10-19/20 architecture work: 82 findings by class and the cheapest stage that would have caught each (44 by script) |
| 2026-10-20-workflow-levels-design.md | The resulting design: six levels, selector, escalation table, config shape, build plan |

## Why the first workflow is "L5 contract"
Most review HOLDs in the 2026-10-19/20 architecture work were behaviour deleted without a replacement (wizard fallback settings, CI resource limits, the `git diff --check` guard). Characterization tests before deletion target that directly. Plans that say "use X and fix Y" leave architecture to implementers who do not know it; a file-level contract (superpowers format) checked by a second model closes that gap. A poor plan can be worse than none (plan-depth report), so the plan is checked before coding.
