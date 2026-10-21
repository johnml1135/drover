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

REPOS_DIR = Path(__file__).resolve().parents[1] / "repos"   # the shipped examples
PACKAGE_DIR = Path(__file__).resolve().parent
PLATFORMS = ("windows", "wsl")
GATE_PLATFORMS = PLATFORMS + ("both",)
DEFAULTS = {"default_branch": "main", "remote": "origin"}


class RepoError(ValueError):
    pass


class _ResolvedRepo(dict):
    """In-process loader result; serialized dictionaries cannot confer this trust."""


def machine_config(prog):
    """Serializable machine facts, with normalized locations and branch defaults.

    Loader metadata is never a machine-file key. Keep legacy fallback policy from
    the original machine layer, so a worker can reload the commit on its own side.
    """
    if not isinstance(prog, _ResolvedRepo):
        raise RepoError('machine_config requires a loaded repo')
    return {**prog['_machine'], **{key: prog[key] for key in
                                  ('name', 'repo', 'worktrees', 'remote', 'default_branch')}}


DOCS_ONLY_GLOBS = ("docs/**", "*.md", "GLOSSARY.md", "LICENSE", "!.github/**")


def cleanup_config(prog):
    """Validated committed output declarations and an optional machine budget."""
    policy = prog.get("cleanup", {})
    if not isinstance(policy, dict) or set(policy) - {"disposable"}:
        raise RepoError("cleanup must contain only disposable")
    outputs = policy.get("disposable", [])
    if not isinstance(outputs, list) or not all(
            isinstance(path, str) and path and not any(c in path for c in "\\:\x00")
            and not path.startswith("/") and all(part.casefold() not in ("", ".", "..", ".git")
                                                  for part in path.split("/")) for path in outputs):
        raise RepoError("cleanup.disposable must contain canonical relative paths or globs outside .git")
    budget = prog.get("budget_gb")
    if budget is not None and (type(budget) not in (int, float) or not math.isfinite(budget) or budget <= 0):
        raise RepoError("budget_gb must be finite and positive")
    return {"disposable": list(outputs), "budget_gb": budget}


def normalize(raw, name=None, *, sources=None):
    """A validated repo dict with every default filled in."""
    original = raw
    cleanup = cleanup_config(raw)
    seed = config._source_leaves(raw, "injected") if sources is not None else None
    if seed is not None:
        seed.update({key: sources[key] for key in seed if key in sources})
    name = str(raw.get("name") or name or "")
    if not name:
        raise RepoError("a repo needs a name")
    if not raw.get("repo"):
        raise RepoError(f"{name}: repo is required")
    raw = {**raw, "repo": os.path.expanduser(str(raw["repo"]))}
    raw["worktrees"] = os.path.expanduser(str(raw.get("worktrees") or worktrees_for(name, raw["repo"])))
    g = dict(raw.get("gate") or {})
    run = dict(raw.get("run") or {})
    plats = run.get("platforms", list(PLATFORMS))
    if not isinstance(plats, list) or not plats or not all(p in PLATFORMS for p in plats):
        raise RepoError(f"{name}: run.platforms must be a non-empty list drawn from {PLATFORMS}")
    platform = str(g.get("platform") or ((raw.get("run") or {}).get("platforms") or ["windows"])[0]).lower()
    if platform not in GATE_PLATFORMS:
        raise RepoError(f"{name}: gate.platform must be one of {GATE_PLATFORMS}")
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
    for key, default_limit in (("timeout_s", 3600), ("idle_s", 1200)):
        try:
            g[key] = float(g.get(key) or default_limit)
        except (TypeError, ValueError) as e:
            raise RepoError(f"{name}: gate.{key} must be a number") from e
    out = {"cleanup": {"disposable": cleanup["disposable"]}, "budget_gb": cleanup["budget_gb"],
            "name": name, "repo": str(raw["repo"]), "worktrees": str(raw["worktrees"]),
            "default_branch": main,
            "remote": remote,
            "run": {"platforms": plats, "default": default, "env": dict(env), "sandbox_writable_roots": list(roots)},
            "scripts": dict(scripts), "brief": brief,
            "gate": {"platform": platform, "unknown": unknown,
                     "timeout_s": g["timeout_s"], "idle_s": g["idle_s"],
                     "docs_only": docs_only, "docs_only_globs": docs_globs,
                     "hint": "Add [scripts] test to .drover/config.toml"},
            "review": {"backend": backend, "seat": rev.get("seat"), "model": model,
                       "effort": rev.get("effort"), "high_risk_model": high_model},
            "landing": {"mode": mode, "push": push, "remote": remote, "main": main},
            "land": {"policy": policy, "push": push}}

    if sources is not None:
        origins = config._source_leaves(out, "default")
        origins.update({key: seed[key] for key in origins if key in seed})
        output_origins = [v for k, v in seed.items() if k.startswith("cleanup.disposable")]
        if output_origins:
            origins["cleanup.disposable"] = output_origins[0]
        if not original.get("name"):
            origins["name"] = "injected"
        def derive(key, *dependencies):
            deps = {}
            for dep in dependencies:
                if dep.startswith("config."):
                    label = config.source_of(dep[7:])
                    if label.startswith("aggregate:"):
                        deps.update({"config." + k: v for k, v in json.loads(label[10:]).items()})
                    else:
                        deps[dep] = label
                else:
                    terminals = config._source_leaves(config.dig(out, dep), "default",
                                                       tuple(config._selector_parts(dep)))
                    deps.update({k: origins[k] for k in terminals})
            origins[key] = "derived:" + json.dumps(deps, sort_keys=True, separators=(",", ":"))
        # Platform normalization orders and deduplicates the supplied platform list.
        supplied_run = original.get("run") or {}
        if "platforms" in supplied_run:
            for i, platform_name in enumerate(plats):
                j = supplied_run["platforms"].index(platform_name)
                origins[f"run.platforms.{i}"] = seed.get(f"run.platforms.{j}", "injected")
        if not original.get("worktrees"):
            derive("worktrees", "name", "repo", "config.paths.worktrees")
        if not supplied_run.get("default"):
            derive("run.default", "run.platforms")
        if not (original.get("gate") or {}).get("platform"):
            if supplied_run.get("platforms"):
                derive("gate.platform", "run.platforms")
            else:
                origins["gate.platform"] = "default"
        derive("gate.unknown", "scripts")
        origins["gate.hint"] = "default"
        for target, alias in (("remote", "landing.remote"), ("default_branch", "landing.main")):
            if alias in seed:
                origins[target] = seed[alias]
        origins["landing.remote"] = origins["remote"]
        origins["landing.main"] = origins["default_branch"]
        origins["landing.push"] = seed.get("landing.push", seed.get("land.push", "default"))
        origins["land.push"] = origins["landing.push"]
        if not rev.get("model"):
            derive("review.model", "config.models.review")
        if not rev.get("backend"):
            if seat:
                index = next(i for i, item in enumerate(config.get()["seats"]) if item["name"] == rev["seat"])
                derive("review.backend", "review.seat", f"config.seats.{index}.backend")
            else:
                derive("review.backend", "review.model", "config.models.ladders")
        if not rev.get("model") and models["review"] not in models["ladders"][backend]:
            derive("review.model", "config.models.review", "review.backend", f"config.models.ladders.{backend}")
        if not rev.get("high_risk_model"):
            derive("review.high_risk_model", "review.backend",
                   "config.models.director" if backend == "claude" else "review.model")
        sources.clear()
        sources.update(origins)
    return out


def repo_name(prog):
    return prog["name"]


def repo_path(prog):
    """The configured checkout path, before placement translates it."""
    return prog["repo"]


def worktree_root(prog):
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


def ci_policy(prog):
    """Validated CI planning and runner limits, owned by the repo loader."""
    return {**prog["gate"], "base": prog["default_branch"]}


def load_all(directory=None):
    """{lowercase name: repo} for every *.toml in the repos directory."""
    out = {}
    for path in sorted(Path(directory or repos_dir()).glob("*.toml")):
        raw = read_machine(path)
        prog = load({**raw, "name": raw.get("name") or path.stem}, machine_path=path)
        out[prog["name"].lower()] = prog
    return out


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
            try:
                versioned = tomllib.loads(result.stdout)
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
