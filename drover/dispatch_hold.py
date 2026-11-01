"""Dispatch hold: a ready task waits while an active branch has really changed files its scope is likely to collide with.

Ported from the Herdr skill's admit-slice checks: a shared changed file, or a magnet file (a shared append-only list
or generated file that conflicts on nearly every merge) matched by the same pattern on both sides. Git failure or an
unknown branch holds nothing, and so does a repo with no configured checkout: `tasks.repo_path` is optional and dispatch
never fills it, so the checkout comes from the repo registry. Git never runs inside a ledger transaction.
"""
import fnmatch
import logging
import os
import subprocess

from . import placement
from .config_defaults import DEFAULT_MAGNETS
from .ledger import scope as scopes
from .ledger import states

log = logging.getLogger(__name__)

_CACHE = {}        # (repo, branch tip, base tip) -> changed files; a tip names its content, so it never goes stale


def _git(repo, *args):
    """Stdout of git in the checkout, or None when git fails."""
    try:
        done = placement.git(repo, *args, timeout=60)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def branch_tips(repo, git=None):
    """{refname: sha} for every ref in the repo, from one `git for-each-ref`; None when git fails."""
    out = (git or _git)(repo, "for-each-ref", "--format=%(objectname) %(refname)")
    if out is None:
        return None
    tips = {}
    for line in out.splitlines():
        sha, _, name = line.strip().partition(" ")
        if sha and name:
            tips[name] = sha
    return tips


def changed_files(repo, branch, base_ref, cache, git=None, tips=None, seen=None):
    """Files the branch changed since it left base_ref (git diff base...branch), or None when unknown.

    base_ref is a full ref name (refs/heads/main, or refs/remotes/origin/main for a pushing repo). Tips come from
    one for-each-ref per repo (pass `tips` to share it); git runs again only on a cache miss, keyed by
    (repo, base tip, branch tip). A ref missing from the listing is unknown and costs no further git call.
    """
    git = git or _git
    if not repo or not branch or not base_ref:
        return None
    if tips is None:
        tips = branch_tips(repo, git)
    if not tips:
        return None
    tip, base = tips.get(f"refs/heads/{branch}"), tips.get(base_ref)
    if not tip or not base:
        return None
    key = (repo, base, tip)
    if seen is not None:
        seen.add(key)
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


def holds(ready, active, cfg, repo_of, git=None, cache=None, base_of=None):
    """{task id: reason} for ready tasks to hold. cfg is the [dispatch] table; repo_of(task) names the task's checkout,
    or None to leave its repo alone; base_of(task) names the base ref.

    Git runs lazily: not at all without a scoped ready task and an active branch in its repo; otherwise one
    for-each-ref per repo, then a diff per uncached branch tip. The cache keeps only the keys seen this call.
    """
    if not cfg.get("conflict_hold", True):
        return {}
    cache = _CACHE if cache is None else cache
    patterns = list(cfg.get("magnets", DEFAULT_MAGNETS))
    base_of = base_of or (lambda task: "refs/heads/main")
    scoped = [t for t in ready if t.get("scope_files")]
    if not scoped:
        return {}
    checkouts, tips, files, seen, out = {}, {}, {}, set(), {}
    for task in scoped:
        for other in active:
            if other["id"] == task["id"] or other.get("repo") != task.get("repo") or not other.get("branch"):
                continue
            if other["id"] not in files:
                name = other.get("repo")
                if name not in checkouts:
                    checkouts[name] = repo_of(other)
                repo = checkouts[name]
                if repo not in tips:
                    tips[repo] = branch_tips(repo, git) if repo else None
                files[other["id"]] = changed_files(repo, other["branch"], base_of(other), cache, git,
                                                   tips=tips[repo], seen=seen)
            why = reason(task["scope_files"], files[other["id"]], patterns)
            if why:
                out[task["id"]] = f"held behind {other['id']}: {why}"
                break
    for key in [k for k in cache if k not in seen]:
        del cache[key]
    return out


def tick_holds(port, cfg, base_of, repo_of, git=None):
    """The dispatcher's entry: holds for the ledger's ready tasks. Any failure logs and holds nothing."""
    try:
        active = [t for status in states.ACTIVE for t in port.list_tasks(status)]
        return holds(port.list_tasks("ready"), active, cfg, repo_of, git=git, base_of=base_of)
    except Exception:
        log.exception("conflict hold failed; nothing is held")
        return {}
