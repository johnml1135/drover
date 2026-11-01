"""Detect landings made outside Drover: git proves them, so nobody has to record them.

Only commits the ledger ties to the task can prove a landing, and the task's change must still be on the base: a
branch name can be reused, and history cannot see a revert. Every failure or unknown state returns no proof, and git
never runs inside a ledger transaction. The scan is synchronous, so its time is budgeted and a stuck repo stops early.
"""
import logging
import re
import subprocess
import time

from . import ci, config, disk, landing, placement, repos
from .ledger import ops, states

INTERVAL_S = 600
GIT_TIMEOUT_S = 30
SCAN_BUDGET_S = 60
RESUME_KEY = 'outside_land_resume'
SHA = re.compile('[0-9a-f]{40}')
log = logging.getLogger(__name__)


def interval_s(cfg):
    return cfg.get('watch', {}).get('interval_s', INTERVAL_S)


def _out(repo, *args, **kw):
    """Git's stripped stdout, or None for any nonzero exit. A timeout raises TimeoutExpired."""
    result = landing.timed_git(repo, *args, timeout=GIT_TIMEOUT_S, **kw)
    return None if result.returncode else result.stdout.strip()


def _count(repo, base, sha, kind):
    return _out(repo, 'rev-list', '--count', kind, f'{base}..{sha}')


def _replayed(repo, base, sha):
    """No commit past the base is a merge, which `git cherry` skips, and the base holds the patch of each.
    The caller has shown `sha` is not on the base, so there is at least one."""
    return _count(repo, base, sha, '--merges') == '0' and landing.contained(repo, sha, base, timeout=GIT_TIMEOUT_S)


def _adds_nothing(repo, base, sha):
    """`sha` changed something, and merging it into the base changes nothing."""
    fork = _out(repo, 'merge-base', base, sha)
    merged = _out(repo, 'merge-tree', '--write-tree', base, sha)
    if not fork or not merged:
        return False
    fork_tree, sha_tree, base_tree = (_out(repo, 'rev-parse', '--verify', f'{commit}^{{tree}}') for commit in (fork, sha, base))
    return bool(base_tree) and fork_tree != sha_tree and merged == base_tree


def _survives(repo, base, start, sha):
    """The task changed something, and merging `sha` into the base changes nothing from any point the task passed
    through, so all of it is still there. One span cannot show this: a run that only restores what `start[0]`, the first
    run's base, held is in no diff from it. Each point merges to `sha`, not the next commit, because a run may remove
    what a later run restores. This compares trees: a conflict or a later edit of the same lines is a missed proof,
    never a false one."""
    first, *later = [start] if isinstance(start, str) else start or [None]
    if not first or not all(later):
        return False
    first_tree, sha_tree, base_tree = (_out(repo, 'rev-parse', '--verify', f'{commit}^{{tree}}') for commit in (first, sha, base))
    if not base_tree or first_tree == sha_tree:
        return False
    return all(_out(repo, 'merge-tree', '--write-tree', f'--merge-base={point}', base, sha) == base_tree
               for point in dict.fromkeys(point for point in (first, *later) if point == first or point != sha))


def _proof(repo, branch, base_ref, commits, start):
    if not commits:
        return None
    recorded = commits[-1]    # an earlier attempt on the base says nothing about the task's final output
    tip = _out(repo, 'rev-parse', '--verify', '-q', f'refs/heads/{branch}^{{commit}}')
    base = _out(repo, 'rev-parse', '--verify', '-q', f'{base_ref}^{{commit}}')
    if not tip or not base:
        return None
    own = _count(repo, base, tip, '--no-merges')
    if own is None or (own != '0' and tip != recorded):
        return None           # the branch holds work the ledger never saw
    if _out(repo, 'merge-base', '--is-ancestor', recorded, base) is not None:
        proof = {'sha': recorded, 'proof': 'ancestor'}
    elif _replayed(repo, base, recorded):
        proof = {'sha': base, 'proof': 'patch-equivalent'}
    elif _adds_nothing(repo, base, recorded):
        proof = {'sha': base, 'proof': 'adds-nothing'}
    else:
        return None
    return proof if _survives(repo, base, start, recorded) else None


def git_proof(repo, branch, base_ref, commits=(), start=None):
    """{'sha', 'proof'} when git proves the task's newest recorded commit landed on `base_ref` and is still there.

    `commits` are the commits the ledger recorded for the task, and `start` the base of its first worker run, or
    the list `candidates` gives: that base, then the task's earlier commits and the base of the run that made the newest.
    `sha` is always on the base: the recorded commit for 'ancestor', the base itself for the other proofs, whose
    recorded commit never reached it. A timeout is unknown, so it gives None."""
    try:
        return _proof(repo, branch, base_ref, commits, start)
    except (OSError, ValueError, subprocess.SubprocessError, ci.CIError):
        return None


def _evidence(conn, task_id):
    done = [e['data'] for e in ops.events(conn, task_id=task_id, kind='worker-done')
            if SHA.fullmatch(e['data'].get('sha') or '')]
    bases = {e['data'].get('run'): (e['data'].get('output_snapshot') or {}).get('base')
             for e in ops.events(conn, task_id=task_id, kind='run-launching')}
    first = next((e['data'].get('run') for e in ops.events(conn, task_id=task_id, kind='run-started')
                  if e['data'].get('role') == 'worker'), None)
    start, commits = bases.get(first), [d['sha'] for d in done]
    points = [start, *commits[:-1], bases.get(done[-1].get('run'))] if done else [start]
    return {'commits': commits, 'start': points if SHA.fullmatch(start or '') else None}


def candidates(conn):
    """Write tasks with a branch that await an answer and have no live run, with their recorded commits and `start`."""
    found = []
    for status in (states.YANKED, states.NEEDS_OWNER):
        for task in ops.list_tasks(conn, status=status):
            if task['write'] and task['branch'] and not task.get('internal') and not any(
                    run['ended'] is None for run in ops.list_runs(conn, task['id'])):
                found.append({**task, **_evidence(conn, task['id'])})
    return found


def _base(prog):
    """The base as the ledger names it and the full ref git reads, as record_external_landing resolves them."""
    branch = repos.default_branch(prog)
    if prog['landing']['push']:
        name = f'{repos.remote_name(prog)}/{branch}'
        return name, f'refs/remotes/{name}'
    return branch, f'refs/heads/{branch}'


def _wsl_up():
    """Whether WSL is running; asking starts nothing, and a stopped WSL is never booted for a scan."""
    try:
        return placement.wsl_running(disk.tracker_distro(config.get()))
    except (OSError, subprocess.SubprocessError):
        return False


def _in_turn(conn, tasks):
    """The candidates, starting with the one the last scan ran out of time before."""
    ids = [task['id'] for task in tasks]
    resume = ops.get_setting(conn, RESUME_KEY, '')
    return tasks[ids.index(resume):] + tasks[:ids.index(resume)] if resume in ids else tasks


def _detect(conn, task, prog):
    name, ref = _base(prog)
    proof = _proof(repos.native(repos.repo_path(prog)), task['branch'], ref, task['commits'], task['start'])
    if proof:
        ops.record_detected_landing(conn, task['id'], proof['sha'], name, proof['proof'])
    return bool(proof)


def tick(conn, cfg, *, decide=None, now=time.monotonic):
    """Scan the candidates once. Proofs are found with no transaction open; each landing is one short transaction.

    A repo stops at its first timeout. The whole scan stops at SCAN_BUDGET_S and resumes there at the next interval."""
    tasks, landed, stuck, up, scanned, started = _in_turn(conn, candidates(conn)), [], set(), None, 0, now()
    programs, resume = repos.load_all() if tasks else {}, ''
    for task in tasks:
        if now() - started >= SCAN_BUDGET_S:
            resume = task['id']
            log.warning('outside landing scan ran out of time; resuming at %s', resume)
            break
        key = task['repo'].lower()
        prog = programs.get(key)
        if prog is None or key in stuck:
            continue
        try:
            if placement.routes_through_wsl(repos.repo_path(prog)):
                up = _wsl_up() if up is None else up
                if not up:
                    continue
            scanned += 1
            if _detect(conn, task, prog):
                landed.append(task['id'])
        except subprocess.TimeoutExpired:
            stuck.add(key)
            log.warning('outside landing scan skips %s: git timed out', key)
        except (OSError, LookupError, ValueError, PermissionError, subprocess.SubprocessError, ci.CIError) as error:
            log.warning('landing of %s not detected: %s', task['id'], error)     # IllegalTransition is a ValueError
    if ops.get_setting(conn, RESUME_KEY, '') != resume:
        ops.set_setting(conn, RESUME_KEY, resume)
    return {'scanned': scanned, 'landed': landed}
