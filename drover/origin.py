"""Sync local main with its remote without writing to the owner's checkout."""
import json
import subprocess

FETCH_TIMEOUT_S = 120
PAUSE_KEY = '_origin_pause_'


class SyncError(ValueError):
    pass


class FetchFailed(SyncError):
    pass


def git(repo, *args, check=True, timeout=None):
    try:
        r = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True,
                           errors='replace', timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        r = subprocess.CompletedProcess(args, 124, '', '')
    if check and r.returncode:
        # Git's stderr can contain a credential-bearing remote URL.
        raise SyncError(f'git {args[0]} failed')
    return r


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
    if run(repo, 'update-ref', ref, upstream, local, check=False).returncode:
        raise SyncError(f'{main} moved during origin sync; retry')
    return upstream


def ahead(repo, remote='origin', main='main'):
    """Local commits absent from the last fetched remote main; no network during rendering."""
    r = git(repo, 'rev-list', '--count', f'refs/remotes/{remote}/{main}..refs/heads/{main}', check=False)
    return int(r.stdout.strip()) if r.returncode == 0 else 0


def push_main(repo, remote='origin', main='main'):
    """Push a snapshot of local main without force. Failure never rolls back a landing."""
    sha = git(repo, 'rev-parse', '--verify', f'refs/heads/{main}^{{commit}}').stdout.strip()
    tracking = f'refs/remotes/{remote}/{main}'
    previous = git(repo, 'rev-parse', '--verify', tracking, check=False)
    old = previous.stdout.strip() if previous.returncode == 0 else '0' * 40
    if git(repo, 'push', remote, f'{sha}:refs/heads/{main}', check=False, timeout=FETCH_TIMEOUT_S).returncode:
        raise SyncError(f'push failed: {remote}/{main}; local landing is retained')
    # Push may not update tracking when remote.fetch excludes main. Preserve concurrent fetches.
    git(repo, 'update-ref', tracking, sha, old, check=False)


def paused(port, name):
    raw = port.get_setting(PAUSE_KEY + name.lower())
    if not raw:
        return False
    state = json.loads(raw)
    if port.get_question(state['question'])['status'] == 'open':
        return True
    port.set_setting(PAUSE_KEY + name.lower(), '')
    return False


def pause(port, name, target_kind, target_id, reason):
    """Pause only this repo and ask once until the owner acknowledges the alert."""
    if paused(port, name):
        return
    qid = port.ask_question(target_kind, target_id, f'Origin sync paused: {name}', body=str(reason),
                            options=[{'label': 'Retry origin sync', 'recommended': True}])
    port.set_setting(PAUSE_KEY + name.lower(), json.dumps({'question': qid}))
    port.append_event('origin-sync-failed', {'repo': name, 'reason': str(reason)})
