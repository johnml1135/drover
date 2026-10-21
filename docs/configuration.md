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
`default_branch`, optional `budget_gb`, and `[run.env]`. Machine env overrides repository env by variable name, including equal values.
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
defaults to `main`. Optional machine `run.env` defaults to `{}`. No other keys are admitted after adoption.
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

Run `python -m drover config migrate-machine` to print the proposed global
config migration without writing. Add `--apply` for a byte-exact backup and
atomic replacement. This removes virtual disk trackers and resolves legacy
VHDX sources to physical paths; unresolved paths refuse migration. The existing
`migrate-machine NAME` migrates repository machine facts separately.

Global `[disk_cleanup]` `temp_root_budget_s` (default 300) bounds the scan of each temp root. When it is
exceeded the root is reported as `temp_root_partial`, scanning stops, and nothing unscanned is removed.

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
or queue, a worktree must have no process using it and be idle at least three
days; at hold or critical, any age qualifies. Dirty worktrees may qualify;
tracked files, source edits, protected paths and linked targets are retained.
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
