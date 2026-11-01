"""Dispatch hold: a ready task waits while an active branch has really changed files its scope is likely to collide with.

Ported from the Herdr skill's admit-slice checks: a shared changed file, or a magnet file (a shared append-only list
or generated file that conflicts on nearly every merge) matched by the same pattern on both sides. Git failure or an
unknown branch holds nothing. Git never runs inside a ledger transaction.
"""
import fnmatch
import logging
import os
import subprocess

from .config_defaults import DEFAULT_MAGNETS
from .ledger import scope as scopes
from .ledger import states

log = logging.getLogger(__name__)

_CACHE = {}        # (repo, branch tip, base tip) -> changed files; a tip names its content, so it never goes stale


def _git(repo, *args):
    """Stdout of `git -C repo args`, or None when git fails."""
    try:
        done = subprocess.run(["git", "-C", repo, *args], text=True, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def _tip(git, repo, ref):
    out = git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return out.strip() if out and out.strip() else None


def changed_files(repo, branch, base_ref, cache, git=None):
    """Files the branch changed since it left base_ref (git diff base...branch), or None when unknown."""
    git = git or _git
    if not repo or not branch or not base_ref:
        return None
    tip, base = _tip(git, repo, branch), _tip(git, repo, base_ref)
    if not tip or not base:
        return None
    key = (repo, tip, base)
    if key not in cache:
        out = git(repo, "diff", "--name-only", f"{base}...{tip}")
        if out is None:
            return None
        cache[key] = {line.strip() for line in out.splitlines() if line.strip()}
    return cache[key]


def _matches(path, patterns):
    return any(fnmatch.fnmatch(path, p) or fnmatch.fnmatch(os.path.basename(path), p) for p in patterns)


def reason(candidate_scope, other_files, patterns):
    """Why the candidate would likely collide with a branch that changed other_files, or None."""
    for path in sorted(other_files or ()):
        if any(scopes.overlap(g, path) for g in candidate_scope or ()):
            return f"shares file {path}"
    for path in sorted(other_files or ()):
        if _matches(path, patterns):
            for g in candidate_scope or ():
                if _matches(g, patterns):
                    return f"magnet file {path} (candidate scope {g})"
    return None


def holds(ready, active, cfg, git=None, cache=None, base_of=None):
    """{task id: reason} for ready tasks to hold. cfg is the [dispatch] table; base_of(task) names the base ref."""
    if not cfg.get("conflict_hold", True):
        return {}
    cache = _CACHE if cache is None else cache
    patterns = list(cfg.get("magnets", DEFAULT_MAGNETS))
    base_of = base_of or (lambda task: "main")
    files = {}
    out = {}
    for task in ready:
        if not task.get("scope_files"):
            continue
        for other in active:
            if other["id"] == task["id"] or other.get("repo") != task.get("repo") or not other.get("branch"):
                continue
            if other["id"] not in files:
                repo = other.get("repo_path") or task.get("repo_path")
                files[other["id"]] = changed_files(repo, other["branch"], base_of(other), cache, git)
            why = reason(task["scope_files"], files[other["id"]], patterns)
            if why:
                out[task["id"]] = f"held behind {other['id']}: {why}"
                break
    return out


def tick_holds(port, cfg, base_of, git=None):
    """The dispatcher's entry: holds for the ledger's ready tasks. Any failure logs and holds nothing."""
    try:
        active = [t for status in states.ACTIVE for t in port.list_tasks(status)]
        return holds(port.list_tasks("ready"), active, cfg, git=git, base_of=base_of)
    except Exception:
        log.exception("conflict hold failed; nothing is held")
        return {}
