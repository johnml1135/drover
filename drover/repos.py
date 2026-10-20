"""Load a repo's drover.toml at a commit, then apply machine overrides.

Only this module reads repo configuration. Actions are relative PowerShell script paths;
all platforms invoke them with the same argv and the checkout as cwd.
"""
import json
import logging
import os
import sys
import tomllib
from pathlib import Path

from . import config, placement
from .placement import native

REPOS_DIR = Path(__file__).resolve().parents[1] / "repos"   # the shipped examples
PACKAGE_DIR = Path(__file__).resolve().parent
PLATFORMS = ("windows", "wsl")
GATE_PLATFORMS = PLATFORMS + ("both",)
DEFAULTS = {"default_branch": "main", "remote": "origin"}


class RepoError(ValueError):
    pass


DOCS_ONLY_GLOBS = ("docs/**", "*.md", "GLOSSARY.md", "LICENSE", "!.github/**")


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
    platform = str(g.get("platform") or ((raw.get("run") or {}).get("platforms") or ["windows"])[0]).lower()
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
    scripts = raw.get("scripts", {})
    if not isinstance(scripts, dict):
        raise RepoError(f"{name}: scripts must be a table of action names and relative script paths")
    for action, path in scripts.items():
        if not isinstance(path, str) or not path or "\\" in path or path.startswith("/") or ":" in path \
                or ".." in Path(path).parts or not path.endswith(".ps1"):
            raise RepoError(f"{name}: scripts.{action} must be a relative .ps1 path inside the repo")
    unknown = "test" not in scripts
    env = run.get("env", {})
    if not isinstance(env, dict) or not all(isinstance(k, str) and k and "=" not in k and isinstance(v, str)
                                          for k, v in env.items()):
        raise RepoError(f"{name}: run.env must map environment variable names to strings")
    roots = run.get("sandbox_writable_roots", [])
    if not isinstance(roots, list) or not all(isinstance(p, str) and p.startswith("/tmp/")
                                            and ".." not in Path(p).parts for p in roots):
        raise RepoError(f"{name}: run.sandbox_writable_roots must be paths under /tmp")
    brief = raw.get("brief", "")
    if not isinstance(brief, str):
        raise RepoError(f"{name}: brief must be a string")
    docs_only = g.get("docs_only", True)
    docs_globs = g.get("docs_only_globs", list(DOCS_ONLY_GLOBS))
    if not isinstance(docs_only, bool):
        raise RepoError(f"{name}: gate.docs_only must be a boolean")
    if not isinstance(docs_globs, list) or not all(isinstance(x, str) and x.strip() for x in docs_globs):
        raise RepoError(f"{name}: gate.docs_only_globs must be a list of non-empty globs")
    land = dict(raw.get("land") or {})
    policy = str(land.get("policy") or "batch")
    if policy not in ("batch", "single"):
        raise RepoError(f"{name}: land.policy must be batch or single")
    landing = dict(raw.get("landing") or {})
    mode = landing.get("mode", "local")
    if mode == "pr":
        raise RepoError(f"{name}: landing.mode = pr is reserved, not implemented")
    if mode != "local":
        raise RepoError(f"{name}: landing.mode must be local (pr is not implemented)")
    push = landing.get("push", land.get("push", False))
    if not isinstance(push, bool):
        raise RepoError(f"{name}: landing.push must be a boolean")
    remote = landing.get("remote", raw.get("remote", DEFAULTS["remote"]))
    main = landing.get("main", raw.get("default_branch", DEFAULTS["default_branch"]))
    for key, value in (("remote", remote), ("main", main)):
        if not isinstance(value, str) or not value.strip() or value.startswith("-") or any(c.isspace() for c in value):
            raise RepoError(f"{name}: landing.{key} must be a non-empty git name")
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
            "default_branch": main,
            "remote": remote,
            "run": {"platforms": plats, "default": default, "env": dict(env), "sandbox_writable_roots": list(roots)},
            "scripts": dict(scripts), "brief": brief,
            "consumes": list(raw.get("consumes") or []),
            "gate": {"platform": platform, "unknown": unknown,
                     "timeout_s": float(g.get("timeout_s") or 3600), "idle_s": float(g.get("idle_s") or 1200),
                     "docs_only": docs_only, "docs_only_globs": docs_globs,
                     "hint": "Add [scripts] test to drover.toml"},
            "review": {"backend": backend, "seat": rev.get("seat"), "model": model,
                       "effort": rev.get("effort"), "high_risk_model": high_model},
            "landing": {"mode": mode, "push": push, "remote": remote, "main": main},
            "land": {"policy": policy, "push": push}}


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
        raw = read_machine(path)
        prog = load({**raw, "name": raw.get("name") or path.stem})
        out[prog["name"].lower()] = prog
    return out


def load(machine, revision="HEAD", checkout=None):
    """Read versioned config at revision, including on reused task worktrees; never read a dirty file.

    A missing drover.toml contributes an empty layer, so migration from machine files uses
    the same merge. An inaccessible machine checkout also leaves its machine layer usable.
    """
    machine = machine.get("_machine", machine)
    if not machine.get("repo"):
        raise RepoError(f"{machine.get('name', 'repo')}: repo is required")
    repo = checkout or machine["repo"]
    versioned = {}
    if placement.reachable(repo):
        result = placement.git(repo, "rev-parse", "--verify", revision + "^{commit}", timeout=30)
        if result.returncode:
            raise RepoError(f"cannot resolve repo config commit {revision}: {result.stderr.strip()}")
        commit = result.stdout.strip()
        listing = placement.git(repo, "ls-tree", commit, "--", "drover.toml", timeout=30)
        if listing.returncode:
            raise RepoError(f"cannot inspect drover.toml at {commit}: {listing.stderr.strip()}")
        if listing.stdout.strip():
            result = placement.git(repo, "show", f"{commit}:drover.toml", timeout=30)
            if result.returncode:
                raise RepoError(f"cannot read drover.toml at {commit}: {result.stderr.strip()}")
            try:
                versioned = tomllib.loads(result.stdout)
            except tomllib.TOMLDecodeError as e:
                raise RepoError(f"invalid drover.toml at {commit}: {e}") from e
    merged = config._merge(versioned, machine)
    out = normalize(merged)
    out["_machine"] = dict(machine)
    return out


def script_argv(prog, action="test"):
    """The complete action command. No platform variants, argument templates or inferred tools."""
    path = prog["scripts"].get(action)
    if path is None:
        raise RepoError(f"{prog['name']}: no scripts.{action} in drover.toml")
    return ["pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-File", path]


def run_config(prog):
    """Execution settings, also usable before a task has a home repo."""
    return {"platforms": list(PLATFORMS), "default": None, "env": {}, "sandbox_writable_roots": [],
            **((prog or {}).get("run") or {})}


def has_script(prog, action):
    return action in prog["scripts"]


def with_test_script(prog, path):
    """Apply an owner-provided script to the effective config and future reloads."""
    return {**prog, "scripts": {**prog["scripts"], "test": path},
            "gate": {**prog["gate"], "unknown": False},
            "_machine": config._merge(prog.get("_machine", {}), {"scripts": {"test": path}})}


def read_machine(path):
    """Raw machine overrides, for the setup editor as well as runtime loading."""
    with open(path, "rb") as stream:
        return tomllib.load(stream)


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
