I can’t save a file in this read-only workspace, so the report is inline below.

# Research Report: Automatically Keeping Code in Line with ADRs

## Findings

**A decision record and an enforcement check serve different roles.** *Building Evolutionary Architectures* describes fitness functions as checks that run through deployment pipelines; Thoughtworks describes them as a way to provide architectural feedback during development. Architecture tests, import rules, and static analysis make decisions such as dependency direction objectively checkable. [O’Reilly’s book outline](https://www.oreilly.com/library/view/building-evolutionary-architectures/9781492097532/), [Thoughtworks on fitness function-driven development](https://www.thoughtworks.com/insights/articles/fitness-function-driven-development)

**Several tools can enforce specific rules, but none covers every decision.** ArchUnit checks Java dependencies, layers, slices, and cycles; Python’s Import Linter offers forbidden-import, layer, and independence contracts; dependency-cruiser expresses forbidden and required JavaScript or TypeScript dependency rules; Semgrep supports custom code-pattern rules. These suit decisions with a checkable code shape; they do not automatically translate arbitrary ADR prose into reliable rules. [ArchUnit guide](https://www.archunit.org/userguide/html/000_Index.html), [Import Linter contracts](https://import-linter.readthedocs.io/en/stable/contract_types/), [dependency-cruiser rules](https://github.com/sverweij/dependency-cruiser/blob/main/doc/rules-reference.md), [Semgrep rule syntax](https://semgrep.dev/docs/writing-rules/rule-ideas)

**ADR management tools mainly help with lifecycle and discovery.** `adr-tools` creates numbered records and links superseding decisions; Log4brains provides ADR authoring and publication; the Backstage ADR plugin supports browsing and search across entities. Their documented features help people find and manage decisions, but do not provide the branch impact gate Drover needs. Backstage’s plugin recognizes MADR formats, including MADR 3.x front matter. [adr-tools](https://github.com/npryce/adr-tools), [Log4brains](https://github.com/thomvaill/log4brains), [Backstage ADR plugin](https://github.com/backstage/community-plugins/blob/main/workspaces/adr/plugins/adr/README.md), [Backstage ADR guidance on superseding records](https://backstage.io/docs/next/architecture-decisions/)

**“ADR as code” is taking shape, but the format is not settled.** `adrkit` uses validated front matter with typed `affects` matchers, including paths and packages; its CI action maps changed files to accepted decisions and comments on pull requests. `ADRian` matches changed paths against `applies_to` globs, can pre-filter diffs before a model call, and uses Claude to check applicable ADRs. These are useful design references, not evidence of a mature standard. A 2026 master’s thesis describes a separate ADR-linked enforcement DSL and reports generating 44 checks from 9 of 17 ADRs in one .NET project; that is promising but narrow evidence. [adrkit quickstart](https://adrkit.dev/quickstart/), [adrkit CI guide](https://adrkit.dev/ci/), [ADRian README](https://github.com/wbern/adrian), [Architectural Decision Enforcement thesis](https://eprints.ost.ch/id/eprint/1381/)

**LLMs can help with explicit, code-visible decisions; “unsure” must be a real outcome.** A 2026 arXiv study analyzed 980 ADRs from 109 repositories and manually validated 305 cases. Its best model scored 91.1% accuracy overall, but performance was weaker when code alone was insufficient; the authors report category imbalance and human-labeling limits. A separate systematic review calls architecture conformance checking underexplored. These results support using a model as a triage aid, not treating its “unaffected” judgment as proof. [ADR violation study](https://arxiv.org/abs/2602.07609), [systematic literature review](https://arxiv.org/abs/2505.16697)

**Documentation coupling helps with drift, but it is not architecture enforcement.** Swimm documents automatically updating code snippets and asking humans to intervene when a change cannot be safely synced; its PR blocking option is configurable. GitHub CODEOWNERS can route or require reviews for documentation changes. Neither mechanism checks whether implementation obeys an architectural decision. [Swimm Auto-sync details](https://swimm.io/blog/how-does-swimm-s-auto-sync-feature-work), [GitHub CODEOWNERS documentation](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners)

**Practitioner reports echo Drover’s risks, but are anecdotal.** Recent HN discussion of agent orchestration describes agents colliding on the same files and overlapping designs. A r/ChatGPTCoding discussion reports agents adding special cases or changing tests to satisfy the immediate task. These posts are not controlled evidence, but they reinforce the value of precise scope, explicit checks, and early feedback. [HN agent-orchestrator discussion](https://news.ycombinator.com/item?id=46993479), [r/ChatGPTCoding discussion](https://www.reddit.com/r/ChatGPTCoding/comments/1x4xadz/agentic_coding_solves_the_easiest_part_of/)

## Proposed Design

The following is a recommendation inferred from the tool designs and research above, not a standard schema.

```yaml
---
id: ADR-0042
status: accepted
applies_to:
  paths: ["drover/dispatch/**", "!**/tests/**"]
  modules: ["drover.orchestration.dispatch"]
enforcement:
  script: "scripts/check_dispatch_boundaries.py"
  mode: "changed-scope"
supersedes: ["ADR-0017"]
---
```

Keep the ADR’s decision prose as the rationale. Require every accepted ADR to state which paths or modules it governs; allow an explicit “no executable rule” value instead of pretending every decision can be automated. `adrkit` demonstrates typed scope metadata, while ADRian demonstrates path-scoped model checks; Fowler’s ADR guidance recommends linking a changed decision to a superseding record. [adrkit schema](https://adrkit.dev/schema/), [ADRian format](https://github.com/wbern/adrian), [Martin Fowler on ADRs](https://martinfowler.com/bliki/ArchitectureDecisionRecord.html)

After rebase, compare the old main SHA with the new main SHA and select only ADRs newly accepted or superseded in that interval. Match those ADR scopes against the branch’s changed paths. Run a referenced, validated script where available; a pass means the rule is satisfied, while a failure identifies a concrete violation. For scoped ADRs without an executable rule, send a small model the ADR, matched paths, and concise diff summary plus relevant hunks. Require `affected`, `unaffected`, or `unsure`, with cited files and lines as evidence. Route `affected` to a stronger review or implementation update; route `unsure` to an owner. This is consistent with path-based routing in `adrkit` and `ADRian`, while the LLM study cautions that models struggle to identify insufficient evidence. [adrkit CI guide](https://adrkit.dev/ci/), [ADRian README](https://github.com/wbern/adrian), [ADR violation study](https://arxiv.org/abs/2602.07609)

Treat “ADR applies” and “branch violates ADR” as separate questions: scope matching establishes relevance, while a check or reviewer establishes conformance. Do not make “no match” silently mean “pass.” `adrkit` warns that an empty `affects` field is advisory, and ADRian notes that a pre-filter can skip a model call rather than confirm compliance. [adrkit quickstart](https://adrkit.dev/quickstart/), [ADRian README](https://github.com/wbern/adrian)

## Implications for Drover

1. **Add a versioned ADR front-matter schema** for `status`, nonempty `applies_to`, `enforcement`, and `supersedes`; reject unknown fields and invalid references in CI. Typed schema validation and explicit lifecycle links are supported by `adrkit` and `adr-tools`. [adrkit schema](https://adrkit.dev/schema/), [adr-tools](https://github.com/npryce/adr-tools)

2. **Trigger checks only for ADR changes since the branch’s last recorded main SHA.** Match accepted and superseding ADRs to the rebased branch’s changed paths before invoking any model; this follows the path-scoped routing used by `adrkit` and `ADRian`. [adrkit CI guide](https://adrkit.dev/ci/), [ADRian README](https://github.com/wbern/adrian)

3. **Make executable rules first-class ADR metadata.** Store the script path and invocation, validate that the target exists, and run it against the rebased tree; use import-linter, ArchUnit, dependency-cruiser, or Semgrep where the decision fits their rule models. [Import Linter](https://import-linter.readthedocs.io/en/stable/contract_types/), [ArchUnit](https://www.archunit.org/userguide/html/000_Index.html), [dependency-cruiser](https://github.com/sverweij/dependency-cruiser/blob/main/doc/rules-reference.md), [Semgrep](https://semgrep.dev/docs/writing-rules/rule-ideas)

4. **Use the cheap model only on scoped branches without a passing executable check.** Give it the ADR, path matches, diff summary, and relevant hunks; require a tri-state result and file/line evidence. The available ADR study supports hybrid review and finds that “code insufficient” cases are harder for models. [ADR violation study](https://arxiv.org/abs/2602.07609)

5. **Control owner noise by routing only `unsure` and unresolved violations to the owner.** Send `affected` cases to a larger reviewer or back to the implementer with the evidence; count a model skip as “not checked,” never as “passed.” [ADRian README](https://github.com/wbern/adrian), [ADR violation study](https://arxiv.org/abs/2602.07609)

6. **Pilot in shadow mode against historical rebases.** Label affected, unaffected, and unsure outcomes; track missed impacts, false alerts, escalation rate, and latency by repo before making checks blocking. This is a Drover-specific evaluation recommendation; published accuracy figures are not a substitute for local validation. [ADR violation study](https://arxiv.org/abs/2602.07609)

7. **Keep broad or vague decisions out of the automatic gate until sharpened.** Require each enforceable decision to state an observable rule and scope; use CODEOWNERS for ADR review and a catalog such as Backstage or Log4brains for discovery. [ADRian’s diff-visibility guidance](https://github.com/wbern/adrian), [GitHub CODEOWNERS](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners), [Backstage ADR plugin](https://github.com/backstage/community-plugins/blob/main/workspaces/adr/plugins/adr/README.md), [Log4brains](https://github.com/thomvaill/log4brains)
