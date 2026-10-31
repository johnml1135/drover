---
schema_version: 1
id: ADR-0014
status: accepted
date: "2026-10-31"
decision-makers: [johnm]
applies_to: ["drover/**", "hub/**", "docs/**", "GLOSSARY.md"]
enforcement: null
supersedes: []
links:
  - rel: related
    target: docs/adr/0012-finite-execution-as-default.md
---

# Drover replaces the Herdr skill, with dispatch set per epic and Bypass outside it

Disk cleanup and agent supervision were split between Drover and the Herdr
skill, and the overlap left real gaps: Drover's cleanup had never applied a
deletion. Drover now owns policy, cleanup and directors, and Herdr stays as an
optional terminal front end. This reorders ADR-0012's priorities to disk first,
then dispatch settings, then directors in panes; its finite-execution default
stands, with one interactive director and multi-director work parked.

## Decision

Each epic has one dispatch setting, Drover dispatch or Director dispatch, and
tasks outside any epic follow a global default. Switching changes only who picks
the next work. Checks, review and the merge queue apply under both, and the merge
queue is the only route to main.

Bypass is not a third setting. The owner yanks one task, which stops its run,
leaves its files alone and writes one closing ledger entry. A director may carry
the work out on the owner's instruction. Drover then neither dispatches, watches
nor reports on it, except that disk cleanup still purges its build output and
removes its worktree under the settled rule. Returning work needs an explicit
handback and is treated as new work. Landings made outside Drover are detected
from git, then by the decider citing a commit on main, then by an Inbox question.

There is one director role, whether a person steers it or Drover starts it, and a
fresh agent must be able to replace it from the repo, ledger and brief alone.
Long sessions rotate through a handoff file instead of compacting. Directors run
in Windows Herdr, so WSL compaction cannot take them down. Drover writes only to
its own idle, unfocused panes, and the compaction stop is the single exception,
for agents it started in WSL panes.

## Considered options

- Three modes, with Bypass as a recorded third mode: rejected, because the owner
  needs a way to take work fully outside Drover, and no surveyed product offers
  three authority modes.
- Dispatch set per repo: rejected, because an epic is the logical grouping of
  work and may span repos. The repo dispatch filter stays as a separate switch.
- Directors in WSL Herdr: rejected, because compaction would stop them.

## Consequences

Yanked WSL jobs are killed by the compaction stop like any unaccounted process,
and the morning report names them. WSL agents in Windows panes must report their
own status to Herdr, which cannot detect them by process.
