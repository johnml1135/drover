# 0003: A task writes at most one repo

Date: 2026-10-17 · Status: accepted

## Context

A change may need context from several repositories, but a task needs a clear write scope, acceptance checks and
merge decision. Allowing one task to mutate several repositories makes review, retries and rollback ambiguous.
Some changes still need a provider and its consumers to evolve together.

## Decision

A coding task has one home repo and writes only there. Its context may include other repos, branches and earlier
reports, all read-only. A research task has no home repo and ends in a report rather than a commit.

Changes spanning repositories are paired tasks on matching branch names: one provider and one or more consumers.
A consumer's checks build against the provider worktree branch while the pair is in development. The provider merges
first. Each consumer then re-runs checks against the merged provider main before it can merge. If the provider is rejected,
its consumers are held.

## Consequences

- Write scope, worktree, review and merging remain local to one repo.
- A consumer may read provider work without gaining permission to edit it.
- Green consumer checks against an unmerged provider are insufficient for final merge.
- An epic may coordinate tasks in several repos without expanding any task's write permission.
- Cross-repo changes require dependency tracking and a provider-first merge order.
