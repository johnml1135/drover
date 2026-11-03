"""Sync local main with its remote and keep its checkout consistent.

Every function takes `git=`, an object with git_client's `ask`, `out` and `mutate`: the git_client module by default,
and landing_control's ControlledGit, which guards the mutations, when landing drives it.
"""
from pathlib import Path
from . import git_client
from .git_client import FETCH, LONG, Answer
class SyncError(ValueError):
    pass
class FetchFailed(SyncError):
    pass
class DirtyCheckout(SyncError):
    pass
class MainMoved(SyncError):
    pass
def sync_error(failed):
    """`error=` for git_client: the command alone, as git's stderr can carry a credential-bearing remote URL."""
    return SyncError(f'git {failed.args_[0]} failed')

def fast_forward(repo, main, new, old, *, git=git_client):
    """Advance a branch through its clean checkout, or CAS its ref when unchecked out."""
    ref = f'refs/heads/{main}'
    listing = git.out(repo, 'worktree', 'list', '--porcelain', '-z', timeout_s=LONG, error=sync_error)
    checkouts = []
    for record in listing.split('\0\0'):
        fields = record.split('\0')
        if f'branch {ref}' in fields:
            checkouts.append(Path(fields[0].removeprefix('worktree ')))
    if not checkouts:
        if not git.mutate(repo, 'update-ref', ref, new, old, timeout_s=LONG).ok:     # atomic: a timeout changed nothing
            raise MainMoved(f'{main} moved during fast-forward; retry')
        return
    if len(checkouts) != 1:
        raise SyncError(f'{main} is checked out in multiple worktrees; owner intervention required')
    checkout = checkouts[0]
    if git.out(checkout, 'status', '--porcelain', '--untracked-files=normal', timeout_s=LONG, error=sync_error).strip():
        raise DirtyCheckout(f'{main} checkout dirty; owner intervention required')
    head = git.out(checkout, 'rev-parse', 'HEAD', timeout_s=LONG, error=sync_error).strip()
    on = git.ask(checkout, 'symbolic-ref', '-q', 'HEAD', timeout_s=LONG, no=(1,))
    branch = on.out.strip() if on.answer is Answer.YES else ''
    if head != old or branch != ref:
        raise MainMoved(f'{main} moved during fast-forward; retry')
    merged = git.mutate(checkout, 'merge', '--ff-only', '--no-autostash', new, timeout_s=LONG)
    if merged.why == 'exit':
        raise MainMoved(f'{main} moved during fast-forward; retry')
    if not merged.ok:       # it may have left .git/index.lock and a part-updated checkout: a retry would not help
        raise SyncError(f'{main} fast-forward did not finish (git merge: {merged.why}); owner intervention required')


def sync(repo, remote='origin', main='main', *, git=git_client, timeout=FETCH):
    """Fetch and advance main with compare-and-swap; preserve local unpushed landings."""
    fetched = git.mutate(repo, 'fetch', remote, main, f'--refmap=+refs/heads/{main}:refs/remotes/{remote}/{main}',
                         timeout_s=timeout)
    if not fetched.ok:
        raise FetchFailed(f'fetch failed: {remote}/{main}; repo paused until owner retries')
    upstream = git.out(repo, 'rev-parse', '--verify', f'refs/remotes/{remote}/{main}^{{commit}}', timeout_s=LONG,
                       error=sync_error).strip()
    ref = f'refs/heads/{main}'
    local = git.out(repo, 'rev-parse', '--verify', ref, timeout_s=LONG, error=sync_error).strip()
    if git.ask(repo, 'merge-base', '--is-ancestor', upstream, local, timeout_s=LONG, no=(1,)).answer is Answer.YES:
        return local
    behind = git.ask(repo, 'merge-base', '--is-ancestor', local, upstream, timeout_s=LONG, no=(1,)).answer
    if behind is Answer.UNKNOWN:
        raise SyncError('git merge-base failed')
    if behind is Answer.NO:
        raise SyncError(f'{main} diverged from {remote}')
    try:
        fast_forward(repo, main, upstream, local, git=git)
    except MainMoved as e:
        raise SyncError(f'{main} moved during origin sync; retry') from e
    return upstream


def observe(repo, remote, main, stopped, moved, tracking, interval, *, git=git_client):
    """Poll remote main without changing local refs or a checkout."""
    while not stopped.is_set():
        result = git.ask(repo, 'ls-remote', '--heads', remote, f'refs/heads/{main}', timeout_s=FETCH, no=())
        fields = result.out.split()
        if (result.answer is Answer.YES and len(fields) == 2 and fields[1] == f'refs/heads/{main}'
                and tracking and fields[0] != tracking):
            moved.set()
            return
        if stopped.wait(interval):
            return


def ahead(repo, remote='origin', main='main', *, timeout=LONG, git=git_client):
    """Local commits absent from the last fetched remote main; no network during rendering. None when git timed out."""
    r = git.ask(repo, 'rev-list', '--count', f'refs/remotes/{remote}/{main}..refs/heads/{main}', timeout_s=timeout, no=())
    return int(r.out.strip()) if r.answer is Answer.YES else (None if r.why == 'timeout' else 0)


def push_main(repo, remote='origin', main='main', *, guard=None, git=git_client):
    """Push a snapshot of local main without force. Failure never rolls back a landing."""
    sha = git.out(repo, 'rev-parse', '--verify', f'refs/heads/{main}^{{commit}}', timeout_s=LONG, error=sync_error).strip()
    tracking = f'refs/remotes/{remote}/{main}'
    previous = git.ask(repo, 'rev-parse', '--verify', tracking, timeout_s=LONG, no=(128,))
    old = previous.out.strip() if previous.answer is Answer.YES else '0' * 40     # unknown too: the CAS below then fails
    if guard is not None:
        guard()
    if not git.mutate(repo, 'push', remote, f'{sha}:refs/heads/{main}', timeout_s=FETCH).ok:
        raise SyncError(f'push failed: {remote}/{main}; local landing is retained')
    # Push may not update tracking when remote.fetch excludes main. Preserve concurrent fetches.
    git.mutate(repo, 'update-ref', tracking, sha, old, timeout_s=LONG)
