# Drover

Drover runs a fleet of AI coding agents against a tracked list of work, taking each change from an owner's plan to a
merged commit on main. It is for one owner (a person or a small team) who wants many agents working in parallel on
their own repos while keeping every decision, install and push under their control. "Drover" is only the product
name; everything mechanical uses the ordinary GitHub and CI vocabulary below.

The path of one change: task -> run -> checks -> review -> merge queue -> merge group -> full CI -> merged.

## People and roles

**Owner**:
The human who plans the work, makes decisions only a person may make, and authorizes installs, policy changes and pushes.

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

**Reserve**:
The share of a seat's allowance kept back for director and owner work, so that workers cannot consume it.
_Avoid_: Buffer, headroom

**Escalation level**:
One step in the order of models a task is tried on: level 1 is the cheapest (Luna), level 2 the next (Sol), and so on. A failed or stuck run moves to the next level; unstick escalation is the same steps used to recover a stuck task.
_Avoid_: Rung, tier, ladder

**Tracker**:
One watched machine resource (memory, a disk, load, GPU memory, the Linux side) with the levels at which new work queues or stops.
_Avoid_: Check, guard, monitor

**Placement**:
The operating system side, Windows or Linux, on which a run executes.
_Avoid_: Host, side, target

## Work

**Repo**:
One git repository Drover merges work into, with its own rules for where runs execute, how work is checked and how it merges.
_Avoid_: Program, project, rig

**Task**:
The single unit of tracked work: one ledger entry with acceptance criteria and a file scope; it writes to at most one repo, its home repo, and merges there as one squashed commit.
_Avoid_: Slice, bead, ticket, issue

**Research task**:
A task with no home repo, whose result is a report rather than a commit.
_Avoid_: Spike, investigation

**Context**:
What a task may read but not write: other repos, other tasks' branches, and earlier reports.
_Avoid_: Inputs, references

**Paired tasks**:
Tasks in different repos developed on matching branches, where one provider task's change is built against by one or more consumer tasks; the provider merges first.
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

**Waiting on <task title>**:
The state of a task frozen behind another task (a dependency or a re-plan in progress); it resumes by itself when that task is done.
_Avoid_: Blocked, replan hold, cascade

**Stuck**:
The state of a task that cannot make progress without help, after its escalation levels are used up or a run keeps failing.
_Avoid_: Blocked

**Import run**:
One pass that reads an external backlog and proposes its items as tasks for the owner to approve.
_Avoid_: Batch

## Checks, CI and merging

**Checks**:
A task's own lint, build and focused tests for the projects it touches. Passing checks is the UI stage "Implementation complete" and moves the task to checked.
_Avoid_: Gate, gated (as the stage)

**Full CI**:
The repo's full test suite, run once per change on a merge group at merge time.
_Avoid_: Test gate, full gate

**CI result**:
The record of one checks or full CI run on one exact commit and clean tree.
_Avoid_: Receipt, gate receipt

**CI runner**:
One slot for a checks or full CI run. Runners are per repo and configurable; how many run at once is a setting.
_Avoid_: Gate slot, lease (internal word, never shown)

**Review**:
An independent judgement of a checked change against its acceptance criteria and the repo's rules, ending in a verdict on one exact commit.
_Avoid_: Audit

**Changes requested**:
The review verdict that sends the task back for more work.
_Avoid_: HOLD

**Approved**:
The review verdict that lets the change join the merge queue.
_Avoid_: LAND

**Merge queue**:
Per repo, the ordered list of reviewed changes waiting to merge.
_Avoid_: Landing queue, landing tab

**Merge group**:
Up to five changes from the merge queue stacked and run through full CI together, purely to share one CI run and save time. A failure drops the responsible change; the rest proceed.
_Avoid_: Landing batch, batch (unqualified)

**Merge group commit**:
The commit produced by replaying a merge group onto main; full CI runs on it.
_Avoid_: Tip

**Merged**:
A change that has reached main after passing full CI; also "on main". Merging is local, and pushing is a separate owner-approved step.
_Avoid_: Landed, shipped

## Inbox

**Inbox**:
The one ranked place for everything that needs the owner. Its items are questions and owner tasks.
_Avoid_: Docket, needs-owner list, queue (for the owner)

**Question**:
An item in the Inbox that asks the owner for a decision, with choices or free text.
_Avoid_: Ticket, prompt

**Owner task**:
An item in the Inbox that asks the owner to do something, such as approving a pull request.
_Avoid_: Chore, action item

**Waiting on you**:
The label on any task or item stopped until the owner acts.
_Avoid_: Needs-owner

**Grill**:
An interview round in which a director asks the owner pointed questions to settle a plan or decision.
_Avoid_: Interrogation

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

## Cleanup

**Cleanup**:
The housekeeping that finds stale branches, worktrees and files; each cleanup run lists cleanup candidates, and deletions wait for the owner's approval.
_Avoid_: Garden, gardening, sweep

**Cleanup run**:
One pass of cleanup, started by the `drover cleanup` command or on a schedule.
_Avoid_: Garden run, batch

**Cleanup candidate**:
One item a cleanup run proposes to delete, shown in the Inbox until approved or declined.
_Avoid_: Garden deletion

## Rules the vocabulary encodes

- A task writes to at most one repo, its home repo; other repos are context and read-only (ADR 0003).
- A provider merges before its consumers, and each consumer re-runs checks against the merged provider; a rejected provider holds its consumers.
- A CI result applies only to an exact commit and an unchanged, clean tree; review verdicts name the exact reviewed commit.
- Check and CI commands are argument lists, never built from task-provided shell text.
- A merge group exists only to share CI time between disjoint changes; its size is one to five.
- Main advances by compare-and-swap, and the owner's checkout files and index are never touched.
- Merging is local; pushing is optional, off by default, and a failed push never undoes a merge.
- Borrowing another seat for a director, installs, safety rules and deployments need the owner and are never tuned automatically.
- Deleting a task is owner-only; imported backlog items start proposed and need owner approval.
- Only the hub opens the ledger; a database file is never shared across the Windows and WSL boundary.

## Retired terms

| Old | New |
|---|---|
| receipt, gate receipt | CI result |
| gate (unqualified), test gate | checks, or full CI, whichever is meant |
| gated (status) | checked |
| gate slot | CI runner |
| landing batch, batch | merge group (or import run, cleanup run) |
| tip (of a batch) | merge group commit |
| landing queue, Landing tab | merge queue |
| landed | merged, or on main |
| HOLD, LAND (verdicts) | changes requested, approved |
| rung, tier, ladder | escalation level (unstick ladder: unstick escalation) |
| replan hold, cascade | Waiting on <task title> |
| needs-owner | Waiting on you |
| blocked (user-visible) | Waiting on <task title>, or Stuck |
| docket | Inbox (`/dv-docket` becomes `/dv-inbox`) |
| garden, gardening | cleanup |
