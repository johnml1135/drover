"""Repo config: drover/repos/<name>.toml says where a repo's repo lives and how it is gated and landed.

    python -m drover.repos [NAME] [--json]

Repos live in <state dir>/repos/*.toml (config `repos_dir`); the shipped repos/ are examples, read only
when DROVER_EXAMPLES=1 and no private directory exists. A legacy programs/ directory is read with a warning.
Keys: repo, worktrees (Windows paths; default <paths.worktrees>\\<name>.worktrees, else <repo>.worktrees), default_branch,
remote; [run] platforms (a list of windows|wsl, default both),
default (the platform used when nothing else decides); [gate] platform (windows|wsl|both: both must pass on each),
unknown, scope_map,
timeout_s, idle_s and [gate.commands] full/scoped (argv lists, `{project}` filled per selected test project);
[review] backend, seat, model, effort, high_risk_model; [land] policy (batch|single), push (default policy.push, false: land on local main only).
`gate.unknown = true` means nobody knows the gate command: the repo's tasks go to the owner with a question.
"""
import json
import logging
import os
import sys
import tomllib
from pathlib import Path

from . import config

REPOS_DIR = Path(__file__).resolve().parents[1] / "repos"   # the shipped examples
PACKAGE_DIR = Path(__file__).resolve().parent
PLATFORMS = ("windows", "wsl")
GATE_PLATFORMS = PLATFORMS + ("both",)
DEFAULTS = {"default_branch": "main", "remote": "origin"}


class RepoError(ValueError):
    pass


def _argv(v, what):
    if not isinstance(v, list) or not v or not all(isinstance(x, str) and x for x in v):
        raise RepoError(f"{what} must be a non-empty list of strings")
    return list(v)


def normalize(raw, name=None):
    """A validated repo dict with every default filled in."""
    name = str(raw.get("name") or name or "")
    if not name:
        raise RepoError("a repo needs a name")
    if not raw.get("repo"):
        raise RepoError(f"{name}: repo is required")
    raw = {**raw, "repo": os.path.expanduser(str(raw["repo"]))}
    raw["worktrees"] = os.path.expanduser(str(raw.get("worktrees") or worktrees_for(name, raw["repo"])))
    g = dict(raw.get("gate") or {})
    platform = str(g.get("platform") or "windows").lower()
    if platform not in GATE_PLATFORMS:
        raise RepoError(f"{name}: gate.platform must be one of {GATE_PLATFORMS}")
    run = dict(raw.get("run") or {})
    plats = run.get("platforms", list(PLATFORMS))
    if not isinstance(plats, list) or not plats or not all(p in PLATFORMS for p in plats):
        raise RepoError(f"{name}: run.platforms must be a non-empty list drawn from {PLATFORMS}")
    plats = [p for p in PLATFORMS if p in plats]
    default = run.get("default") or ("windows" if "windows" in plats else plats[0])
    if default not in plats:
        raise RepoError(f"{name}: run.default {default!r} is not in run.platforms {plats}")
    gate_plats = list(PLATFORMS) if platform == "both" else [platform]
    if any(p not in plats for p in gate_plats):
        raise RepoError(f"{name}: gate.platform {platform!r} needs run.platforms to include {gate_plats}")
    cmds = {k: _argv(v, f"{name}: gate.commands.{k}") for k, v in (g.get("commands") or {}).items()}
    unknown = bool(g.get("unknown", not cmds))
    if not unknown and "full" not in cmds:
        raise RepoError(f"{name}: a known gate needs gate.commands.full")
    land = dict(raw.get("land") or {})
    policy = str(land.get("policy") or "batch")
    if policy not in ("batch", "single"):
        raise RepoError(f"{name}: land.policy must be batch or single")
    rev = dict(raw.get("review") or {})
    models = config.get()["models"]
    seat = config.seat_by_name(rev["seat"]) if rev.get("seat") else None
    if rev.get("seat") and seat is None:
        raise RepoError(f"{name}: unknown review.seat {rev['seat']!r}")
    model = rev.get("model") or models["review"]
    backend = rev.get("backend") or (seat["backend"] if seat else next(
        (b for b, ladder in models["ladders"].items() if model in ladder), "claude"))
    if backend not in config.BACKENDS:
        raise RepoError(f"{name}: review.backend must be one of {config.BACKENDS}")
    if seat and seat["backend"] != backend:
        raise RepoError(f"{name}: review.seat does not match review.backend")
    if not rev.get("model") and model not in models["ladders"][backend]:
        model = models["ladders"][backend][-1]
    high_model = rev.get("high_risk_model") or (models["director"] if backend == "claude" else model)
    if not all(isinstance(v, str) and v.strip() for v in (model, high_model)):
        raise RepoError(f"{name}: review models must be non-empty strings")
    if rev.get("effort") is not None and (not isinstance(rev["effort"], str) or not rev["effort"].strip()):
        raise RepoError(f"{name}: review.effort must be a non-empty string")
    return {"name": name, "repo": str(raw["repo"]), "worktrees": str(raw["worktrees"]),
            "default_branch": str(raw.get("default_branch") or DEFAULTS["default_branch"]),
            "remote": str(raw.get("remote") or DEFAULTS["remote"]),
            "run": {"platforms": plats, "default": default},
            "consumes": list(raw.get("consumes") or []),
            "gate": {"platform": platform, "unknown": unknown, "scope_map": g.get("scope_map"),
                     "timeout_s": float(g.get("timeout_s") or 3600), "idle_s": float(g.get("idle_s") or 1200),
                     "project_pattern": str(g.get("project_pattern") or ""),
                     "commands": cmds, "hint": g.get("hint") or ""},
            "review": {"backend": backend, "seat": rev.get("seat"), "model": model,
                       "effort": rev.get("effort"), "high_risk_model": high_model},
            "land": {"policy": policy, "push": bool(land.get("push", config.get()["policy"]["push"]))}}


def worktrees_for(name, repo):
    """Where a repo's worktrees go when it does not say: <paths.worktrees>\\<name>.worktrees for a shared root,
    else <repo>.worktrees next to the repo."""
    shared = config.get()["paths"]["worktrees"].rstrip("\\/")
    if shared:
        return shared + ("\\" if "\\" in shared else "/") + f"{name}.worktrees"
    repo = str(repo).rstrip("\\/")
    return repo + ".worktrees"


def repos_dir():
    """Private configs first; legacy directories are read-only and examples require opt-in."""
    mine = config.repos_dir()
    if mine.is_dir():
        return mine
    legacy = mine.with_name("programs")
    if legacy.is_dir():
        logging.getLogger(__name__).warning("programs/ is deprecated; move repo configs to repos/")
        return legacy
    return REPOS_DIR if os.environ.get("DROVER_EXAMPLES") == "1" else mine


def gate_platforms(prog):
    """The platforms a repo's gate runs on, in order: [windows, wsl] for `both`."""
    plat = prog["gate"]["platform"]
    return list(PLATFORMS) if plat == "both" else [plat]


def load_all(directory=None):
    """{lowercase name: repo} for every *.toml in the repos directory."""
    out = {}
    for path in sorted(Path(directory or repos_dir()).glob("*.toml")):
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        prog = normalize(raw, path.stem)
        out[prog["name"].lower()] = prog
    return out


def split_command(text):
    """argv from a command line: whitespace separates, single or double quotes group and are removed, nothing else is
    interpreted (a backslash is a path character, there is no shell)."""
    out, cur, quote, have = [], [], None, False
    for ch in str(text):
        if quote:
            if ch == quote:
                quote = None
            else:
                cur.append(ch)
        elif ch in "\"'":
            quote, have = ch, True
        elif ch.isspace():
            if cur or have:
                out.append("".join(cur))
            cur, have = [], False
        else:
            cur.append(ch)
    if quote:
        raise RepoError("unbalanced quote in the gate command")
    if cur or have:
        out.append("".join(cur))
    return out


def gate_argv(text):
    """The argv that runs an owner-given command line: a .ps1 script cannot be started directly, so it runs under
    `powershell.exe -NoProfile -ExecutionPolicy Bypass -File <script> <args>`."""
    argv = split_command(text)
    if not argv or not all(argv):
        raise RepoError("the gate command is empty")
    if argv[0].lower().endswith(".ps1"):
        argv = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", *argv]
    return argv


def with_override(prog, command_line):
    """The repo with its gate replaced by one owner-given command line (the answer to the unknown-gate question)."""
    try:
        argv = gate_argv(command_line)
    except RepoError:
        return prog
    g = {**prog["gate"], "unknown": False, "commands": {"full": argv}, "scope_map": None}
    return {**prog, "gate": g}


native = config.native
WIN_RE, MNT_RE = config.WIN_RE, config.MNT_RE


def scope_map_path(prog):
    name = prog["gate"].get("scope_map")
    if not name:
        return None
    p = Path(native(name)).expanduser()
    return p if p.is_absolute() else Path(native(prog["repo"])).expanduser() / p


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="drover.repos")
    ap.add_argument("name", nargs="?")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    progs = load_all()
    if a.name:
        progs = {k: v for k, v in progs.items() if k == a.name.lower()}
        if not progs:
            print(f"no repo {a.name!r}", file=sys.stderr)
            return 2
    if a.json:
        print(json.dumps(progs, indent=1))
    else:
        for p in progs.values():
            print(f"{p['name']}: {p['repo']} gate {'UNKNOWN' if p['gate']['unknown'] else p['gate']['platform']}"
                  f" land {'push' if p['land']['push'] else 'local'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
