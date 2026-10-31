# Configuration

Drover has two configuration scopes. The machine's `<home>/config.toml` supplies seats, models, paths,
hub settings and named resource trackers. `DROVER_HOME` selects its directory; built-in defaults, that file,
and recognized environment variables apply in that order. Application overlays are removed.

The repository entry is `.drover/config.toml`, committed with `schema_version = 1`. Only that entry at the
selected commit supplies repository policy: scripts, run platforms/env/writable roots, CI, review, landing
and brief text. Dirty files, root `drover.toml` and unnamed siblings are ignored. Scripts must be canonical
POSIX relative `.ps1` paths beneath `.drover/scripts/`. Every action runs unchanged as
`pwsh -NoLogo -NoProfile -NonInteractive -File <script>` with the worktree as cwd; the repository and machine
provide their tooling. Future workflows use YAML under `.drover/workflows/*.yaml`; workflows are not implemented
in this phase.

After adoption, `<state>/repos/<name>.toml` contains only `name`, `repo`, `worktrees`, `remote`,
`default_branch`, optional `budget_gb`, optional `local_disposable`, and `[run.env]`. Machine env overrides repository env by variable name, including equal values.
Unknown keys and machine policy are errors naming the file and key. Missing committed entries preserve complete
machine fallback, including older script paths. An unreachable checkout permits startup fallback at HEAD;
exact revisions require a reachable checkout. A present invalid entry always fails.

Run `python -m drover config explain capacity.parallel_cap` for machine scope, or
`python -m drover config explain scripts --repo NAME --revision REV` for a repository at a commit.
The JSON contains the key, effective value and terminal source labels: default, repo registration, committed entry,
environment variable, transient owner answer, or derived dependencies. Selectors use numeric list indices;
escape literal dots and backslashes with a backslash. Sources are diagnostic data and never enter config files.

To migrate, commit the entry and scripts together, then install a backed-up repo registration containing only facts.
The wizard resolves all selected repositories before writing any state and never writes into their checkouts.
Rollback restores both repository and registration backups; older commits retain fallback behavior.
Legacy `resources.toml` and legacy resource dictionaries are unsupported. Move load/RAM thresholds to named
`[[trackers]]`, disk prefixes to named disk trackers, and GPU `vram_fallback_pct` to the vram tracker's
`fallback_pct` before removing old operational files. No ledger migration is required.

## Repository key reference

Every repository key is listed below. Tables must have the documented shape; unlisted keys and registration facts
in the committed entry are rejected. The defaults below describe omitted values; a missing `test` action remains unknown.

| Key | Meaning and default |
| --- | --- |
| `schema_version` | Required integer `1`; booleans and other versions are rejected. |
| `brief` | Worker rules/text; defaults to the empty string. |
| `scripts` | Arbitrary action-name to script-path table; defaults to `{}`. `test` supplies checks/full CI; optional `docs` supplies documentation checks. |
| `run.platforms` | Nonempty list drawn from `windows`, `wsl`; defaults to both in that order. Normalization removes duplicates and orders the effective list Windows first. |
| `run.default` | Must belong to `run.platforms`; defaults to Windows when allowed, otherwise WSL. |
| `run.env` | Environment-name to string table; defaults to `{}`. Machine entries override repository entries by name. |
| `run.sandbox_writable_roots` | Extra paths strictly below `/tmp`, without parent traversal; defaults to `[]`. The runner creates them with mode 0777. |
| `gate.platform` | `windows`, `wsl`, or `both`; defaults to the first supplied `run.platforms` entry, otherwise Windows. Every gate platform must be allowed by `run.platforms`. |
| `gate.timeout_s` | Overall CI wall-clock limit in seconds; defaults to `3600`. |
| `gate.idle_s` | CI output-idle limit in seconds; defaults to `1200`. |
| `gate.docs_only` | Boolean allowing documentation-only classification; defaults to `true`. |
| `gate.docs_only_globs` | Defaults to `["docs/**", "*.md", "GLOSSARY.md", "LICENSE", "!.github/**"]`; `!` excludes matches. An empty list classifies no changes as docs-only. Symlink changes always need checks. |
| `review.backend` | `claude` or `codex`; defaults to the selected review seat's backend, otherwise the backend whose machine model ladder contains the review model (Claude if none matches). |
| `review.seat` | Optional machine seat name; defaults to no pinned seat. It must exist and match `review.backend`. |
| `review.model` | Nonempty model name; defaults to machine `models.review` (built-in `sonnet-5.5`). If that default is outside the selected backend's ladder, use that ladder's last model. |
| `review.effort` | Optional nonempty effort string; defaults to no repository override. |
| `review.high_risk_model` | Nonempty model name; defaults to machine `models.director` for Claude (built-in `opus-5.5`), otherwise the resolved review model. |
| `landing.mode` | Defaults to `local`; `pr` is reserved and not implemented. |
| `landing.push` | Boolean, defaults to `false` unless legacy `land.push` supplies it. |
| `land.policy` | `batch` or `single`; defaults to `batch`. |
| `cleanup.disposable` | Declared disposable output paths; defaults to `[]`. Relative literal paths or globs, matched only to ignored or untracked files. |
| `land.push` | Legacy push alias; defaults to `false` and loses to explicit `landing.push`. |

`gate.unknown` and `gate.hint` are derived output fields, not accepted repository settings. A missing `test`
action is unknown; an owner may answer the existing script question. The precedence is:
committed test script > transient owner answer > machine fallback script.
An answer remains available across commit reloads even while a committed script shadows it. Only the existing
owner-answer mechanism authorizes it; writing a setting alone cannot authorize a script.

## Registration facts and defaults

The machine's `<state>/repos/<name>.toml` selects `name` and the required checkout `repo` path. Discovery uses
the filename stem when `name` is omitted. `worktrees` defaults to `<paths.worktrees>/<name>.worktrees` when a
shared machine root is configured, otherwise `<repo>.worktrees`. `remote` defaults to `origin`; `default_branch`
defaults to `main`. `worktrees` is the Windows placement's root. WSL-placed tasks get their worktree at
`<paths.wsl_worktrees>/<name>.worktrees/drover-<task>` instead (machine key `[paths] wsl_worktrees`, default `~/work`,
where `~` is the home of `paths.wsl_user` in `paths.wsl_distro`). It must be a Linux path on the distro's own
filesystem; config validation refuses `/mnt/<drive>` and Windows paths. The repository stays the one Windows clone
(ADR 0007): the worktree is created with the WSL git against it, so its `.git` file and the repository's
`worktrees/<id>/gitdir` record are relative paths only WSL can resolve; the worktree is locked with reason `wsl`, which
keeps Windows `git worktree prune` from dropping it. Repos that run only on Windows (`gate.platform = "windows"`) never
use the WSL root. Worktrees already under `/mnt/<drive>` finish in place and are removed by disk cleanup when settled;
cleanup inventories both roots, each from its own side. Optional machine `run.env` defaults to `{}`. No other keys are admitted after adoption.
Before adoption, the same machine layer may supply all repository policies and old relative script paths.
Legacy fallback `landing.remote`/`landing.main` override `remote`/`default_branch`; the wizard serializes the
resolved names into their canonical machine keys. These legacy landing names are rejected after adoption.
The wizard preserves omitted push during fallback, honors explicit booleans, and rejects supplied push after
adoption, including explicit `false`.

Machine-global keys, defaults, seat settings and resource tracker thresholds are listed in
[drover.example.toml](../drover.example.toml). `config explain` attributes explicit values even when equal to a
lower layer. Derived values name terminal dependencies; aggregate explanations enumerate their descendants,
including empty containers. Repository sources name `.drover/config.toml` and the full selected commit SHA.

To adopt an existing repo registration, first commit its policy in the repository entry,
then run `python -m drover.config migrate-registration NAME` with the usual `DROVER_HOME`
and state-directory configuration (the earlier name `migrate-machine` still works but prints a
deprecation notice). This dry run prints the proposed facts-only
file, lists policy keys, and reports whether the committed entry already supplies
them or its defaults match. Any policy difference refuses the migration and names
the machine and committed values. `--apply` saves a timestamped `.bak-…` file beside
the repo registration before writing the facts-only file. Repeating an applied migration
makes no further backup. Legacy `gate.commands` tables are reported as obsolete:
the loader already ignores them, and commands come from the committed `scripts.test`.
Commit the entry and script before applying; rollback restores both the repository
revision and the machine backup together. An already running hub needs its normal
managed reload after the coordinated rollout.


### Physical disks and cleanup declarations

Disk trackers measure physical free GiB (1024**3 bytes), including the host drive
of WSL storage. Windows and WSL default to H: 150/100/50/25 and C:
60/40/25/15 for `warn_gb`, `queue_gb`, `hold_gb`, `critical_gb`; Linux-only
hosts default to `/` with the data-drive tuple. Other drives use that tuple.
Levels apply strictly below each threshold; equal thresholds are permitted.
Unknown readings do not block. Warning triggers cleanup, queue stops new
workers, hold also stops new CI starts, and critical latches the pause switch.
Recovery releases queue/hold; only the owner clears a critical pause.

A repo that cannot carry Drover config (for example one owned by another organisation) may declare disposable paths
locally instead: `local_disposable = ["obj", "bin/**"]` in `<state>/repos/<name>.toml`. It takes the same relative paths or globs as
`cleanup.disposable` and is validated by the same code, so a bad entry is a configuration error. Cleanup removes the committed
list plus this one, without duplicates. It is read only from that owner-written registration; a committed `.drover/config.toml`
cannot set it, and no repo or worker writes it. Drover preserves it when it re-saves the registration.

Run `python -m drover config migrate-machine` to print the proposed global
config migration without writing. Add `--apply` for a byte-exact backup and
atomic replacement. This removes virtual disk trackers and resolves legacy
VHDX sources to physical paths; unresolved paths refuse migration. The existing
`migrate-machine NAME` migrates repository machine facts separately.

Global `[disk_cleanup]` `temp_root_budget_s` (default 300) bounds the scan of each temp root. When it is
exceeded the root is reported as `temp_root_partial`, scanning stops, and nothing unscanned is removed.

Global `[disk_cleanup]` `worktree_output_idle_s` (default 7200, finite and positive) is the idle time a worktree needs
before cleanup removes its declared disposable paths below hold (see below). It never shortens whole-worktree removal.

Global `[disk_cleanup]` `cooldown_s` (default 21600, 6 h) is the least time the hub waits after any cleanup it
launched (finished or failed) before launching another. The hub re-triggers only after free space falls 10 GB below
the last cleanup's result. Cleanup never consults machine load or RAM: it neither waits nor stops for them, and runs at
BelowNormal (Windows) or nice 10 (WSL), pinned to `cpu_limit` CPUs.

Global `[disk_cleanup]` `cpu_limit` (default 4, a positive integer) is the number of CPUs cleanup and its child
processes may use (the first N; never more than the machine has). Pinning is best effort.

Global `[disk_cleanup]` `wsl_purge_gb` (default 300), `wsl_target_gb` (default 225) and `wsl_stop_gb` (default 500)
are sizes in GB of the space used inside WSL, read from the running distro (a reading never starts WSL; an unreadable
one triggers nothing). Used space at or above `wsl_purge_gb` starts a cleanup on its own cooldown, whatever H: has
free; the cleanup aims for `wsl_target_gb`; at or above `wsl_stop_gb` new work stops starting. Each is finite and
positive, and `wsl_target_gb` < `wsl_purge_gb` < `wsl_stop_gb`.

The overnight WSL compaction has no settings. `scripts/Install-CompactionTask.ps1` registers `\DroverCompaction`,
nightly at 03:30, elevated, as the owner; a night the machine was off is not caught up. The task runs
`python -m drover.compaction`, which holds WSL through `disk/compaction-active.json` in the state directory. While that
file is under 2 hours old, new WSL work is held, nothing on a timer calls `wsl.exe`, and a cleanup that falls due waits.
The hub gives headless WSL runs 5 minutes from the start of the hold, then stops them and puts their tasks back on
ready in their worktrees. The job then kills what is left, runs `fstrim` and `wsl --shutdown`, compacts the VHDX
read-only, restarts WSL and releases the hold, even when a step failed. The result is in
`disk/compaction-report.json`, and anything but `compacted` raises an Inbox notice. `sparseVhd` stays off.

Cleanup deferrals (the cooldown, or another cleanup holding the lock) are recorded in the disk state as
`cleanup_deferred`; after 30 minutes of deferral while a drive is at queue or worse, one Inbox notice is raised and
retracted when cleanup runs. At the `critical` disk level the cooldown (kept per drive) is bypassed.

Windows load: there is no load average, so the `load` tracker reads CPU busy % (`GetSystemTimes`, smoothed over 60 s)
and maps it onto the per-core scale: 100% busy counts as about 3.68 per core. With the defaults (`queue_per_core` 2.5,
`hold_per_core` 3.5) new work therefore queues at about 68% busy and holds at 95%. Disk cleanup ignores this load. Dispatch is stricter on Windows than before, when load was unread. To tune it, set
`queue_per_core` and `hold_per_core` on the `load` tracker: the busy percentage is `per_core / 3.68`, so
`queue_per_core = 3.0` queues at about 82% busy.

Machine `[disk_cleanup]` accepts only `temp_roots = []`, `report_roots = []`
(absolute paths) and `caches = []`. A cache entry has either literal `argv`
and absolute `cwd`, or an absolute folder `path`; both require
`placement = "windows"` or `"wsl"`. Folder entries accept `max_age_days`
(default 30). Daily cleanup removes older files using owning-side access time,
falling back to modification time when access time is not later. Below warn,
folder cleanup removes least recently used files first and stops at warn. Repository machine files may declare a finite positive
`budget_gb` for an advisory budget.

Committed `.drover/config.toml` may declare `[cleanup] disposable = []`:
relative literal paths or globs such as `*.log` and `**/obj`, without dot
segments, absolute paths or `.git`. Cleanup reads these declarations from the
committed local default branch configured by the machine's `default_branch`
(`refs/heads/<branch>`, as in `migrate-machine`), including for older worktrees.
Worktree branches and machine files cannot widen the declarations. If the default
branch cannot be resolved or read, cleanup retains the affected content and
reports the unavailable evidence. Stage 5 removes only matching ignored/untracked
files, checking tracked files against each worktree's own index. At warn
or queue, a worktree must have no process using it and be idle at least
`[disk_cleanup] worktree_output_idle_s` (default 7200, 2 h); at hold or critical,
any age qualifies. This limit applies only to removing these disposable paths, which
leaves the worktree itself. Removing a whole worktree is separate and keeps its
three-day settlement. Live-task, ownership, busy-process and tracked-file checks
apply at any idle time. Dirty worktrees may qualify;
tracked files, source edits, protected paths and linked targets are retained.
Stage 4 also keeps worktree registrations safe to share between Windows and WSL, for every
registered repo and without new settings. Windows git cannot see a Linux path, so a
`git worktree prune` run from Windows drops the registration of every unlocked WSL worktree.
Drover creates each worktree locked with its side's name and sets
`gc.worktreePruneExpire=never` when it creates one. The stage backstops both: it reports
`prune_expiry_unset` and `unlocked_worktree` in a dry run, and on apply sets the expiry and
writes the missing lock (`windows` or `wsl`, from the worktree's recorded path; a lock note
already there is kept, and a worktree whose directory this side can see is gone is left
alone). A directory whose registration was lost is reported as `lost_registration` with the
branch it would join. On apply it is registered again, locked with this side's name, only if
a local branch is named like the directory (its path under the worktree root, so `hx/api-names` matches
`hx/api-names`; or its last component, where `drover-T1` also matches `drover/T1`),
that branch is not checked out in another worktree, and at most 10% of the branch's tracked
files differ from the directory (uncommitted work does not stop a match). Files are never
changed: the old `.git` file is saved under `<state>/disk/registration-backup/` and the
action is journaled in `disk-cleanup-manifest.jsonl`. Every other case is kept untouched and
reported as `lost_registration_unproven` with the reason (no branch named like it, branch in
use elsewhere, content mismatch) for the owner to resolve; when no branch is named like it, the
row's `suggestion` may name the one branch that alone holds one of its five newest files exactly
as committed, and it is never applied; a failed attempt is
`lost_registration_failed`. Worktrees nested below a root are found (to four levels, stopping at the first `.git`, skipping
links and build output). Each side handles only the roots it can open.

Stage 6 reports merged worktrees with live tasks as `merged_live_task` items;
cleanup never closes those tasks or removes their worktrees.

When `[disk_cleanup]` is absent, global `config migrate-machine` proposes editable
machine defaults before writing: the OS temp directory, `<state>/scratch`, and
existing agent scratch roots. Only tools detected on PATH receive literal cache
commands (uv cache prune, pip cache purge, npm cache clean --force, and dotnet
nuget locals http-cache --clear). Each command records its placement and absolute
working directory. Tools without a prune command may receive folder entries
(for example Cargo's registry cache); no folder is seeded for a tool with a
prune command. Existing `[disk_cleanup]` declarations, including empty lists,
are preserved. Review the dry report before applying the migration or installer.


Disk automation rollout uses the same configuration and state directory on both
placements. Run `python -m drover config migrate-machine` to inspect the proposal;
`--apply` saves a byte-exact timestamped backup before atomic replacement.
Inspect declared paths, then run `python -m drover disk-cleanup --dry-run --json`.
A completed report saves a receipt for that exact machine and scope. Changed
declarations require another report before any `--apply`.

`python -m drover disk-tracker install --dry-run` prints schedules and proposed
defaults without writing or registering anything. Plain `install` registers
report-only schedules; `install --apply` enables applied automation after the
matching report. A one-off `disk-cleanup --apply` changes only that run.
Install from native Windows Python on WSL hosts; installation from WSL refuses
to create Linux timers that would disappear with the reader.
Windows schedules run as the current interactive user with LeastPrivilege,
every 15 minutes and daily at 03:00. Linux-only machines use persistent systemd
user timers. These require the user session/user manager; they do not promise
pre-login coverage. Both pin interpreter, import root, config home and state.

Before enabling applied automation, wrap every external cleaner invocation:
`python -m drover disk-cleanup lock -- EXECUTABLE ARGUMENTS...`.
Native and WSL callers must map the same state directory. The wrapper holds the
shared directory lock for the entire child and returns its status. A hard-killed
holder is reclaimed automatically only when proven dead: owner.json records the holder's host, side
(windows/wsl) and process birth time, and the next acquire reclaims (only the holder of a `disk-cleanup.lock.reclaim` guard
directory re-proves the holder dead and renames the lock to a tombstone; an existing guard fails closed and is never
cleared automatically) when the same host and side show that pid absent or reborn. Age never steals a lock; a holder on the other
side or host, or with an unreadable owner.json, stays locked for deliberate inspection. While a lock blocks the hub's
disk tick it launches no triggered run, and after 30 minutes it raises one Inbox notice naming the lock and owner.
Drover never edits external cleaner scripts or retires them.

Keep herdr until the owner records seven consecutive calendar days of successful
applied daily reports for the same scope, without cleanup errors or protected
mutations. A missing, failed or scope-changed day restarts that release evidence.
The owner reviews the reports and decides retirement separately.

Restore removed checkouts using exact owning-side argv in
`disk-cleanup-manifest.jsonl`; branches survive and detached commits have rescue
refs. To roll back automation, disable its two schedules, retain disk state and
the journal for inspection, restore the configuration backup and previous code.
VHDX compaction remains a separate manual owner operation; cleanup never schedules it.

Windows review must verify interpreter/import-root availability after updates,
task XML quoting and current-user identity, physical-drive readings with WSL down,
toast failure with durable Inbox replay, cross-kernel lock contention, root trim
argv/distro pinning, junction protection and long paths, and capacity-hold release
while saved CI runners still recover.

An owner can designate one reviewed change as a base repair through
`landing.designate_base_repair(conn, repo_name, base_sha, task_id, reviewed_head)`.
The designation is stored in `landing_base_repair:<repo>` and applies only to that
base and reviewed head. It admits that task alone through a base-red hold; other
queue prerequisites still apply. Repairs require fresh full CI at the merge
commit on every configured platform, including documentation-only changes.
A failed repair cycle emits `landing.repair-blocked` once and pauses the
designation. Designate again after addressing the failure; infrastructure
recovery keeps its independent two-attempt limit for unchanged base/content.
