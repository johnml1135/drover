"""Load a repo's .drover/config.toml at a commit, then apply machine overrides.

Only this module reads repo configuration. Actions are relative PowerShell script paths;
all platforms invoke them with the same argv and the checkout as cwd.
"""
import json
import logging
import math
import os
import sys
import tomllib
from pathlib import Path

from . import config, placement
from .placement import native
from .repos_normalize import (DEFAULTS, DOCS_ONLY_GLOBS, GATE_PLATFORMS, PLATFORMS, RepoError,
                              _ResolvedRepo, cleanup_config, machine_config, normalize, worktrees_for)

REPOS_DIR = Path(__file__).resolve().parents[1] / "repos"   # the shipped examples
PACKAGE_DIR = Path(__file__).resolve().parent


def repo_name(prog):
    return prog["name"]


def repo_path(prog):
    """The configured checkout path, before placement translates it."""
    return prog["repo"]


def worktree_root(prog, where="windows"):
    """Where a repo's worktrees go for a placement. Windows keeps the configured root (`worktrees`); WSL gets
    `<paths.wsl_worktrees>/<name>.worktrees` on the distro's own filesystem (ADR 0007)."""
    if where == "wsl":
        return placement.wsl_worktrees_root().rstrip("/") + f"/{prog['name']}.worktrees"
    return prog["worktrees"]


def default_branch(prog):
    return prog["default_branch"]


def remote_name(prog):
    return prog.get("remote", DEFAULTS["remote"])


def review_config(prog):
    """The resolved review policy, independent of worker routing."""
    return dict(prog["review"])


def brief_text(prog):
    return prog.get("brief", "")


def landing_config(prog):
    """Landing behavior with the legacy policy resolved by normalize."""
    return {**prog["landing"], "policy": prog["land"]["policy"]}


def machine_defaults(raw, repo=""):
    """Saved setup defaults; raw repo registrations may omit runtime defaults."""
    raw = raw or {}
    return {"repo": raw.get("repo", repo),
            "push": (raw.get("landing") or {}).get("push", (raw.get("land") or {}).get("push", False))}


def machine_overrides(name, repo, trees, *, base=None, push=None, adopted=False):
    """Serialize registration facts after adoption, preserving complete policy during fallback."""
    base = base or {}
    canonical = base if "_machine" in base else normalize({**base, "name": name, "repo": repo})
    saved = base.get("_machine", base)
    if adopted and push is not None:
        raise RepoError(f"{name}: push belongs in .drover/config.toml after adoption")
    raw = {"name": name, "repo": repo, "worktrees": saved.get("worktrees", trees),
           "default_branch": canonical["default_branch"], "remote": canonical["remote"]}
    if "budget_gb" in saved:
        raw["budget_gb"] = saved["budget_gb"]
    if adopted:
        if "env" in saved.get("run", {}):
            raw["run"] = {"env": dict(saved["run"]["env"])}
    else:
        for key in ("scripts", "gate", "run", "brief", "review", "cleanup"):
            if key in saved:
                raw[key] = saved[key]
        if "policy" in saved.get("land", {}):
            raw["land"] = {"policy": saved["land"]["policy"]}
        raw["landing"] = {"push": canonical["landing"]["push"] if push is None else push}
        if "mode" in saved.get("landing", {}):
            raw["landing"]["mode"] = saved["landing"]["mode"]
    normalize(raw)
    return raw


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


def gate_order(platforms):
    """WSL first: arbitrary repo scripts share a checkout and cannot safely overlap."""
    return sorted(platforms, key=lambda platform: platform != 'wsl')


def ci_policy(prog):
    """Validated CI planning and runner limits, owned by the repo loader."""
    return {**prog["gate"], "base": prog["default_branch"]}


def _registration_inventory(directory=None):
    """{lowercase name: (raw registration, path)} without resolving effective policy."""
    out = {}
    for path in sorted(Path(directory or repos_dir()).glob("*.toml")):
        raw = read_machine(path)
        out[str(raw.get("name") or path.stem).lower()] = (raw, path)
    return out


def registered_names(directory=None):
    """Registered repo names, without loading effective policies or caching results."""
    return set(_registration_inventory(directory))


def load_named(name, directory=None):
    """Load one registered repo's effective policy, or return None when it is unknown."""
    registration = _registration_inventory(directory).get(str(name).lower())
    if registration is None:
        return None
    raw, path = registration
    return load({**raw, "name": raw.get("name") or path.stem}, machine_path=path)


def load_all(directory=None):
    """{lowercase name: repo} for every *.toml in the repos directory."""
    out = {}
    for path in sorted(Path(directory or repos_dir()).glob("*.toml")):
        raw = read_machine(path)
        prog = load({**raw, "name": raw.get("name") or path.stem}, machine_path=path)
        out[prog["name"].lower()] = prog
    return out


def parse_committed_config(text, commit):
    """Parse strict repository policy without I/O; commit labels error provenance.

    Remaining validation stays in normalize, in its original order, along with
    machine- and user-dependent defaults.
    """
    entry = ".drover/config.toml"
    try:
        versioned = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise RepoError(f"{entry}@{commit}: <syntax>: {e}") from e
    allowed = {
        "run": {"platforms", "default", "env", "sandbox_writable_roots"},
        "gate": {"platform", "timeout_s", "idle_s", "docs_only", "docs_only_globs"},
        "review": {"backend", "seat", "model", "effort", "high_risk_model"},
        "landing": {"mode", "push"}, "land": {"policy", "push"}, "scripts": None,
        "cleanup": {"disposable"},
    }
    def invalid(key, detail):
        raise RepoError(f"{entry}@{commit}: {key}: {detail}")
    if type(versioned.get("schema_version")) is not int or versioned["schema_version"] != 1:
        invalid("schema_version", "must be integer 1")
    for key, value in versioned.items():
        if key in ("schema_version", "brief"):
            continue
        if key not in allowed:
            invalid(key, "not a repository key")
        if not isinstance(value, dict):
            invalid(key, "must be a table")
        for subkey in value:
            if allowed[key] is not None and subkey not in allowed[key]:
                invalid(f"{key}.{subkey}", "not a repository key")
    # Committed policy is strict; legacy machine fallback intentionally is not.
    strings = {"brief", "run.default", "gate.platform", "review.backend", "review.seat",
               "review.model", "review.effort", "review.high_risk_model", "landing.mode", "land.policy"}
    enums = {"run.default": PLATFORMS, "gate.platform": GATE_PLATFORMS,
             "review.backend": config.BACKENDS, "landing.mode": ("local",),
             "land.policy": ("batch", "single")}
    for table, fields in versioned.items():
        fields = fields.items() if isinstance(fields, dict) else [(None, fields)]
        for field, value in fields:
            key = f"{table}.{field}" if field is not None else table
            if key in strings and (not isinstance(value, str) or (key != "brief" and not value.strip())):
                invalid(key, "must be a non-empty string" if key != "brief" else "must be a string")
            if key in enums and value not in enums[key]:
                invalid(key, f"must be one of {enums[key]}")
            if key in ("gate.timeout_s", "gate.idle_s") and type(value) not in (int, float):
                invalid(key, "must be a number (not a boolean)")
            if key in ("gate.docs_only", "landing.push", "land.push") and type(value) is not bool:
                invalid(key, "must be a boolean")
    for action, path in versioned.get("scripts", {}).items():
        if not isinstance(path, str) or not path.startswith(".drover/scripts/") or \
                "\\" in path or ":" in path or not path.endswith(".ps1") or \
                any(part in ("", ".", "..") for part in path.split("/")):
            invalid(f"scripts.{action}", "must be a canonical .drover/scripts/*.ps1 path")
    return versioned


def load(machine, revision="HEAD", checkout=None, *, machine_path=None, sources=None):
    """Read versioned config at revision, including on reused task worktrees; never read a dirty file.

    A missing .drover/config.toml contributes an empty layer, so migration from repo registrations uses
    the same merge. An inaccessible machine checkout leaves its machine layer usable for
    startup, but an exact revision requires a reachable checkout.
    """
    trusted = isinstance(machine, _ResolvedRepo)
    answer = machine.get("_test_script") if trusted else None
    machine_path = Path(machine_path or (machine.get("_machine_path") if trusted else None)
                        or repos_dir() / f"{machine.get('name', 'repo')}.toml").resolve()
    if trusted:
        machine = machine["_machine"]
    private = [key for key in machine if key.startswith("_")]
    if private:
        raise RepoError(f"{machine_path}: {private[0]} is internal metadata")
    if not machine.get("repo"):
        raise RepoError(f"{machine.get('name', 'repo')}: repo is required")
    repo = checkout or machine["repo"]
    versioned = {}
    if placement.reachable(repo):
        result = placement.git(repo, "rev-parse", "--verify", revision + "^{commit}", timeout=30)
        if result.returncode:
            raise RepoError(f"cannot resolve repo config commit {revision}: {result.stderr.strip()}")
        commit = result.stdout.strip()
        entry = ".drover/config.toml"
        listing = placement.git(repo, "ls-tree", commit, "--", entry, timeout=30)
        if listing.returncode:
            raise RepoError(f"cannot inspect {entry} at {commit}: {listing.stderr.strip()}")
        if listing.stdout.strip():
            result = placement.git(repo, "show", f"{commit}:{entry}", timeout=30)
            if result.returncode:
                raise RepoError(f"cannot read {entry} at {commit}: {result.stderr.strip()}")
            versioned = parse_committed_config(result.stdout, commit)
            try:
                normalize(config._merge(versioned, {"name": machine.get("name"), "repo": machine["repo"]}))
            except (RepoError, TypeError, ValueError, AttributeError) as e:
                raise RepoError(f"{entry}@{commit}: {e}") from e
    elif revision != "HEAD":
        raise RepoError(f"cannot read repo config commit {revision}: checkout {repo} is unreachable")
    if versioned:
        for key, value in machine.items():
            if key in ("name", "repo", "worktrees", "remote", "default_branch", "budget_gb"):
                continue
            if key == "run" and isinstance(value, dict):
                forbidden = [f"run.{k}" for k in value if k != "env"]
                if not forbidden:
                    continue
            else:
                forbidden = [f"{key}.{k}" for k in value] if isinstance(value, dict) and value else [key]
            raise RepoError(f"{machine_path}: {', '.join(forbidden)} is not a machine fact; run "
                            f"python -m drover.config migrate-registration {machine.get('name', machine_path.stem)}")
    merged = config._merge(versioned, machine)
    if answer is not None and "test" not in versioned.get("scripts", {}):
        merged = config._merge(merged, {"scripts": {"test": answer}})
    origins = None
    if sources is not None:
        origins = config._source_leaves(versioned, f"repo:.drover/config.toml@{commit}") if versioned else {}
        origins.update(config._source_leaves(machine, "machine:" + str(machine_path)))
        if answer is not None and "test" not in versioned.get("scripts", {}):
            origins["scripts.test"] = "answer:test_script"
    out = _ResolvedRepo(normalize(merged, sources=origins))
    out["_adopted"] = bool(versioned)
    out["_committed_test"] = "test" in versioned.get("scripts", {})
    if versioned:
        out["schema_version"] = 1
        if origins is not None:
            origins["schema_version"] = f"repo:.drover/config.toml@{commit}"
    out["_machine"] = dict(machine)
    out["_machine_path"] = str(machine_path)
    if answer is not None:
        out["_test_script"] = answer
    if sources is not None:
        sources.clear()
        sources.update(origins)
    return out


def script_argv(prog, action="test", platform=None):
    """The complete action command. No platform variants, argument templates or inferred tools."""
    path = prog["scripts"].get(action)
    if path is None:
        raise RepoError(f"{prog['name']}: no scripts.{action} in .drover/config.toml")
    return [placement.executable("pwsh", platform), "-NoLogo", "-NoProfile", "-NonInteractive", "-File", path]


def run_config(prog):
    """Execution settings, also usable before a task has a home repo."""
    return {"platforms": list(PLATFORMS), "default": None, "env": {}, "sandbox_writable_roots": [],
            **((prog or {}).get("run") or {})}


def has_script(prog, action):
    return action in prog["scripts"]


def with_test_script(prog, path):
    """Apply an owner-provided script to the effective config and future reloads."""
    normalize({**prog, "scripts": {"test": path}})
    if not isinstance(prog, _ResolvedRepo):
        prog = load(prog)
    committed = prog.get("_committed_test", False)
    return _ResolvedRepo({**prog, "_test_script": path,
            "scripts": dict(prog["scripts"]) if committed else {**prog["scripts"], "test": path},
            "gate": {**prog["gate"], "unknown": False}})


def read_machine(path):
    """Raw machine overrides, for the setup editor as well as runtime loading."""
    with open(path, "rb") as stream:
        raw = tomllib.load(stream)
    for key in raw:
        if key.startswith("_"):
            raise RepoError(f"{Path(path).resolve()}: {key} is internal metadata")
    return raw


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
