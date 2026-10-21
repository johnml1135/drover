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
| `cleanup.build_outputs` | Declared disposable output paths; defaults to `[]`. Canonical relative literal paths only. |
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

Machine `[disk_cleanup]` accepts only `temp_roots = []`, `report_roots = []`
(absolute paths) and `caches = []`. Each cache entry has literal `argv`, an
absolute `cwd`, and `placement = "windows"` or `"wsl"`. There are no default
cache commands. Repository machine files may declare a finite positive
`budget_gb` for an advisory budget.

Committed `.drover/config.toml` may declare `[cleanup] build_outputs = []`:
canonical relative literal paths, without globs, dot segments or `.git`.
Machine files cannot override this policy after adoption. Declared outputs
still require clean, idle worktrees and ignored/untracked containment evidence;
tracked files and protected or linked targets are retained.

When `[disk_cleanup]` is absent, global `config migrate-machine` proposes editable
machine defaults before writing: the OS temp directory, `<state>/scratch`, and
existing agent scratch roots. Only tools detected on PATH receive literal cache
commands (uv cache prune, pip cache purge, npm cache clean --force, and dotnet
nuget locals http-cache --clear). Each command records its placement and absolute
working directory. Existing `[disk_cleanup]` declarations, including empty lists,
are preserved. Review the dry report before applying the migration or installer.
