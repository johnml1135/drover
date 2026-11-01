"""Detect landings made outside Drover: git proves them, so nobody has to record them.

A branch that never diverged looks contained in its base, so containment alone proves nothing. Every failure or
unknown state returns no proof, and git never runs inside a ledger transaction.
"""
import logging
import re
import subprocess

from . import ci, landing, repos
from .ledger import ops, states

INTERVAL_S = 600
log = logging.getLogger(__name__)


def interval_s(cfg):
    return cfg.get('watch', {}).get('interval_s', INTERVAL_S)


def _out(repo, *args):
    """Git's stripped stdout, or None for any nonzero exit."""
    result = landing.git(repo, *args, check=False, timeout=30)
    return None if result.returncode else result.stdout.strip()


def _count(repo, base, sha, kind):
    return _out(repo, 'rev-list', '--count', kind, f'{base}..{sha}')


def _replayed(repo, base, sha):
    """No commit past the base is a merge, which `git cherry` skips, and the base holds the patch of each.
    The caller has shown there is at least one, or `contained` would be true of nothing."""
    return _count(repo, base, sha, '--merges') == '0' and landing.contained(repo, sha, base)


def _adds_nothing(repo, base, tip):
    """The branch changed something, and merging it into the base changes nothing."""
    fork = _out(repo, 'merge-base', base, tip)
    merged = _out(repo, 'merge-tree', '--write-tree', base, tip)
    if not fork or not merged:
        return False
    fork_tree, tip_tree, base_tree = (_out(repo, 'rev-parse', '--verify', f'{sha}^{{tree}}') for sha in (fork, tip, base))
    return bool(base_tree) and fork_tree != tip_tree and merged == base_tree


def _proof(repo, branch, base_ref, commits):
    tip = _out(repo, 'rev-parse', '--verify', '-q', f'refs/heads/{branch}^{{commit}}')
    base = _out(repo, 'rev-parse', '--verify', '-q', f'{base_ref}^{{commit}}')
    if not tip or not base:
        return None
    own = _count(repo, base, tip, '--no-merges')
    if own is None:
        return None
    if own == '0':
        # A fast-forward and a branch that never diverged look the same to git: only the ledger can tell them apart.
        # Only the newest commit counts: an earlier attempt on the base says nothing about the task's final output.
        if not commits:
            return None
        if _out(repo, 'merge-base', '--is-ancestor', commits[-1], base) is not None:
            return {'sha': commits[-1], 'proof': 'ancestor'}
        return {'sha': base, 'proof': 'patch-equivalent'} if _replayed(repo, base, commits[-1]) else None
    if _replayed(repo, base, tip):
        return {'sha': base, 'proof': 'patch-equivalent'}
    if commits and _adds_nothing(repo, base, tip):
        return {'sha': base, 'proof': 'adds-nothing'}
    return None


def git_proof(repo, branch, base_ref, commits=()):
    """{'sha', 'proof'} when git proves `branch` landed on `base_ref`, else None.

    `commits` are the commits the ledger recorded for the task. `sha` is always on the base: the recorded commit for
    'ancestor', the base itself for the other proofs, whose branch commits never reached it."""
    try:
        return _proof(repo, branch, base_ref, commits)
    except (OSError, ValueError, subprocess.SubprocessError, ci.CIError):
        return None


def candidates(conn):
    """Write tasks with a branch that await an answer and have no live run, with their recorded commits."""
    found = []
    for status in (states.YANKED, states.NEEDS_OWNER):
        for task in ops.list_tasks(conn, status=status):
            if task['write'] and task['branch'] and not task.get('internal') and not any(
                    run['ended'] is None for run in ops.list_runs(conn, task['id'])):
                done = [e['data'].get('sha') for e in ops.events(conn, task_id=task['id'], kind='worker-done')]
                found.append({**task, 'commits': [sha for sha in done if re.fullmatch('[0-9a-f]{40}', sha or '')]})
    return found


def _base(prog):
    """The base as the ledger names it and the full ref git reads, as record_external_landing resolves them."""
    branch = repos.default_branch(prog)
    if prog['landing']['push']:
        name = f'{repos.remote_name(prog)}/{branch}'
        return name, f'refs/remotes/{name}'
    return branch, f'refs/heads/{branch}'


def tick(conn, cfg, *, decide=None):
    """Scan the candidates once. Proofs are found with no transaction open; each landing is one short transaction."""
    tasks, landed = candidates(conn), []
    programs = repos.load_all() if tasks else {}
    for task in tasks:
        prog = programs.get(task['repo'].lower())
        if prog is None:
            continue
        try:
            name, ref = _base(prog)
            proof = git_proof(repos.native(repos.repo_path(prog)), task['branch'], ref, task['commits'])
            if proof:
                ops.record_detected_landing(conn, task['id'], proof['sha'], name, proof['proof'])
                landed.append(task['id'])
        except (OSError, ValueError, PermissionError) as error:     # IllegalTransition and RepoError are ValueErrors
            log.warning('landing of %s not detected: %s', task['id'], error)
    return {'scanned': len(tasks), 'landed': landed}
