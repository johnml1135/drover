# Drover replaces the Herdr skill

Status: settled with the owner on 2026-10-31 (Q1-Q50). Decision record: ADR-0014.
Capability inventory and the 32 pane safety rules: the Herdr skill at
`H:\repos\my-skills\herdr`, chiefly `SKILL.md`, `references/supervision.md`, `wsl/ui_guard`
and `wsl/herdr-timers.py`.

## Goal

Drover owns policy, disk cleanup, dispatch and directors. Herdr the terminal stays as an
optional front end: directors and manual work run in panes, automatic workers stay
headless, and Drover works with Herdr closed. The skill is deleted capability by
capability. Order: disk, then dispatch, then directors.

## Phase 1: disk

**Purge.** Disposable paths idle 2 hours or more may be purged even from worktrees active
the same day; the 3-day whole-worktree rule is unchanged. Each repo declares disposable
paths in its committed `.drover/config.toml`. The shared `machine` repo is registered with
a local, uncommitted disposable policy.

**Budget.** Space used inside WSL is measured independently of H: free, and either can
trigger cleanup on its own. H: thresholds stay 150/75/50/25 GB free. WSL used:
- at 300 GB or more, purge least recently used disposable paths until 225 GB or less;
- at 500 GB or more, no new work starts and running work is not killed.

**Load.** Cleanup runs on at most 4 CPUs (configurable) at its existing low priority, on
both sides. It never aborts or defers because of machine load. Each side clears only what
it owns.

**Authority.** Unattended apply covers disposable paths only. Removing a whole worktree
needs approval unless it is settled and clean. When Drover cannot tell whether something
is in use, it keeps it; the cleanup task is not elevated.

**Overnight compaction.** Runs on Windows, once a night, from a scheduled elevated task.
Overnight work is autonomous only. Sequence:
1. Hold new WSL work and stop the hub's WSL polling, which otherwise re-wakes WSL.
2. Compaction stop: "stop now" to agents Drover started in WSL panes, up to 5 minutes;
   then a rude interruption, 1 minute; then kill what remains on the WSL side, including
   yanked and unaccounted processes.
3. Headless workers get 5 minutes, then are killed and requeued in their worktree.
4. `fstrim`, `wsl --shutdown`, read-only mount, `Optimize-VHD -Mode Full`.
5. Restart WSL, resume agents with "restart complete, keep going", release the hold.
6. Morning report: what was stopped, killed, resumed and reclaimed.

If a session will not resume, a fresh agent starts from the repo, ledger and brief, and
the report flags it. How each agent was started (seat `claude`, `claude-personal` or
`codex`, and Windows or WSL) is always recorded, because a wrong seat or side is expected
to explain most resume failures. `sparseVhd` stays off.

**Guard retirement.** `HerdrDiskGuard` stays until 3 clean applied days.

## Phase 2: dispatch

**Setting.** Each epic has Drover dispatch or Director dispatch, switchable by the owner at
any time. Tasks with no epic follow a global default. `dispatch_enabled`, `dispatch_repos`
and STOP stay as they are, above the setting. Switching changes only who picks next;
running work, checks, review and the merge queue carry on.

**Bypass.** The owner yanks one task. A director cannot yank, but may carry out yanked work
on the owner's instruction. The yank stops the run, leaves files alone and writes one
closing ledger entry, so the task is not dispatched again. After that Drover does not
watch or report on it, except cleanup: its builds are purged on the idle rule and its
worktree is removed when all committed, nothing running, 3 days idle, branch kept.
Handback is explicit and treated as new work.

**Landing.** The merge queue is the only route to main; direct landing is Bypass only.
Landings made outside Drover are detected in order: git (ancestor, patch-equivalent, or a
merge that adds nothing); then the `decider`, shadow first, and live only when it cites a
real commit on main; then an Inbox question. From `feat/manual-land` keep `contained()`,
`verify_manual_land()` and the owner's hold-clearing action, and drop the form. Reuse
`ops_outside.record_external_landing`. Push is a per-repo setting, off by default.

**Capacity and conflicts.** Concurrent agent panes and workers: a configurable cap starting
at 40. The skill's magnet-file and rebase-onto-unlanded checks become a dispatch hold.
`warden`, `velocity` and `context-watch` become hub trackers and the Inbox.

## Phase 3: directors in panes

**Role.** One Director role, one interactive director, multi-director parked (ADR-0012). A
fresh agent must be able to replace it from the repo, ledger and brief. Any configured seat
may be Director; Opus 5.5 is the default.

**Placement.** Directors and Drover management run in Windows Herdr. A WSL agent in a Windows
pane reports its own status through `herdr.exe` with `WSLENV`, by porting
`wsl/herdr-wsl-agent-state.sh` and extending it from Codex to Claude.

**Pane safety.** Port the 32 rules and their tests before any Director runs in a pane. The
adapter writes only to Drover's own idle, unfocused panes; other panes get durable mail. The
compaction stop is the single exception, for Drover-started agents in WSL panes. The Codex
"Trust this folder?" standing rule moves to Drover for worktrees of already trusted repos;
every other dialog stays owner-only.

**Context.** A director is asked for a handoff file at 275k tokens (configurable), rotates at
its next idle moment, and is forced at 300k. The file covers goal, done, decisions and
rejected options, standing rules, files touched, unverified work, next step, and what not to
redo. The new session restates the constraints before starting. Drover sends no `/compact`;
the owner may. Fresh agents load only short files matching their task through ADR
`applies_to`, under a hard size limit, with no general overview.

**Cadence.** Directors wake on events, grouped to at most one wake per configurable window,
default 10 minutes. Urgent events use a short configurable window. The default wake limit
rises from 4 to 6 per hour.

**Vocabulary.** Drover's glossary wins: "lane", "gate", "receipt", "READY", "LAND", "HOLD" and
"garden" retire, and panes stay panes.

## Skill retirement

Each capability is deleted from the skill at cutover, when its Drover replacement lands,
with no waiting period. Exceptions: `HerdrDiskGuard` waits 3 clean applied days, and the
lock and repair scripts wait for a verified real cross-side run. The skill itself goes last.

## Open issue

`config_validation.py` forbids Codex on Windows (ADR 0002: it corrupts the login registry). A
Codex Director therefore runs in a Windows pane with its process in WSL, so compaction stops
it, unlike a Claude Director. Phase 3 treats it as a Drover-started agent in a WSL pane.
