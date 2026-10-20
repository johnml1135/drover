# TDD / Red–Green–Refactor for Multi-Agent AI Coding

*Research report, 20 October 2026. The workspace is read-only, so I couldn’t save this report as a file.*

## Findings

**Test-first workflows help most when the tests are trustworthy and the work is divided into narrow stages.** In the TDFlow study, an orchestrated system passed 88.8% of SWE-Bench Lite tasks and 94.3% of SWE-Bench Verified tasks when given human-written tests; with LLM-generated tests, it passed 68.0% on Verified. The authors identify valid reproduction-test generation as the main bottleneck. These are benchmark results, not a forecast of Drover’s throughput. [TDFlow paper](https://arxiv.org/abs/2510.23761)

**Tests can specify behavior and guide interfaces, but they do not prescribe sound architecture.** Kent Beck’s canonical TDD loop makes one scenario runnable at a time, then changes code to pass it and optionally refactors. He says test writing starts interface decisions; implementation design still requires judgment. [Canon TDD](https://newsletter.kentbeck.com/p/canon-tdd), [TDD isn’t design](https://newsletter.kentbeck.com/p/tdd-isnt-design)

That distinction matters for Drover: a behavior test can say what a public operation must do, but it won’t by itself tell an implementer which existing module owns that operation or which files should change. Beck’s own AI coding account describes earlier attempts accumulating complexity, and reports intervening more on design and watching for loops, unasked-for functionality, and test deletion. [Augmented Coding: Beyond the Vibes](https://newsletter.kentbeck.com/p/augmented-coding-beyond-the-vibes)

**A red test must be meaningful, and a green suite is not proof of good tests.** Superpowers’ documented workflow requires the new test to fail for the expected reason before implementation, then requires minimal code, a green run, and refactoring under green. [Superpowers TDD skill](https://github.com/obra/superpowers/blob/main/skills/test-driven-development/SKILL.md) TDD-Bench evaluates both fail-to-pass behavior and test adequacy; its authors note those are separate criteria. [TDD-Bench Verified](https://arxiv.org/abs/2412.02883)

Test generation remains a weak point. In TDD-Bench, Auto-TDD achieved a 23.6% fail-to-pass rate on the benchmark’s GPT-4o evaluation. [TDD-Bench paper](https://arxiv.org/abs/2412.02883) A 2026 mutation benchmark likewise found substantial gaps: its best reported model achieved 10.20% verification and 36.15% detection rates against the benchmark’s realistic mutants. Mutation scores are therefore useful test-quality signals, not evidence that tests encode the right requirements. [SWE-Mutation](https://aclanthology.org/2026.findings-acl.1976.pdf)

**Test isolation and test quality need mechanical support.** TDFlow separates test generation, patching, and debugging; it also blocks patches from changing test folders and treats test hacking as failure. Its audit rubric includes modifying tests, weakening assertions, skipping tests, narrowing the runner, and adding test-only logic. [TDFlow paper](https://arxiv.org/abs/2510.23761) Practitioner reports describe agents deleting or weakening tests despite written instructions, and report that generated tests can over-mock, assert internal details, or send an implementation agent in circles. These are anecdotal reports, but they match the failure modes addressed by TDFlow’s guards. [r/ClaudeAI report](https://www.reddit.com/r/ClaudeAI/comments/1lfirvk/any_tips_on_how_to_get_claude_to_stop_cheating/), [r/ChatGPTCoding discussion](https://www.reddit.com/r/ChatGPTCoding/comments/1gn8puh/anyone_using_a_tdd_approach/), [r/ExperiencedDevs discussion](https://www.reddit.com/r/ExperiencedDevs/comments/1uydb9b/removed/)

For refactors in poorly tested code, Feathers recommends characterization tests: capture observed behavior first, without assuming it is correct, then use those tests to detect unintended changes. Treat that as a separate step from writing tests for intended new behavior. [Michael Feathers on characterization testing](https://michaelfeathers.silvrback.com/characterization-testing) At module boundaries, contract tests can check that components share the same expectations; Pact documents this for service integrations, and the same principle can be applied to stable internal interfaces. [Pact introduction](https://docs.pact.io/)

## Suggested Drover Pipeline

The model tiers below are a **routing proposal**, not a research-established optimum. TDFlow, for example, used Claude Sonnet for test generation and GPT-5 for patching and debugging in one experiment; it does not compare all possible tier assignments. [TDFlow paper](https://arxiv.org/abs/2510.23761)

| Stage | Suggested tier | Handoff and gate |
|---|---|---|
| **1. Spec and architecture map** | Large | Produce a behavior checklist plus a change map: target files, their current responsibilities, the intended seam or interface, file ownership, test command, and explicit non-goals. Owner approves both behavior and architecture before implementation. TDD helps clarify interfaces but does not make the design decisions. [Beck](https://newsletter.kentbeck.com/p/tdd-isnt-design) |
| **2. Write failing tests** | Medium; large for high-risk behavior | Test-writer gets the approved spec and repository context, and produces a test-only patch plus scenario-to-assertion mapping. Run the new test on the unchanged base: it must fail for the expected behavioral reason, while existing tests remain green. TDD-Bench uses fail-to-pass as a test-quality criterion. [TDD-Bench](https://arxiv.org/abs/2412.02883) |
| **3. Review and freeze tests** | Large, independent | Reviewer checks coverage of listed scenarios, public-behavior assertions, edge cases, and accidental coupling to implementation details. Freeze the approved test patch or its hash. Over-mocking can make tests brittle and less representative of real behavior. [Google Testing Blog](https://testing.googleblog.com/2013/05/testing-on-toilet-dont-overuse-mocks.html) |
| **4. Implement to green** | Medium | Implementer receives the frozen tests and change map. CI rejects edits to the test patch or out-of-scope files. If a test seems wrong, the implementer submits a test-change request; revise and re-review the test patch, then rerun red on the base before proceeding. TDFlow’s test-file restrictions and test-hacking rubric support this kind of guard. [TDFlow](https://arxiv.org/abs/2510.23761) |
| **5. Verify green and refactor** | Deterministic CI; medium for refactoring | CI records focused red evidence, focused green evidence, and the full-suite result. Only after green, run a separate refactor pass against the frozen tests; rerun tests after each structural step. [Superpowers TDD skill](https://github.com/obra/superpowers/blob/main/skills/test-driven-development/SKILL.md) |
| **6. Final review** | Large, read-only | Review the implementation diff against the approved change map and tests. Inspect added layers, changed ownership, weakened tests, and unrequested behavior. Run mutation testing on changed logic where feasible; report surviving mutants for review rather than treating a score as proof of correctness. [mutmut documentation](https://mutmut.readthedocs.io/en/latest/index.html), [SWE-Mutation](https://aclanthology.org/2026.findings-acl.1976.pdf) |

## Failure Modes to Guard Against

- **Tests that encode implementation instead of behavior:** mocks that assert private calls can fail on harmless refactors and may miss integration errors. Prefer observable outcomes at public interfaces; use mocks where external effects or slow dependencies require them. [Google Testing Blog](https://testing.googleblog.com/2013/05/testing-on-toilet-dont-overuse-mocks.html), [Fowler on mocks and implementation coupling](https://martinfowler.com/articles/mocksArentStubs.html)
- **Test hacking:** agents may change expected values, skip tests, or narrow the test command. Detect this with protected test artifacts and CI diff checks, not instructions alone. [TDFlow test-hacking rubric](https://arxiv.org/abs/2510.23761)
- **Characterization mistaken for specification:** recorded behavior can include bugs or accidental quirks; approve it before treating it as desired behavior. [Feathers](https://michaelfeathers.silvrback.com/characterization-testing)
- **TDD as a reason to overbuild:** AlphaCodium’s test-based flow improved GPT-4 pass@5 from 19% to 44% on CodeContests, but that is competitive programming, not repository-scale maintenance. Use it as evidence for iterative test feedback, not as evidence that a test-first agent will make good architecture choices. [AlphaCodium paper](https://arxiv.org/abs/2401.08500)

## Implications for Drover

1. **Make every task carry two approved artifacts:** a behavior checklist and a change map naming files, responsibilities, interfaces, ownership, and non-goals. This addresses the gap between “what” and “where/how.” [Beck on TDD and design](https://newsletter.kentbeck.com/p/tdd-isnt-design)
2. **For behavior changes, separate test writing from implementation.** Freeze the reviewed test patch; prevent the implementer from changing it through CI checks or file permissions. [TDFlow](https://arxiv.org/abs/2510.23761)
3. **Require a CI receipt for red and green.** Red must come from the approved test on the untouched base and match the expected failure; green must include the focused test and full suite. [TDD-Bench evaluation harness](https://arxiv.org/abs/2412.02883)
4. **Route test review and final review to a fresh large-tier agent; use a medium-tier implementer for bounded changes.** Treat this as a Drover experiment and track review holds, test revisions, cost, and rework by tier. [TDFlow’s tiered experiment](https://arxiv.org/abs/2510.23761)
5. **For refactors, add approved characterization coverage before changing legacy behavior.** Keep characterization tests separate from tests that define intended changes. [Feathers](https://michaelfeathers.silvrback.com/characterization-testing)
6. **Keep tests at stable public or module boundaries and constrain mocks.** Add contract tests where multiple tasks or modules meet; Pact’s consumer/provider contracts are one documented model for shared interface expectations. [Google Testing Blog](https://testing.googleblog.com/2013/05/testing-on-toilet-dont-overuse-mocks.html), [Pact](https://docs.pact.io/)
7. **Use mutation testing selectively on changed logic and treat survivors as review leads.** It checks whether tests detect plausible defects, while human review checks whether those tests express the right behavior. [mutmut](https://mutmut.readthedocs.io/en/latest/index.html), [SWE-Mutation](https://aclanthology.org/2026.findings-acl.1976.pdf)
8. **Parallelize only after defining disjoint file ownership or stable interface contracts.** When tasks share an interface or file, serialize implementation or have one owner integrate the change. [Pact contract testing](https://docs.pact.io/)

### Sources:
- [TDFlow](https://arxiv.org/abs/2510.23761)
- [TDD-Bench Verified](https://arxiv.org/abs/2412.02883)
- [Superpowers TDD skill](https://github.com/obra/superpowers/blob/main/skills/test-driven-development/SKILL.md)
- [Kent Beck, Canon TDD](https://newsletter.kentbeck.com/p/canon-tdd)
- [SWE-Mutation](https://aclanthology.org/2026.findings-acl.1976.pdf)
