# Drover

Drover runs a headless fleet of AI coding workers, taking approved tasks from a plan to tested, reviewed commits.
A Windows hub owns the queue and dashboard; Windows and WSL workers do the work in isolated git worktrees.

Give Drover a backlog and stay in control of the decisions that need an owner:

- **Headless workers:** Claude and Codex CLIs run without terminal panes, with live logs in the hub.
- **Local decision models:** small judgement calls run through Ollama, with shadow comparisons and CPU model variants.
- **Allowance-aware seats:** admission protects account reserves and considers available allowance and reset time.
- **Owner approval:** imported tasks remain proposed until approved. Unknown gates and risky changes become questions.
- **Self-tuning:** recorded outcomes guide routing, confidence thresholds, worker limits and landing batch sizes.
- **Checked landing:** gates and reviews apply to exact commits. Work lands locally by default; pushing is a separate policy.

Drover is a working prototype. Begin with a small task and inspect its gate, review and landing before admitting a backlog.

## Quickstart

You need Windows Python 3.13+, git, `uv`, and at least one signed-in worker CLI. For Linux workers, use WSL2 with
Python 3.12+ and the CLI installed inside the distro. Install PowerShell 7 (`pwsh`) on each platform that runs repo actions. Install Ollama and suitable local models to enable model
judgements. Tailscale provides authenticated access from another device.

From your Drover checkout in PowerShell:

```powershell
uv sync --project hub
uv run --project hub python -m drover init
uv run --project hub python -m hub
```

The wizard detects paths and tools, previews its TOML, then writes your configuration. It asks about commercial
use because some decision models have non-commercial licences. CPU execution uses prepared `-cpu` model variants;
when Ollama was unavailable during setup, run `python -m drover.decide ensure-cpu-models` before using CPU decisions.

The default state directory is `%USERPROFILE%\.drover` (`~/.drover` on Linux). It holds `config.toml`, private
`repos/*.toml`, the ledger, run files and decision history. The default repo root is `~/repos`; set your own in the
wizard. If you choose a different state directory, follow its printed `DROVER_HOME` instructions.

By default, the wizard makes the state directory a git repo and excludes runtime data. A private remote is strongly
recommended for backup. **The remote must be private:** configuration and decision records may describe private work.
Use `python -m drover config sync` to commit pending edits and push when configured; push failure keeps local history.

Open `http://127.0.0.1:8765`. For installation at login and remote access, follow the
[Windows installation guide](hub/scripts/README-INSTALL.md).

### Add a repo

Re-run `python -m drover init` and select a discovered repo. Alternatively, copy
[acme-lib.toml](repos/acme-lib.toml) into `<state>/repos/`, then edit its name, repo path,
worktree path, default branch and remote. [acme-app.toml](repos/acme-app.toml) is another neutral example.
Repo registrations hold registration facts; repository rules belong in a committed `.drover/config.toml`:

```toml
schema_version = 1

[scripts]
test = ".drover/scripts/test.ps1"

[run]
platforms = ["windows", "wsl"]
default = "wsl"

[gate]
platform = "both"
```

Commit the named PowerShell script with that file. Drover runs every action as
`pwsh -NoLogo -NoProfile -NonInteractive -File <script>` with the worktree as cwd.
The script owns tool setup and test selection; its exit code determines pass or fail.
Task checks receive `DROVER_CI_KIND=checks` and `DROVER_CHANGED_FILES` as a JSON path array;
full CI receives kind `full`, and documentation checks receive `docs`.
PowerShell and the repo's tools must already be installed on each selected platform.

The loader reads `.drover/config.toml` from the commit being worked or checked. After adoption, the machine
file may contain only `name`, `repo`, `worktrees`, `remote`, `default_branch` and `[run.env]`; policy leftovers
are errors. Machine env overrides repository env by variable name. Without the committed entry, complete
machine policy remains the migration fallback. Dirty config files and root `drover.toml` are ignored.
The repository's `[run].sandbox_writable_roots` declares extra writable paths below `/tmp`; the runner creates
them with mode 0777. Repo brief text belongs in the versioned entry.
Inspect origins with `python -m drover config explain scripts --repo NAME`, or inspect effective values with
`python -m drover.repos --json`. See [configuration and migration](docs/configuration.md).

Packaged examples are loaded only with `DROVER_EXAMPLES=1`, when no private `repos/` directory exists. A legacy
`programs/` directory is read with a deprecation warning; move its files into `repos/` when convenient.

### Approve a task

Import a proposed backlog through the hub API (`POST /api/v1/import`) or create a task through `POST /api/v1/tasks`
with its `repo`, goal, acceptance criteria and file scope. API clients use the configured authentication.
In the hub's Inbox, inspect the proposed task and choose Approve. Enable dispatch when ready.
The Plan and Runs views show progress; owner questions appear in the Inbox.

Versioned repo configs support `[landing]` with `mode = "local"`, `push = false`, `remote = "origin"`, and
`main = "main"` by default. `mode = "pr"` is reserved and rejected. `[land].policy` still selects batch or single.
Before new task branches and before landing, Drover fetches remote main and fast-forwards the local main ref.
It preserves local unpushed landings; histories with commits unique to both sides stop in `needs-owner`.
A failed fetch pauses only that repo and opens an owner alert; answer it to retry.
When origin moves during gating, the batch is replayed on the new main and gated again; conflicts need the owner.
Main's checkout files and index are never updated, so its displayed status may change when the ref advances.
The owner can push ahead commits using **Push main** on the Landing tab. `push = true` pushes after a local landing;
a failed push alerts the owner and retains the landed task. No push uses force.
Legacy `remote`, `default_branch`, and `[land].push` remain supported; `[landing]` values take precedence.

Every coding task has one home repo. Cross-repo work uses paired tasks, with the provider landing first and the
consumer testing again against the landed provider. Research tasks produce reports. See [ADR 0003](docs/adr/0003-task-writes-one-repo.md).

## Reference

- [Current design](docs/design.md), [vocabulary](GLOSSARY.md), [dispatcher details](docs/dispatcher.md)
- [Configuration reference](drover.example.toml), [architecture decisions](docs/adr/)

## Licence

Drover's own code is licensed under the [MIT License](LICENSE).

Some local decision models are licensed for non-commercial use. Model weights and worker tools have their own
licences, separate from Drover's code. The setup wizard asks about commercial use; choose models whose terms
permit your intended use and check their licences before deployment.

`drover/` uses the Python standard library. The hub uses FastAPI, Jinja2 and Uvicorn.
Run checks from the checkout root:

```sh
pwsh -NoLogo -NoProfile -NonInteractive -File .drover/scripts/test.ps1
```

The script selects the platform's Python, runs `python -m unittest discover -s tests -t .`,
cleans up its temporary fixtures, and prints thread stacks if the suite stalls. Install the hub dependencies
with `uv sync --project hub` and use that environment to include hub tests.

### Test tiers and the parallel runner

`python3 scripts/test.py` runs every `tests/test_*.py` module in its own process, up to 8 at a time, and exits
non-zero if any module fails. It runs the **default tier**: fast tests that fake the runner, the decision model and
the clock. `python3 scripts/test.py --tier integration` (equivalently `DROVER_TESTS=integration`) also runs the
**integration tier**, the tests that start the real `drover.runner`, real child processes, real timeouts or a real
gate script; this is the pre-landing run and what `.drover/scripts/test.ps1` executes. Mark such a test with
`@integration` from `tests/tiers.py`; a test is moved to that tier, never deleted. On Windows run the script with
the hub venv's `python.exe` so the hub tests are included. Plain `python -m unittest discover -s tests -t .` still
works and runs the default tier. Tests mock the decision model and use no real sleeps; a test that takes over a
second states why in a comment.

Account allowance, routing and the Stats tab: [usage and statistics](docs/USAGE-STATS.md).
