"""Repository answers and configuration file writing for the setup wizard."""
import os
import re
import shutil
import time
import tomllib
from pathlib import Path

from . import config, repos
from .wizard_asking import WizardError, to_bool


def example_for(name):
    """The shipped example repo of that name (case-insensitive) as a dict, or None."""
    for path in sorted(repos.REPOS_DIR.glob("*.toml")):
        if path.stem.lower() == str(name).lower():
            try:
                return repos.read_machine(path)
            except (OSError, tomllib.TOMLDecodeError):
                return None
    return None


def repo_toml(p, worktrees_root):
    """Write machine settings, preserving saved repo behavior as fallback and overrides."""
    base = p.get("base") or {}
    repo = p["repo"]
    trees = (worktrees_root.rstrip("\\/") + ("\\" if "\\" in worktrees_root else "/") + p["name"] + ".worktrees"
             if worktrees_root else repo.rstrip("\\/") + ".worktrees")
    resolved = p.get("resolved")
    if resolved is None:
        resolved = _resolve_repo(p["name"], repo, base, p.get("state", config.state_dir()))
    raw = repos.machine_overrides(p["name"], repo, trees, base=resolved, push=p.get("push"),
                                  adopted=resolved["_adopted"])
    return config.dumps(raw)


def _saved_repo(state, name):
    state = Path(config.native(state))
    path = _inside(state, state / "repos" / f"{name}.toml")
    try:
        return repos.read_machine(path)
    except FileNotFoundError:
        return None


def _resolve_repo(name, repo, base, state):
    """Resolve the selected checkout using the actual saved repo registration name."""
    path = _inside(state, Path(config.native(state)) / "repos" / f"{name}.toml")
    return repos.load({**(base or {}), "name": str(name), "repo": str(repo)}, machine_path=path)


def q_repos(a, d, found_repos, worktrees, state):
    a.say("\n9. Repos: machine paths and landing policy; repo actions live in .drover/config.toml.")
    if "repos" in a.answers:
        out = [_repo_answer(x, state) for x in a.answers["repos"]]
        for p in out:
            repo_toml(p, worktrees)
        _examples_note(a, out)
        return out
    if not found_repos:
        a.say("   no repos found under the repos root; add repos later by re-running init or copying repos/*.toml")
        return []
    for i, r in enumerate(found_repos, 1):
        a.say(f"   {i}. {r['name']}  {r['path']}")
    pick = a.ask("repos_pick", "Repos to set up (numbers, comma separated; all; none)", "none")
    chosen = found_repos if pick.lower() == "all" else [] if pick.lower() in ("none", "") else \
        [found_repos[int(x) - 1] for x in re.findall(r"\d+", pick) if 0 < int(x) <= len(found_repos)]
    out = []
    for r in chosen:
        k = r["name"]
        name = a.ask(f"repo_{k}_name", f"{k}: repo name", k)
        existing = _saved_repo(state, name)
        base = existing if existing is not None else example_for(name)
        repo = a.ask(f"repo_{k}_repo", f"{name}: repo path", repos.machine_defaults(existing, r["path"])["repo"])
        a.say(f"   {name}: actions and run platforms come from the repo's versioned .drover/config.toml")
        resolved = _resolve_repo(name, repo, base, state)
        push = None
        if resolved["_adopted"]:
            if f"repo_{k}_push" in a.answers:
                raise WizardError(f"{name}: push belongs in .drover/config.toml after adoption")
        else:
            push = a.ask(f"repo_{k}_push", f"{name}: push landed work to the remote?",
                         bool(repos.machine_defaults(base)["push"]), to_bool)
        p = {"name": name, "repo": repo, "push": push, "base": base, "resolved": resolved,
             "existing": existing is not None}
        repo_toml(p, worktrees)
        out.append(p)
    _examples_note(a, out)
    return out


def _examples_note(a, chosen):
    if chosen:
        a.say("   Note: private repo configs live in the state repos/ folder. Packaged examples require DROVER_EXAMPLES=1. "
              "Copy an example there, or re-run init, to add repos.")


def _repo_answer(x, state):
    """Merge answers with the saved repo, using examples only for new repos."""
    existing = _saved_repo(state, x["name"])
    base = existing
    if base is None and x.get("from_example", True):
        base = example_for(x["name"])
    name = str(x["name"])
    repo = str(x.get("repo", repos.machine_defaults(base)["repo"]))
    resolved = _resolve_repo(name, repo, base, state)
    return {"name": name, "repo": repo, "push": x["push"] if "push" in x else None,
            "base": base, "resolved": resolved, "existing": existing is not None}



# ---- assembling and writing -----------------------------------------------------------------------------------

HEADER = """# Drover configuration, written by `python -m drover init`. Every key is documented in drover.example.toml.
# Keys left out take the built-in defaults. Re-run `python -m drover init` to change it (the old file is backed up).
"""


def _merge_named(existing, updates):
    merged = {x.get("name", x.get("kind")): dict(x) for x in existing}
    for x in updates:
        name = x.get("name", x.get("kind"))
        merged[name] = config._merge(merged.get(name, {}), x)
    return list(merged.values())


def build_config(s, existing=None):
    """The raw config dict for the answers `s`; validated with config.validate before it is returned."""
    existing = existing or {}
    seats = []
    for seat in s["seats"]:
        out = {k: seat[k] for k in ("name", "backend", "priority", "max_workers", "reserve_pct", "director", "placement",
                                    "binary")}
        out["director_reserve_pct"] = seat["director_reserve_pct"]
        if seat["config_dir"]:
            out["config_dir"] = seat["config_dir"]
        if seat.get("codex_home"):
            out["codex_home"] = seat["codex_home"]
        seats.append(out)
    raw = {"paths": {"state_dir": s["state_dir"], "repos_root": s["repos_root"], "worktrees": s["worktrees"],
                     "wsl_distro": s["wsl_distro"], "wsl_user": s["wsl_user"]},
           "seats": seats, "trackers": s["trackers"],
           "decider": {"models": {"primary": s["primary"], "shadow": s["shadow"]}, "device": s["device"],
                       "ollama_models": s["ollama_models"], "commercial_use": s["commercial_use"]},
           "config_sync": {"remote": s["private_remote"], "auto_push": True},
           "hub": {"ui_port": s["ui_port"], "api_port": s["api_port"], "tailscale": s["tailscale"],
                   "allowed_logins": ([s["allowed_login"]] if s["allowed_login"] else [])
                                     + existing.get("hub", {}).get("allowed_logins", [])[1:],
                   "auth": {"mode": "tailscale"}}}
    raw = config._merge(existing, raw)
    config.validate(raw)
    return raw


def _read_existing(state_dir):
    try:
        with open(Path(config.native(state_dir)) / "config.toml", "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _inside(state, path):
    state, path = Path(state).resolve(), Path(path).resolve()
    if state != path and state not in path.parents:
        raise WizardError(f"refusing to write {path}: outside the state dir {state}")
    return path


def write_file(state, path, text, now):
    """Write text to path (inside the state dir only); an existing file is first copied to <name>.bak-<stamp>."""
    path = _inside(state, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S', time.localtime(now()))}")
        n = 1
        while backup.exists():
            backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S', time.localtime(now()))}-{n}")
            n += 1
        shutil.copy2(path, backup)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    return backup
