# Drover design

Drover coordinates coding workers from an owner's plan to checked commits.
The hub owns scheduling, durable state, approval and the website.
Workers execute bounded tasks in isolated worktrees.
This document defines the system's contracts; the glossary defines its terms.

## Ownership and placement

Windows is the home of the hub and the durable ledger.
WSL is a worker pool for Linux tools and workloads.
The dispatcher runs inside the hub process.
Each hub thread uses its own ledger connection.
WSL workers exchange run files with the hub through the cross-platform runner.
The optional WSL-facing HTTP listener and bearer token are disabled by default.
The watchdog starts WSL when queued work needs it.
WSL restarts affect its workers without taking down the owner website.
See ADRs 0001 and 0002 for the ownership and placement rationale.

## Durable state

The default state directory is `%USERPROFILE%\.drover` on Windows.
On Linux, the equivalent default is `~/.drover`.
`DROVER_HOME` selects the configuration home.
`paths.state_dir` may select a different runtime state directory.
One cross-repo SQLite ledger lives at `<state>/ledger.db`.
The hub is the only process that opens the production ledger.
SQLite uses WAL, foreign keys, a busy timeout and atomic claims.
A database file is never shared across the Windows/WSL filesystem boundary.
`runs/`, `rescue/`, `logs/` and `hub-token` also belong under state.
Installation applies an owner-only ACL to the state directory.

## Configuration

The primary configuration is TOML, parsed with the standard library.
Built-in defaults are overlaid by the home config and a checkout-local override.
Environment overrides apply last.
The checkout override is anchored to the Drover checkout, not a worker's cwd.
Tables merge by key; named trackers merge by name; seats replace as a list.
Unknown keys and invalid values produce errors naming the configuration key.
`[paths]` controls repo roots, worktrees, state and WSL settings.
The neutral repo-root default is `~/repos`.
`[[seats]]`, `[models]`, `[capacity]` and `[[trackers]]` describe execution resources.
`[policy]`, `[decider]` and `[hub]` control initial policy, local decisions and serving.
`[config_sync]` controls the optional private backup remote and automatic push.
The example TOML documents every supported key.

## Repo rules

One repo is one git repository with its own execution, checks and merge rules.
The target repo's committed `drover.toml` owns its rules and action script paths.
The one repo loader reads that file from the commit being worked or checked, then overlays
`<state>/repos/<name>.toml`. The machine file names checkout paths, worktree paths, default branch,
remote and machine environment. An explicitly configured directory may replace the private location.
A missing versioned file contributes an empty layer during migration; the merge is the same.
A legacy `programs/` directory is read only if `repos/` is missing and emits a deprecation warning.
Packaged neutral examples require `DROVER_EXAMPLES=1` and no private config directory.

`[scripts]` maps action names to relative `.ps1` paths in the repo.
Every action runs as `pwsh -NoLogo -NoProfile -NonInteractive -File <script>` with the worktree as cwd.
The repo's script owns build tools, test selection and output; its exit code determines the result.
PowerShell 7 and the repo's tools must already be installed on each selected platform.
`[run].platforms` constrains Windows/WSL placement; `default` selects the default.
`[run.env]` adds environment entries to workers and CI processes.
`[run].sandbox_writable_roots` allows extra writable paths below `/tmp`; the runner creates them with mode 0777.
`[gate].platform` selects Windows, WSL or both, with both required to pass.
Unknown test actions generate an owner question.
Placement outside the allowed platforms sends the task to the owner.
`[land].policy` selects single-task merging or merge groups; pushing defaults off.

## Tasks and write scope

A task has acceptance criteria, a file scope and at most one home repo.
A coding task writes only its home repo and merges as one squashed commit.
It may read other repos, branches and earlier reports as context.
A research task has no home repo and produces a report.
An epic groups tasks under a written plan, potentially across repos.
Task identity is distinct from an optional filesystem path in its metadata.
Overlapping write scopes in the same repo prevent simultaneous claims.
Dependency edges distinguish blocking work from informative waiting relationships.
Imported backlog starts proposed and requires owner approval before admission.
See ADR 0003 for the single-repo write contract.

## Paired tasks

A pair coordinates a provider and one or more consumers in other repos.
The tasks use matching branch names across those repos.
During development, each consumer's checks build against the provider worktree branch.
The provider must pass its checks and review and merge before its consumers.
Each consumer then runs checks again against the merged provider main.
A rejected provider holds its consumers.
A rework request contains a fixed label and an explanatory comment.
Labels are api-shape, missing-capability, bug, performance and other.
Routing tries a local decision, then a cheap worker, then the default director.
A merged provider or several consumers require the director.
A pair accepts at most two rework requests.

## Seats and allowance

A seat represents one signed-in account, identified by its configuration directory.
Claude seats use CLAUDE_CONFIG_DIR; Codex seats use CODEX_HOME.
Each seat carries priority, maximum workers, placement and allowance reserves.
A default director identifies a seat, a model and an effort level.
Borrowing another seat for that director requires owner approval.
Admission is based on account allowance rather than list-price dollars.
Priorities order seats; projected allowance expiry may take precedence.
A seat projected to lose at least 15% of unused weekly allowance goes first.
That threshold is tunable.
Claude usage is read headlessly per seat while active and before assignment.
The raw usage response is retained; parse failure alerts the owner.
Codex usage comes from rollout token-count rate-limit readings.
Manual usage entry is the fallback.
Director capacity is reserved before worker admission consumes it.

## Routing and resources

Workers start at a suitable model level for task size and risk.
Cheap workers handle small tasks, initial review and routine triage where effective.
Retries escalate through the configured escalation levels rather than retrying indefinitely.
The router considers eligible seats, placement, allowance headroom and reset time.
Trackers measure load, available RAM, disk space, WSL health and VRAM.
Queue thresholds admit no new work; hold thresholds stop work.
The setup wizard scales resource thresholds to the detected machine.
Per-seat worker limits and the global parallel cap both apply.
Inference can fall back to CPU when VRAM is above the configured threshold.
Resource admission is enforced in software.

## Runs and sessions

A run is one attempt at a task and records the policy version it starts under.
A session is one model conversation on exactly one seat.
A run may span several sessions through resume or handoff.
Resume requires the same seat and an unchanged worktree.
Otherwise a fresh session receives a handoff describing the work so far.
A seat limit pauses work until its allowance resets.
Context thresholds are configured per model.
Handoff is the main threshold and compaction is the safety net.
Director conversations compact without handing off to a different seat.
Named director sessions are resumable from the website on the same seat and OS.
Read-only transcript search and cached summaries provide context across sessions.

## Worker execution and recovery

Workers are headless CLI processes with structured output.
One cross-platform runner owns each worker's process tree.
Windows uses a job object; Linux uses a process group.
The runner enforces wall and idle timeouts and writes status and run records.
The hub records the intended placement and nonce before launching a run.
After restart, adoption uses run-id and nonce markers, never an arbitrary PID.
A failed process scan is unknown and cannot justify an orphan decision.
Two clean scans are required before an absent runner is considered orphaned.
Finish processing reuses an existing CI result rather than running checks twice.
Backoff prevents unavailable infrastructure from blocking the rest of the queue.

## Checks, reviews and merging

CI results apply to exact commit SHAs and unchanged, clean trees.
Check and CI actions use the same fixed PowerShell invocation on both platforms.
The scripts receive `DROVER_CHANGED_FILES`, a JSON string array, and `DROVER_CI_KIND`:
`checks` for task checks, `full` for merge CI, or `docs` for documentation-only checks.
Scoping and test policy belong entirely to the repo script.
Results keep the process exit code and output tail for owner review and triage.
Review verdicts also identify the exact reviewed SHA.
Merge groups exist only to share CI time across disjoint tasks.
Merge group size ranges from one to five and starts at three.
A clearly attributable failure drops the responsible task.
Ambiguous attribution gets one local choice; low confidence falls back to bisection.
Merge groups turn off when the median full CI duration is ten minutes or less.
`[landing]` defaults to mode local, push false, remote origin, main main; pr mode is reserved and rejected.
Local main is fetched and synchronized before branching and again immediately before merging.
Main advances through compare-and-swap update-ref; the owner checkout files and index stay untouched.
Local ahead-only history is retained for subsequent unpushed merges; two-sided divergence needs the owner.
Fetch failure pauses that repo and opens a retry alert, without using cached refs.
Remote movement during full CI replays the merge group and runs full CI on it again; replay conflicts need the owner.
Merging is local; optional push follows it and push failure alerts without undoing the merge.
The owner-only Merge queue page offers a POST Push main action for repos ahead of the fetched remote.

## Local decisions and tuning

Ollama supplies small structured judgements, with a primary and a shadow model.
The winner can be selected per question; large state uses the long-context model.
Decision outcomes record confidence, criteria version and later result.
Device selection supports auto, GPU and CPU.
CPU uses prepared `-cpu` variants with `num_gpu: 0`.
The wizard benchmarks devices when available and warns when preparation is incomplete.
Model licences must permit the intended use; setup asks about commercial use.
Tuning starts daily and backs off through 1, 1, 2, 3, 5 and 7 days.
The cadence advances only when a pass adopts no change.
Fewer than twenty new runs skips a pass without advancing it.
Cost deterioration, changed prices/models, a new repo or manual reset restarts cadence.
Every pass audits missed and needless escalations.
Automatic changes cover routing weights, thresholds, merge group size, cadence and small cap adjustments.
Brief templates, criteria, pinned rules and shadow promotion require owner approval.
Safety rules, installs and deployments are never automatically tuned.
Replanning freezes affected work and dependents while a fresh director returns a ledger diff.
A diff within epic scope that loosens no acceptance criteria can apply automatically.
Other replans require the owner.

## Decision history

State may be a private git repo holding config, repo rules, policy and decisions.
The wizard offers this by default and writes a runtime-data `.gitignore`.
Ledger databases, run files, rescue bundles, logs and tokens are excluded.
Decision records include date, author role, policy version, what changed and why.
They preserve the earlier reasons they supersede.
Owners and tuning changes must explain their choices against prior reasons.
Each record is committed locally when git history is enabled.
A private remote is strongly recommended; its visibility remains the owner's responsibility.
Automatic push is best effort and a failure preserves the local commit.
`python -m drover config sync` commits pending changes and retries configured sync.

## Owner interface and observability

The hub provides Inbox, Plan, Runs, Merge queue and Health views.
Owner questions support choices and free text; comments attach to tracked work.
The owner approves backlog, risky cleanup and policy proposals.
Scripted board rendering and notifications need no model calls.
The Stats contract retains task/run metrics and policy versions.
Hourly allowance and resource samples remain for 35 days, then daily samples for a year.
Run logs are retained for 30 days; durable task/run statistics remain.
Archiving hides tasks and deletion is owner-only.
Tailscale authentication protects remote owner access.
OIDC settings are reserved for a future authentication option.
