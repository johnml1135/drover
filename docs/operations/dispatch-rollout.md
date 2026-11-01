# Dispatch settings, Bypass and outside-merge detection (owner runbook)

Run these steps in order. Nothing here is done by the build; each step is the owner's.

1. **Deploy the hub.** Follow `docs/hub-deployment.md` ("Each deploy"). The ledger migrates to version 18 on first open
   (it adds each epic's dispatch setting and when a task was picked); take the backup that deploy already takes.
2. **Check git.** Drover needs git 2.40 or later on every machine that runs the hub or holds a repo (`git --version`).
   With an older or failing git nothing is recorded and nothing is held; work carries on without the checks.
3. **Read the shadow answers.** `landed_outside` starts in shadow: the decider's answer is recorded and changes nothing.
   Git still proves a merge on its own, and anything git cannot prove opens one Inbox question for the task. Compare the
   recorded shadow answers with your own answers to those questions for as long as you like. A first scan of an old
   ledger can find a backlog: it reaches the Inbox five questions per scan, and the rest wait for later scans.
4. **Turn it live when the answers earn it.** Edit `decide-criteria.json` in the deployed tree and set `"mode": "live"` in
   the `landed_outside` entry, then restart the hub. `[decider] mode` does not change this question: its own entry wins.
   Live, the decider's answer counts only when it names a commit that is on main and that touches a file the task's
   recorded commit changed; any other answer, a decider that is down or an answer under the confidence threshold still
   opens the Inbox question. Setting it back to `"shadow"` returns to recording only.

## Settings

- `capacity.parallel_cap` comes from config only (default 40, range 1 to 64). A value stored in policy is ignored and the
  tuner no longer changes it; the resources verdict still paces launches.
- `capacity.launches_per_tick` (default 4, range 1 to 64) limits the reviews, planners and workers one dispatcher tick
  starts, so stopping yanked runs and reaping finished ones are never kept waiting behind a burst.
- `[dispatch] conflict_hold` (default true) and `magnets` hold a ready task while an active task in the same repo changes
  a file its scope covers (see `docs/configuration.md`). The check reads the repo's configured checkout; while that
  checkout cannot be read (WSL down, or the overnight compaction for a WSL repo) nothing is held.
- `[watch] interval_s` (default 600), `stalled_hours`, `red_rate`, `min_gates` and `context_tokens` set the Watch
  notices in the Inbox: machine hold, velocity and context size.
- Dispatch setting: per epic on the epic page; the default for tasks outside any epic on the Health page. Pausing
  dispatch, the repo filter and STOP still sit above it. Switching changes only who picks next.

## Behaviour to expect

- **Yank.** From a task's page the owner yanks it. Drover stops its run wherever it is (also while dispatch is paused),
  never restarts it, leaves its files alone and writes one closing entry. The task stays yanked, and tasks that depend on
  it keep waiting.
- **Abandon and Hand back are refused while the run is still stopping.** Retry when the run has ended. Abandon cancels the
  task and releases its dependents. Hand back makes a new draft with the same goal, acceptance criteria and scope that
  needs your approval; dependents now wait on it.
- **A merge outside Drover is recorded only with proof.** The task must have a recorded commit of its own, and git must
  show that commit (or the commits it was made from) merged on main, cleanly from every point the task passed through. A
  branch with no commits of its own, or one sitting at the base, is never a merge. A yanked task with no candidate
  commits is not watched and asks nothing; a task whose branch was deleted but which has candidates is asked about.
  A question retracts itself if git later proves the merge. A question offers at most eight commits, newest first, and
  says how many older ones it leaves out. A repo left out of `dispatch_repos` is not scanned at all, and when git cannot
  answer, or WSL is down for a WSL repo, nothing is asked and a later scan tries again.
- **Closed tasks keep their worktree for three days.** Cleanup now waits three days of quiet before removing the
  worktree of any closed task except a merged one. This is stricter for ordinary cancelled tasks than before. The branch
  is always kept, a dirty worktree is kept, and a yanked task needs no approval beyond the same rule.
