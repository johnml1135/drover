"""Explicit owner-approved branch disposition with rescue refs and checked Git operations.

Automatic disk cleanup does not use this operation.
"""
import os
import re
import time
from pathlib import Path

from . import placement


def git(cwd, *args, check=False):
    # GIT_OPTIONAL_LOCKS=0: `git status` must not rewrite the index, so a scan writes nothing.
    r = placement.git(cwd, *args, env=dict(os.environ, GIT_OPTIONAL_LOCKS='0'))
    if check and r.returncode:
        raise RuntimeError('git %s: %s' % (' '.join(args), r.stderr.strip()))
    return r

DELETE_DISPOSITIONS = ('script-delete', 'propose-delete', 'owner-review')  # rows the owner can approve as a delete


class GitError(RuntimeError):
    pass


def _git_ok(repo, *args):
    """stdout of a git command that must succeed; anything else is a GitError (callers fail closed)."""
    r = git(repo, *args)
    if r.returncode:
        raise GitError('git %s: rc %d %s' % (' '.join(args[:2]), r.returncode, r.stderr.strip()))
    return r.stdout


def default_branch(repo):
    """The remote default branch name (origin/HEAD), else main or master when one exists on origin; None if unknown.
    The name is only a claim: delete_branch verifies refs/remotes/origin/<name> resolves to a commit."""
    r = git(repo, 'symbolic-ref', '--short', 'refs/remotes/origin/HEAD')
    if r.returncode == 0 and r.stdout.strip().startswith('origin/'):
        return r.stdout.strip()[len('origin/'):]
    for name in ('main', 'master'):
        if git(repo, 'rev-parse', '--verify', '-q', 'refs/remotes/origin/' + name).returncode == 0:
            return name
    return None


def checked_out_anywhere(repo, branch):
    out = _git_ok(repo, 'worktree', 'list', '--porcelain').splitlines()
    return ('branch refs/heads/' + branch) in [ln.strip() for ln in out]


def valid_branch_name(repo, branch):
    if not isinstance(branch, str) or not branch or branch.startswith('-') or '@{' in branch or '..' in branch \
            or branch.startswith('refs/') or branch.startswith('/') or '\x00' in branch:
        return False
    return git(repo, 'check-ref-format', '--branch', branch).returncode == 0


ZERO = '0' * 40


def is_merged(repo, sha, upstream):
    """True only on positive evidence: merge-base --is-ancestor rc 0, or `git cherry` rc 0 with output and no '+'.
    rc 1 from merge-base is a clean "no"; any other rc, a cherry error or empty cherry output is NOT merged.
    Raises GitError when merge-base itself fails (rc other than 0 or 1)."""
    rc = git(repo, 'merge-base', '--is-ancestor', sha, upstream).returncode
    if rc == 0:
        return True
    if rc != 1:
        raise GitError('git merge-base: rc %d' % rc)
    r = git(repo, 'cherry', upstream, sha)
    lines = r.stdout.splitlines()
    return r.returncode == 0 and bool(lines) and not any(ln.startswith('+') for ln in lines)


def delete_branch(repo, branch, approved_row, log=None, now=None, rescue_root=None, bundle=False):
    """Delete one local branch that has no worktree, after rechecking it with argv git. Local only: never a remote.

    Fails closed: any git error, an unresolvable origin/<default>, a hostile name, a row that does not name this
    branch, the default branch, a branch checked out anywhere, or one that is gone refuses. Merged (see is_merged):
    delete. Otherwise only a row approved as an unmerged delete proceeds: a never-overwritten
    refs/drover-rescue/<branch>-<date>-<sha> ref (verified) first, and with `bundle` a `git bundle` at
    <rescue_root>/<repo name>/<branch>.bundle (a failed bundle stops the delete). The delete is
    `update-ref -d refs/heads/<b> <sha seen at check time>`, so a branch that moved is kept. `log(step, **data)` is
    called for every step. Returns {ok, reason|..., steps}."""
    steps = []

    def step(name, **data):
        steps.append(dict(step=name, **data))
        if log:
            log(name, branch=branch, **data)

    def refuse(reason):
        step('refused', reason=reason)
        return {'ok': False, 'reason': reason, 'steps': steps}

    try:
        return _delete_branch(repo, branch, approved_row, step, refuse, steps, now, rescue_root, bundle)
    except (GitError, OSError) as e:
        return refuse('git error, nothing deleted: %s' % e)


def _delete_branch(repo, branch, approved_row, step, refuse, steps, now, rescue_root, bundle):
    if not valid_branch_name(repo, branch):
        return refuse('invalid branch name')
    if not approved_row or approved_row.get('branch') != branch:
        return refuse('the row does not name this branch')
    base = default_branch(repo)
    if base is None:
        return refuse('cannot determine the default branch')
    if branch == base or branch in ('main', 'master'):
        return refuse('default branch')
    upstream = 'refs/remotes/origin/' + base
    if git(repo, 'rev-parse', '--verify', '-q', upstream + '^{commit}').returncode:
        return refuse('origin/%s does not resolve to a commit' % base)
    r = git(repo, 'rev-parse', '--verify', '-q', 'refs/heads/' + branch + '^{commit}')
    sha = r.stdout.strip()
    if r.returncode or not re.fullmatch(r'[0-9a-f]{40,64}', sha):
        return refuse('no such local branch')
    if checked_out_anywhere(repo, branch):
        return refuse('checked out in a worktree')
    merged = is_merged(repo, sha, upstream)
    step('checked', head=sha, merged=merged, base='origin/' + base)
    rescue = bundle_path = None
    if not merged:
        if approved_row.get('disposition') not in DELETE_DISPOSITIONS:
            return refuse('unmerged and not approved as a delete (%s)' % approved_row.get('disposition'))
        date = time.strftime('%Y%m%d', time.gmtime(now or time.time()))
        rescue = 'refs/drover-rescue/%s-%s-%s' % (branch, date, sha[:8])
        if git(repo, 'update-ref', rescue, sha, ZERO).returncode:  # old value zero: never overwrite
            return refuse('rescue ref already exists or could not be created')
        if git(repo, 'rev-parse', '--verify', '-q', rescue + '^{commit}').stdout.strip() != sha:
            return refuse('rescue ref does not resolve to the branch tip')
        step('rescue-ref', ref=rescue)
        if bundle:
            root = Path(rescue_root) / Path(repo).name
            root.mkdir(parents=True, exist_ok=True)
            bundle_path = root / (branch.replace('/', '__') + '.bundle')
            if git(repo, 'bundle', 'create', str(bundle_path), 'refs/heads/' + branch).returncode:
                return refuse('could not write the bundle')
            step('bundle', path=str(bundle_path))
    if git(repo, 'update-ref', '-d', 'refs/heads/' + branch, sha).returncode:
        return refuse('branch moved or could not be deleted')
    step('deleted', head=sha, rescue_ref=rescue, bundle=str(bundle_path) if bundle_path else None)
    return {'ok': True, 'head': sha, 'rescue_ref': rescue, 'bundle': str(bundle_path) if bundle_path else None,
            'steps': steps}

