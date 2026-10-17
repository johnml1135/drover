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
Python 3.12+ and the CLI installed inside the distro. Install Ollama and suitable local models to enable model
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

Re-run `python -m drover init` and select a discovered repo, its supported platforms, gate command and push policy.
Alternatively, copy [acme-lib.toml](repos/acme-lib.toml) into `<state>/repos/`, then edit its name and path and supply
the command that really tests your repo. [acme-app.toml](repos/acme-app.toml) shows a consumer's dependency declaration.
Inspect private repo configuration with `python -m drover.repos --json`.

Packaged examples are loaded only with `DROVER_EXAMPLES=1`, when no private `repos/` directory exists. A legacy
`programs/` directory is read with a deprecation warning; move its files into `repos/` when convenient.

### Approve a task

Import a proposed backlog through the hub API (`POST /api/v1/import`) or create a task through `POST /api/v1/tasks`
with its `repo`, goal, acceptance criteria and file scope. API clients use the configured authentication.
In the hub's Inbox, inspect the proposed task and choose Approve. Enable dispatch when ready.
The Plan and Runs views show progress; owner questions appear in the Inbox.

Every coding task has one home repo. Cross-repo work uses paired tasks, with the provider landing first and the
consumer testing again against the landed provider. Research tasks produce reports. See [ADR 0003](docs/adr/0003-task-writes-one-repo.md).

## Reference

- [Current design](docs/design.md), [vocabulary](GLOSSARY.md), [dispatcher details](docs/dispatcher.md)
- [Configuration reference](drover.example.toml), [architecture decisions](docs/adr/)
- [Licence note](LICENSE-NOTE.md)

`drover/` uses the Python standard library. The hub uses FastAPI, Jinja2 and Uvicorn.
Run checks from the checkout root:

```sh
python3 -m unittest discover -s tests -t .
uv run --project hub python -m unittest discover -s tests -t .
```
