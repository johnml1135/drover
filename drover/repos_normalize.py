"""Validate and normalize repository configuration."""
import json
import math
import os
from pathlib import Path

from . import config

PLATFORMS = ("windows", "wsl")

GATE_PLATFORMS = PLATFORMS + ("both",)

DEFAULTS = {"default_branch": "main", "remote": "origin"}

DOCS_ONLY_GLOBS = ("docs/**", "*.md", "GLOSSARY.md", "LICENSE", "!.github/**")


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

def worktrees_for(name, repo):
    """Where a repo's worktrees go when it does not say: <paths.worktrees>\\<name>.worktrees for a shared root,
    else <repo>.worktrees next to the repo."""
    shared = config.get()["paths"]["worktrees"].rstrip("\\/")
    if shared:
        return shared + ("\\" if "\\" in shared else "/") + f"{name}.worktrees"
    repo = str(repo).rstrip("\\/")
    return repo + ".worktrees"
