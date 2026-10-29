"""Sync local main with its remote and keep its checkout consistent."""
import subprocess
from pathlib import Path
from . import placement
FETCH_TIMEOUT_S = 120
class SyncError(ValueError):
    pass
class FetchFailed(SyncError):
    pass
class DirtyCheckout(SyncError):
    pass
class MainMoved(SyncError):
    pass
def git(repo, *args, check=True, timeout=None):
    try:
        r = placement.git(repo, *args, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        r = subprocess.CompletedProcess(args, 124, '', '')
    if check and r.returncode:
        # Git's stderr can contain a credential-bearing remote URL.
        raise SyncError(f'git {args[0]} failed')
    return r

def fast_forward(repo, main, new, old, *, run=git):
    """Advance a branch through its clean checkout, or CAS its ref when unchecked out."""
    ref = f'refs/heads/{main}'
    listing = run(repo, 'worktree', 'list', '--porcelain', '-z').stdout
    checkouts = []
    for record in listing.split('\0\0'):
        fields = record.split('\0')
        if f'branch {ref}' in fields:
            checkouts.append(Path(fields[0].removeprefix('worktree ')))
    if not checkouts:
        if run(repo, 'update-ref', ref, new, old, check=False).returncode:
            raise MainMoved(f'{main} moved during fast-forward; retry')
        return
    if len(checkouts) != 1:
        raise SyncError(f'{main} is checked out in multiple worktrees; owner intervention required')
    checkout = checkouts[0]
    if run(checkout, 'status', '--porcelain', '--untracked-files=normal').stdout.strip():
        raise DirtyCheckout(f'{main} checkout dirty; owner intervention required')
    head = run(checkout, 'rev-parse', 'HEAD').stdout.strip()
    branch = run(checkout, 'symbolic-ref', '-q', 'HEAD', check=False).stdout.strip()
    if head != old or branch != ref:
        raise MainMoved(f'{main} moved during fast-forward; retry')
    if run(checkout, 'merge', '--ff-only', '--no-autostash', new, check=False).returncode:
        raise MainMoved(f'{main} moved during fast-forward; retry')


def sync(repo, remote='origin', main='main', *, run=git, timeout=FETCH_TIMEOUT_S):
    """Fetch and advance main with compare-and-swap; preserve local unpushed landings."""
    fetched = run(repo, 'fetch', remote, main, f'--refmap=+refs/heads/{main}:refs/remotes/{remote}/{main}',
                  check=False, timeout=timeout)
    if fetched.returncode:
        raise FetchFailed(f'fetch failed: {remote}/{main}; repo paused until owner retries')
    upstream = run(repo, 'rev-parse', '--verify', f'refs/remotes/{remote}/{main}^{{commit}}', check=True).stdout.strip()
    ref = f'refs/heads/{main}'
    local = run(repo, 'rev-parse', '--verify', ref, check=True).stdout.strip()
    if run(repo, 'merge-base', '--is-ancestor', upstream, local, check=False).returncode == 0:
        return local
    if run(repo, 'merge-base', '--is-ancestor', local, upstream, check=False).returncode:
        raise SyncError(f'{main} diverged from {remote}')
    try:
        fast_forward(repo, main, upstream, local, run=run)
    except MainMoved as e:
        raise SyncError(f'{main} moved during origin sync; retry') from e
    return upstream


def observe(repo, remote, main, stopped, moved, tracking, interval):
    """Poll remote main without changing local refs or a checkout."""
    while not stopped.is_set():
        result = git(repo, 'ls-remote', '--heads', remote, f'refs/heads/{main}', check=False, timeout=FETCH_TIMEOUT_S)
        fields = result.stdout.split()
        if (result.returncode == 0 and len(fields) == 2 and fields[1] == f'refs/heads/{main}'
                and tracking and fields[0] != tracking):
            moved.set()
            return
        if stopped.wait(interval):
            return


def ahead(repo, remote='origin', main='main', *, timeout=None):
    """Local commits absent from the last fetched remote main; no network during rendering."""
    r = git(repo, 'rev-list', '--count', f'refs/remotes/{remote}/{main}..refs/heads/{main}', check=False, timeout=timeout)
    return int(r.stdout.strip()) if r.returncode == 0 else (None if r.returncode == 124 else 0)


def push_main(repo, remote='origin', main='main', *, guard=None):
    """Push a snapshot of local main without force. Failure never rolls back a landing."""
    sha = git(repo, 'rev-parse', '--verify', f'refs/heads/{main}^{{commit}}').stdout.strip()
    tracking = f'refs/remotes/{remote}/{main}'
    previous = git(repo, 'rev-parse', '--verify', tracking, check=False)
    old = previous.stdout.strip() if previous.returncode == 0 else '0' * 40
    if guard is not None:
        guard()
    if git(repo, 'push', remote, f'{sha}:refs/heads/{main}', check=False, timeout=FETCH_TIMEOUT_S).returncode:
        raise SyncError(f'push failed: {remote}/{main}; local landing is retained')
    # Push may not update tracking when remote.fetch excludes main. Preserve concurrent fetches.
    git(repo, 'update-ref', tracking, sha, old, check=False)
