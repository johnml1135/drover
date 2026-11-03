"""Explicit owner-approved branch disposition with rescue refs and checked Git operations.

Automatic disk cleanup does not use this operation.
"""
import os
import re
import time
from pathlib import Path

from . import config, git_client, git_worktrees
from .disk_state import CleanupLock


DELETE_DISPOSITIONS = ('script-delete', 'propose-delete', 'owner-review')  # rows the owner can approve as a delete


class GitError(RuntimeError):
    pass


def _env():
    return dict(os.environ, GIT_OPTIONAL_LOCKS='0')     # a scan must not rewrite the index, so it writes nothing


def _ask(repo, *args, no=(1,)):
    """A read at LONG, which had no timeout: any exit is read by its code, as before; a run that did not end or could not
    start is a GitError, so delete_branch refuses rather than letting a raw timeout escape."""
    reply = git_client.ask(repo, *args, timeout_s=git_client.LONG, no=no, env=_env())
    if reply.why in ('timeout', 'unavailable'):
        raise GitError('git %s: %s' % (' '.join(args[:2]), reply.why))
    return reply


def _changed(repo, *args):
    """Whether a ref change worked; one that did not, a timeout included, refuses as a failed one did."""
    return git_client.mutate(repo, *args, timeout_s=git_client.LONG, env=_env()).ok


def default_branch(repo):
    """The remote default branch name (origin/HEAD), else main or master when one exists on origin; None if unknown.
    The name is only a claim: delete_branch verifies refs/remotes/origin/<name> resolves to a commit."""
    r = _ask(repo, 'symbolic-ref', '--short', 'refs/remotes/origin/HEAD')
    if r.answer is git_client.Answer.YES and r.out.strip().startswith('origin/'):
        return r.out.strip()[len('origin/'):]
    for name in ('main', 'master'):
        if _ask(repo, 'rev-parse', '--verify', '-q', 'refs/remotes/origin/' + name).answer is git_client.Answer.YES:
            return name
    return None


def checked_out_anywhere(repo, branch):
    rows = git_worktrees.listing(repo, timeout_s=git_client.LONG).rows    # a failure raises; delete_branch refuses
    return any(row.get('branch') == 'refs/heads/' + branch for row in rows)


def valid_branch_name(repo, branch):
    if not isinstance(branch, str) or not branch or branch.startswith('-') or '@{' in branch or '..' in branch \
            or branch.startswith('refs/') or branch.startswith('/') or '\x00' in branch:
        return False
    return _ask(repo, 'check-ref-format', '--branch', branch).answer is git_client.Answer.YES


ZERO = '0' * 40


def is_merged(repo, sha, upstream):
    """True only on positive evidence: merge-base --is-ancestor rc 0, or `git cherry` rc 0 with output and no '+'.
    rc 1 from merge-base is a clean "no"; any other rc, a cherry error or empty cherry output is NOT merged.
    Raises GitError when merge-base itself fails (rc other than 0 or 1)."""
    ancestor = _ask(repo, 'merge-base', '--is-ancestor', sha, upstream)
    if ancestor.answer is git_client.Answer.YES:
        return True
    if ancestor.answer is git_client.Answer.UNKNOWN:
        raise GitError('git merge-base: rc %s' % ancestor.code)
    r = _ask(repo, 'cherry', upstream, sha)
    lines = r.out.splitlines()
    return r.answer is git_client.Answer.YES and bool(lines) and not any(ln.startswith('+') for ln in lines)


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
        with CleanupLock(config.state_dir()):
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
    if _ask(repo, 'rev-parse', '--verify', '-q', upstream + '^{commit}').answer is not git_client.Answer.YES:
        return refuse('origin/%s does not resolve to a commit' % base)
    r = _ask(repo, 'rev-parse', '--verify', '-q', 'refs/heads/' + branch + '^{commit}')
    sha = r.out.strip()
    if r.answer is not git_client.Answer.YES or not re.fullmatch(r'[0-9a-f]{40,64}', sha):
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
        if not _changed(repo, 'update-ref', rescue, sha, ZERO):  # old value zero: never overwrite
            return refuse('rescue ref already exists or could not be created')
        if _ask(repo, 'rev-parse', '--verify', '-q', rescue + '^{commit}').out.strip() != sha:
            return refuse('rescue ref does not resolve to the branch tip')
        step('rescue-ref', ref=rescue)
        if bundle:
            root = Path(rescue_root) / Path(repo).name
            root.mkdir(parents=True, exist_ok=True)
            bundle_path = root / (branch.replace('/', '__') + '.bundle')
            if not _changed(repo, 'bundle', 'create', str(bundle_path), 'refs/heads/' + branch):
                return refuse('could not write the bundle')
            step('bundle', path=str(bundle_path))
    if not _changed(repo, 'update-ref', '-d', 'refs/heads/' + branch, sha):
        return refuse('branch moved or could not be deleted')
    step('deleted', head=sha, rescue_ref=rescue, bundle=str(bundle_path) if bundle_path else None)
    return {'ok': True, 'head': sha, 'rescue_ref': rescue, 'bundle': str(bundle_path) if bundle_path else None,
            'steps': steps}

