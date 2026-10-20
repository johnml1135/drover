# Topic E: Human decision rights and `ARCHITECTURE.md`

*Delivery note: The workspace is enforced read-only, so I could not save this report to a file. The report is provided here.*

## Findings

The strongest pattern is to separate three artifacts: requirements state what behavior a person wants; `ARCHITECTURE.md` maps how the system is organized; ADRs record why consequential choices were made. matklad recommends a short, slow-changing map of modules, relationships, boundaries, and invariants; rust-analyzer shows this in practice with module responsibilities, explicit invariants, and labels for API boundaries. [matklad](https://matklad.github.io/2021/02/06/ARCHITECTURE.md.html), [rust-analyzer architecture](https://github.com/rust-analyzer/rust-analyzer/blob/d7c99931d05e3723d878bea5dc26766791fa4e69/docs/dev/architecture.md)

OpenAI’s harness-engineering account uses a short `AGENTS.md` as a map to deeper repository docs, including `ARCHITECTURE.md`; it describes mechanical checks and doc-gardening to reduce drift. `AGENTS.md` is standard Markdown with no required fields, while Claude Code documents `CLAUDE.md` as behavioral context rather than enforcement. One cross-tool wrinkle: Claude’s default instruction-file behavior can favor `CLAUDE.md` over `AGENTS.md`, so use an import or verify which file each configured agent loads. [OpenAI harness engineering](https://openai.com/index/harness-engineering/), [AGENTS.md](https://agents.md/), [Claude Code memory docs](https://code.claude.com/docs/en/memory)

C4 provides zoom levels from system context through containers, components, and code; its guidance says to create component diagrams only when useful. arc42 is explicitly tailorable, and offers a one-page canvas for a lightweight architecture overview. These fit a concise repo map; neither requires adopting a heavyweight documentation process. [C4 model](https://c4model.com/), [C4 component diagrams](https://c4model.com/diagrams/component), [arc42 overview](https://arc42.org/overview/)

ADRs should capture a single decision’s context, status, decision, and consequences. MADR adds explicit drivers and options with pros and cons; `adr-tools` can number records and mark replaced decisions as superseded. Agents can draft proposals and maintain records, but the human who makes the decision should control whether a record is accepted. [Nygard](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions), [MADR template](https://github.com/adr/madr/blob/develop/template/adr-template.md), [adr-tools](https://github.com/npryce/adr-tools)

## Who decides

Documented processes vary in formality, but reserve deliberation for changes with broad impact, significant tradeoffs, or consequences that are hard to reverse. Rust routes “substantial” changes through RFCs and team decisions; Python PEPs are the primary process for major features and the Steering Council is the final authority; Kubernetes requires KEPs for most non-trivial, controversial, or wide-ranging changes. Uber has required RFCs for new services. AWS recommends centralizing one-way-door decisions while delegating reversible ones. [Rust RFC process](https://github.com/rust-lang/rfcs), [PEP 1](https://peps.python.org/pep-0001/), [Kubernetes KEP process](https://github.com/kubernetes/enhancements/blob/master/keps/sig-architecture/0000-kep-process/README.md), [Uber service RFCs](https://www.uber.com/us/en/blog/building-tincup-microservice-implementation/), [AWS decision guidance](https://docs.aws.amazon.com/wellarchitected/2025-02-25/framework/ops_priorities_eval_tradeoffs.html)

Some project policies explicitly require upfront design review: Gerrit requires design docs for its design-driven contribution process, while Spotify describes ADRs as one practice used by only some teams, often reached through RFCs or engineering meetings. Architecturally significant requirements include technically challenging or constraining requirements that shape architecture, not just visible features. [Gerrit design docs](https://gerrit-review.googlesource.com/Documentation/dev-design-docs.html), [Spotify ADR practice](https://engineering.atspotify.com/2020/04/when-should-i-write-an-architecture-decision-record), [OpenUP on architecturally significant requirements](https://archive.eclipse.org/epf/downloads/OpenUP/published/openup_published_1.5.1.5_20121212/openup/core.tech.common.extend_supp/guidances/concepts/arch_significant_requirements_1EE5D757.html)

Agent workflows offer useful but inconsistent gates: Devin says it asks when confidence is low and otherwise proceeds; Factory Spec Mode proposes a plan read-only and asks approval; Kiro’s standard spec flow has reviews between phases, while Quick Spec removes those gates; Claude Code Plan mode proposes a plan without editing source code. Superpowers calls for human design approval before architectural implementation. **Recommendation:** use confidence as supplementary context, not as the escalation rule; the change classes should determine the gate. [Devin 2.1](https://cognition.com/blog/devin-2-1), [Factory Spec Mode](https://docs.factory.com/autonomy-and-safety/specification-mode), [Kiro spec workflows](https://kiro.dev/docs/specs/feature-specs/requirements-first/), [Claude Code Plan mode](https://code.claude.com/docs/en/desktop), [Superpowers brainstorming](https://github.com/obra/superpowers/blob/main/skills/brainstorming/SKILL.md)

## Proposed decision table

| Change class | Human decision before implementation | Agent decides |
|---|---|---|
| User-visible behavior or acceptance criteria | Desired behavior, edge cases, and success criteria | Implementation within the existing architecture |
| Persistent or serialized data model | Entities, fields, migration/backfill, and compatibility | Migration mechanics after approval |
| Main API or contract | Public exports, routes, CLI, protocol/schema, and compatibility policy | Internal implementation behind the approved contract |
| New project, package, or architectural module; changed dependency direction, layer, or cross-cutting pattern | Module responsibility, location, dependencies, and significant quality tradeoffs | Implementation details within the approved design |
| Routine internal change that preserves behavior, contracts, and architecture | No additional decision | Plan and implement |

Define “module” for the gate as a new package, subproject, or named subsystem with its own interface or dependency edge; a helper file inside an existing module does not trigger it. This is a proposed operational threshold to keep the owner’s rule scriptable without escalating every file addition.

## Detection and `ARCHITECTURE.md`

**Use both plan-time and diff-time checks.** Require every plan to declare its intended behavior and list proposed paths. Before coding, a script matches paths against a per-repo policy for migration/model files, public API surfaces, and package/project manifests. After coding, run the same classification on the actual diff; if it hits a trigger the plan missed, pause for human review. File matching is a backstop, not a reliable detector of user intent, so make behavior declaration mandatory.

For data models, match migration and schema paths and run the framework’s model-to-migration drift check: Django’s `makemigrations --check` exits nonzero if model changes lack migrations, while Alembic can generate candidate migrations by comparing application metadata and schema. Alembic notes those candidates require review. [Django command reference](https://docs.djangoproject.com/en/dev/ref/django-admin/), [Alembic autogenerate](https://alembic.sqlalchemy.org/en/latest/autogenerate.html)

For APIs, compare tracked public-surface reports or schemas against the base revision: API Extractor’s tracked reports make signature changes visible in PR diffs; Griffe can compare Python API snapshots, but warns there is no universally agreed definition of Python’s public API, so define it explicitly, for example with `__all__`; `japicmp` compares Java artifact compatibility; `oasdiff` and Buf provide breaking-change checks for OpenAPI and Protobuf. [API Extractor](https://api-extractor.com/pages/setup/configure_api_report/), [Griffe checks](https://mkdocstrings.github.io/griffe/guide/users/checking/), [Griffe public APIs](https://mkdocstrings.github.io/griffe/guide/users/recommendations/public-apis/), [japicmp](https://github.com/siom79/japicmp), [oasdiff](https://github.com/oasdiff/oasdiff/blob/main/docs/BREAKING-CHANGES.md), [Buf breaking checks](https://buf.build/docs/breaking/)

Recommended `ARCHITECTURE.md` shape:

1. **Purpose and system context:** one-paragraph system summary and, if it clarifies scope, a C4 context/container view.
2. **Module map:** each major module’s responsibility, directory or key files, consumers, and allowed dependency direction.
3. **Invariants and data ownership:** durable rules, canonical model/schema locations, and serialization boundaries.
4. **Main interfaces:** public APIs and contract sources, noting stable versus internal surfaces.
5. **Cross-cutting concerns:** important flows and rules such as transactions, errors, security, or concurrency.
6. **Decision pointers and change gates:** link the ADR index and state which changes need a human decision.

Keep the map stable and concise; record volatile details in code docs or generated schema/API references. Update it in the same change when module responsibilities, boundaries, or invariants move. OpenAI describes CI checks and recurring doc-gardening; HN practitioners likewise value a single map and warn that big changes should update docs, while noting stale docs are a real maintenance risk. [matklad](https://matklad.github.io/2021/02/06/ARCHITECTURE.md.html), [OpenAI harness engineering](https://openai.com/index/harness-engineering/), [HN discussion](https://news.ycombinator.com/item?id=26048784)

**Practitioner evidence, as anecdote:** HN commenters describe architecture maps as saving time finding where to change code and distinguish the map’s “how” from ADRs’ “why.” In r/ExperiencedDevs, one team reported that proposals were implemented but the final decision often remained buried in Slack; a commenter suggested keeping proposal and accepted decision together. A Claude Code user described switching to plan files for line-by-line review and persistent history, while giving up the built-in approval step. These are practitioner reports, not controlled evidence. [HN architecture discussion](https://news.ycombinator.com/item?id=26048784), [r/ExperiencedDevs RFC-to-ADR discussion](https://www.reddit.com/r/ExperiencedDevs/comments/1qfffh2/rfcdesign_doc_to_adr_what_does_your_process/), [r/ClaudeCode plan-file discussion](https://www.reddit.com/r/ClaudeCode/comments/1sdl1eg/do_you_write_plans_to_a_file_or_use_the_builtin/)

## Implications for Drover

1. Make the decision table a policy in each repo; require human approval for behavior, persistent/serialized models, main APIs, and architectural module/project changes.
2. Require plans to list exact intended write paths, behavior changes, affected contracts/models, and any new module or dependency edge before implementation.
3. Add a script-driven gate before coding and again on the actual diff; route unexpected triggers to a person before merge.
4. Establish `ARCHITECTURE.md` as the concise system map, with module responsibilities, dependency rules, invariants, data ownership, main APIs, and ADR index.
5. Keep one canonical `AGENTS.md` as the cross-agent entry map; import it from `CLAUDE.md` where needed, and put enforcement in Drover/CI rather than prose alone.
6. Generate or compare API/schema artifacts in CI; use a repo-specific public API declaration and fail closed when triggered contract changes lack an explicit human decision.
7. Let agents draft proposed ADRs, but only a person can accept them; after implementation, link the accepted record and refresh the architecture map if structure changed.
8. Reserve planned write sets across parallel tasks; serialize work with overlapping paths and re-run the diff gate if an agent expands its scope. A practitioner specifically recommends using changed paths as an off-plan signal. [r/ChatGPTCoding](https://www.reddit.com/r/ChatGPTCoding/comments/1vn44j3/how_do_you_keep_humans_in_the_loop_without/)
