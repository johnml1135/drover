# Drover

Drover runs a fleet of AI coding agents against a tracked list of work, from an owner's plan to landed commits on main.

## People and roles

**Owner**:
The human who plans the work, makes decisions only a person may make, and authorizes installs and policy changes.

**Director**:
A strong model called fresh for one judgement, such as re-planning an epic, and then released.

**Default director**:
The seat, model and effort level used for a director judgement unless that kind of judgement names its own.
_Avoid_: Orchestrator, lead (for the role)

**Worker**:
The role that carries out one run of one task in its own worktree.
_Avoid_: Lane, agent (for the role)

## Capacity

**Seat**:
One signed-in account of one model backend, with its own allowance, priority and limit on concurrent workers.
_Avoid_: Slot, login, profile

**Allowance**:
The usage a seat may still spend before its window resets.
_Avoid_: Quota, credits, budget

**Tracker**:
One watched machine resource (memory, a disk, load, GPU memory, the Linux side) with the levels at which new work queues or stops.
_Avoid_: Check, guard, monitor

**Placement**:
The operating system side, Windows or Linux, on which a run executes.
_Avoid_: Host, side, target

## Work

**Repo**:
One git repository Drover lands work on, with its own rules for where runs execute, how work is checked and how it lands.
_Avoid_: Program, project, rig

**Task**:
The single unit of tracked work: one ledger entry with acceptance criteria and a file scope; it writes to at most one repo, its home repo, and lands there as one squashed commit.
_Avoid_: Slice, bead, ticket, issue

**Research task**:
A task with no home repo, whose result is a report rather than a commit.
_Avoid_: Spike, investigation

**Context**:
What a task may read but not write: other repos, other tasks' branches, and earlier reports.
_Avoid_: Inputs, references

**Paired tasks**:
Tasks in different repos developed on matching branches, where one provider task's change is built against by one or more consumer tasks; the provider lands first.
_Avoid_: Linked tasks, cross-repo task

**Rework request**:
A consumer's labelled, explained request that its provider change its work, accepted or declined by a judgement.
_Avoid_: Bounce, reopen

**Run**:
One worker's attempt at one task. A task may have several runs (retries, escalations), and a run may span several sessions.
_Avoid_: Attempt, job

**Session**:
One model conversation and its accumulated context, belonging to exactly one seat; it may be resumed, handed off to a fresh session, or named and returned to later.
_Avoid_: Chat, thread, conversation

**Handoff**:
Ending a session and starting a fresh one that receives a written account of where the work stands.
_Avoid_: Rotation, reset

**Director session**:
A named session between the owner and a director, belonging to no task, that may draw on other sessions' transcripts.
_Avoid_: Planning chat, console

**Epic**:
A group of related tasks sharing one written plan that keeps them coherent; its tasks may be in different repos.
_Avoid_: Convoy, area, program (for the grouping)

**Report**:
The written findings of a run, kept in the ledger, that later tasks may be given as context.
_Avoid_: Notes, handoff, summary

**Ledger**:
The official, tracked record of every task, its state, its runs and its history.
_Avoid_: Backlog file, task list, PLAN.md

## Tuning

**Policy**:
The versioned bundle of routing weights, brief templates, decision criteria and pinned rules in force; a run keeps the version it started under.
_Avoid_: Config, batch, settings

**Decision record**:
A written account of one choice about how Drover is configured or tuned, who made it, why, and what it replaced.
_Avoid_: ADR (outside the codebase), changelog, note

**Tuning pass**:
A scheduled review of logged outcomes that may publish a new policy version.
_Avoid_: Retro, optimisation run

## Landing

**Landing batch**:
A group of gated tasks merged to main together, purely to share one test-gate run and save CI time.
_Avoid_: Batch (unqualified)
