The workspace is read-only, so I couldn’t save this report to a file. No files were modified; the report is below.

# Three loops for Drover

## Findings

Treat the loops as connected, continuing work—not a one-way requirements handoff. The product loop decides what user behavior and outcome matter; the architecture loop translates that intent into a fit with the existing codebase; the implementation loop makes and verifies bounded changes. Product discovery can continue while delivery runs: SVPG describes discovery and delivery as continuous, parallel activities, and warns that turning them into separate teams can weaken shared ownership. The Design Council’s Double Diamond likewise allows early testing and new evidence to send work back to redefine the problem. ([SVPG on continuous discovery](https://www.svpg.com/continuous-discovery/), [SVPG on discovery and delivery](https://www.svpg.com/discovery-delivery/), [Design Council Double Diamond](https://www.designcouncil.org.uk/resources/the-double-diamond/))

For the product loop, Shape Up suggests a useful intake discipline: set an appetite, sketch a solution within it, find “rabbit holes,” then write a pitch that states the problem, constraints, solution, risks, and limitations. A betting table chooses which pitches to fund. This keeps a raw idea from becoming an unexamined commitment. ([Shape Up: Principles of Shaping](https://basecamp.com/shapeup/1.1-chapter-02), [Shape Up: Set Boundaries](https://basecamp.com/shapeup/1.2-chapter-03), [Shape Up: The Betting Table](https://basecamp.com/shapeup/2.2-chapter-08))

The architecture loop needs a distinct artifact: a repo-grounded implementation contract. Google’s engineering book says major projects often require an approved design doc before work starts; it should cover implementation strategy, key decisions and trade-offs, alternatives, and concerns such as security and storage. Some teams discuss these docs before coding, effectively reviewing design before code exists. Stripe describes a lighter version for API changes: a brief supporting document reviewed broadly before release. These examples support early review, but also show that review cadence should fit the change rather than copy one company-wide ceremony. ([Software Engineering at Google, “Design Docs”](https://abseil.io/resources/swe-book/html/ch10.html), [Stripe on API review](https://stripe.com/blog/api-versioning))

Team Topologies offers a useful way to define interaction between the loops. Use temporary, high-bandwidth collaboration while product and architecture decisions are genuinely open; use a clear service boundary for approved contracts handed to implementation; bring the architecture owner back briefly to facilitate when implementation finds a real obstacle. Team Topologies calls these collaboration, X-as-a-Service, and facilitation. ([Team Topologies: key concepts](https://teamtopologies.com/key-concepts))

For implementation review, move the first design check earlier. Google’s code-review guidance says to raise major design concerns promptly because later work may depend on the faulty design; it also argues that smaller changes are easier to review, less likely to introduce bugs, and cheaper to redo if rejected. ([Google review guidance](https://google.github.io/eng-practices/review/reviewer/navigate.html), [Google on small changes](https://google.github.io/eng-practices/review/developer/small-cls.html))

## Proposed interface between loops

This is a Drover-specific proposal based on the preceding patterns. Treat each transition as a persisted event and compact artifact, rather than asking every agent to reconstruct the conversation.

| Event | Owner and payload |
|---|---|
| `intent-ready` | Product loop: user problem, desired behavior, success evidence, examples, non-goals, and appetite. |
| `contract-ready` | Architecture loop: repo paths and existing symbols to reuse; allowed files/modules; interface and invariants; alternatives considered; tests; risks; parallel dependencies; and choices the agent may make without asking. |
| `decision-needed` | Implementation loop: one unresolved choice, why it falls outside the contract, evidence gathered, options, recommendation, impact, and whether work can continue safely. |
| `decision-made` | Owner: selected option or delegated default, with rationale and any contract change. |
| `built` | Implementation loop: diff summary, actual files touched, commands and results, deviations, and remaining risks. |
| `accepted` / `rejected` | Owner or deterministic gate: acceptance evidence, or specific failed criteria sent back to the responsible loop. Rejection should identify whether the gap is behavioral, architectural, or implementation-level. |

Keep product acceptance examples separate from implementation instructions. The implementation packet should turn the architecture contract into concrete tasks: exact files or symbols, what to extend or replace, what not to add, task dependencies, and validation commands. Google recommends design docs for feedback on proposed implementation and says they become decision archives once implementation is complete; GitHub’s Copilot guidance similarly recommends giving agents project structure, build/test instructions, contribution rules, and technical principles. ([Google documentation best practices](https://google.github.io/styleguide/docguide/best_practices.html), [GitHub Copilot task best practices](https://docs.github.com/en/copilot/using-github-copilot/using-copilot-coding-agent-to-work-on-tasks/best-practices-for-using-copilot-to-work-on-tasks))

## Interruptions and the owner’s inbox

**Proposed policy:** let Drover continue within an approved contract when the choice is local, reversible, and covered by existing conventions and gates. Queue low-impact questions for a batch. Interrupt for unclear user behavior, a needed contract change, a cross-module or public-interface decision, destructive or security-sensitive work, or a blocker that prevents safe progress. This is an inference from Google PAIR’s guidance on control and trust calibration plus Gas Town’s severity-routed escalation model; neither source establishes a universal confidence threshold. GitHub Copilot automations provide a related documented example: they explain changes, rate confidence, and hold lower-confidence changes for review. ([Google PAIR: Explainability and Trust](https://pair.withgoogle.com/chapter/explainability-trust/), [Gas Town escalation protocol](https://github.com/gastownhall/gastown/blob/main/docs/design/escalation.md), [GitHub agent-powered workflows](https://docs.github.com/en/copilot/concepts/agent-capabilities/agent-powered-workflows-and-operations))

A good inbox item should fit on one screen:

- **Question and decision deadline**
- **Evidence:** relevant code paths, tests, or observations
- **Options:** usually two or three, with Drover’s recommended default
- **Consequence:** behavior, architecture, and rework affected by each option
- **Delay effect:** whether work pauses, continues on a safe branch, or misses a target
- **Response actions:** choose an option, approve the default, change the contract, or stop/re-scope

Batch routine choices, but don’t batch urgent blockers into a daily digest. Gas Town’s protocol gives escalations severity levels, routes them to different destinations, and supports stale re-escalation; its mail protocol asks agents to include what they tried. ([Gas Town escalation protocol](https://github.com/gastownhall/gastown/blob/main/docs/design/escalation.md), [Gas Town mail protocol](https://gastown.dev/docs/design/mail-protocol/))

## Parallel work and token use

Separate workspaces help isolate edits, but they do not make two tasks architecturally independent. GitHub documents parallel sessions in isolated worktrees; practitioners on Hacker News report that agents can still make incompatible decisions about the same code despite separate worktrees. **Recommendation:** partition by module or interface, declare file and symbol ownership before dispatch, and serialize shared contracts or shared-file changes. ([GitHub Copilot agent sessions](https://docs.github.com/en/copilot/how-tos/github-copilot-app/agent-sessions), [Hacker News discussion of parallel agents](https://news.ycombinator.com/item?id=47866750))

For tokens, prefer deterministic orchestration for known gates and routing; use model judgment only where interpretation is required. Anthropic distinguishes predefined workflows from agents that dynamically choose their steps, and recommends simpler patterns when they suffice. Route simple classification or extraction to small models, reserve stronger models for architecture choices and difficult reviews, and reuse stable prompt prefixes where the provider supports caching. Anthropic describes Haiku for high-volume classification, extraction, and routing; OpenAI notes that smaller models are usually faster and cheaper when used appropriately and documents prompt caching for repeated prefixes. ([Anthropic: Building Effective Agents](https://www.anthropic.com/engineering/building-effective-agents), [Anthropic model overview](https://platform.claude.com/docs/en/models/overview), [OpenAI latency optimization](https://developers.openai.com/api/docs/guides/latency-optimization), [OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching))

Practitioner posts offer useful but anecdotal signals: a popular r/ClaudeAI workflow recommends concise, handcrafted architecture guidance and persistent small task plans; an HN discussion describes constraining plans to a few files and using a roadmap to preserve progress. These are reports, not controlled evidence of quality gains. ([r/ClaudeAI workflow discussion](https://www.reddit.com/r/ClaudeAI/comments/1la5kp4/), [Hacker News: established codebases](https://news.ycombinator.com/item?id=46292682)) The sources describe practices and product workflows, not proof that any process will prevent 80% of issues. Drover should measure whether its own early contracts reduce architectural review findings and fix rounds.

## Implications for Drover

1. Add separate product-brief and architecture-contract artifacts; require `contract-ready` before multi-file implementation.
2. Make the contract repo-specific: named files/symbols, reuse targets, allowed changes, boundaries, test commands, and explicit “do not add” constraints.
3. Add a pre-edit architecture checkpoint for multi-file or high-risk work; let one-file, reversible changes use existing repo conventions and deterministic gates.
4. Partition parallel tasks by module/interface, assign file and symbol ownership, and serialize shared-file or contract changes; use worktrees for workspace isolation.
5. Implement `decision-needed` as a structured queue with options, recommendation, evidence, impact, deadline, and delay consequence; batch routine items and route urgent blockers immediately.
6. Review a small first slice or design-bearing diff before an agent builds the rest; reject contract drift at that point rather than after the full PR.
7. Route predictable checks and task categories deterministically, use small models for triage, reserve stronger models for design/review, and track cache hits and tokens per accepted change.
8. Track `accepted`/`rejected` reasons and owner interruptions; tune the gates against architectural rework and HOLD-to-fix rounds, not raw agent throughput.
