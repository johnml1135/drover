"""Isolated read checkouts and fail-closed end-of-run verification.

Never switch the owner's checkout. Key snapshots by repository, ref and commit;
a moving branch gets a new snapshot. Each clone retains history independently. These are read inputs, never task worktrees to land.
"""
import hashlib
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import threading

from . import config, placement, repos, task_context

_LOCK = threading.Lock()


def check_read_roots(cwd, paths):
    """Reject inputs covered by the Codex worker's writable workspace root."""
    writable = placement.path_parts(cwd)
    for path in paths:
        read = placement.path_parts(path)
        if read[:len(writable)] == writable or writable[:len(read)] == read:
            raise ValueError(f'read-only checkout inside or containing writable workspace: {path}')


def _git(path, *args):
    r = placement.git(path, '--no-optional-locks', *args, timeout=60)
    if r.returncode:
        raise ValueError(f'read checkout: {r.stderr.strip()[-250:]}')
    return r.stdout.strip()


def verify(reads):
    for read in reads:
        path = read['path']
        if _git(path, 'rev-parse', 'HEAD') != read['sha'] or _git(
                path, 'status', '--porcelain', '--untracked-files=all', '--ignored=matching'):
            raise ValueError(f'read-only checkout changed: {path}')


def prepare(entries, configured=None, root=None):
    configured = repos.load_all() if configured is None else configured
    root = Path(root or config.state_path('read-checkouts')).resolve()
    out = []
    with _LOCK:
        for entry in entries:
            name, branch = task_context.split_read(entry)
            prog = configured.get(name.lower())
            if prog is None:
                raise ValueError(f'unknown read repo {name!r}')
            source = repos.native(prog['repo'])
            branch = branch or prog['default_branch']
            sha = None
            # Only named branches, never revision syntax or an arbitrary path.
            for ref in (f'refs/heads/{branch}', f'refs/remotes/{prog.get("remote", "origin")}/{branch}'):
                try:
                    sha = _git(source, 'rev-parse', '--verify', ref + '^{commit}')
                    break
                except ValueError:
                    pass
            if not sha:
                raise ValueError(f'unknown read branch {name}@{branch}')
            key = hashlib.sha256(f'history\0{source}\0{branch}\0{sha}'.encode()).hexdigest()[:24]
            dest = root / key
            read = dict(entry=entry, path=str(dest), sha=sha)
            if not dest.exists():
                root.mkdir(parents=True, exist_ok=True)
                temp = Path(tempfile.mkdtemp(prefix='read-', dir=root))
                try:
                    via_wsl = placement.routes_through_wsl(source)
                    url = (PurePosixPath(source).as_uri() if via_wsl else Path(source).resolve().as_uri())
                    clone_dest = placement.to_wsl_path(temp) if via_wsl else str(temp)
                    _git(source, 'clone', '--no-checkout', '--', url, clone_dest)
                    if via_wsl:
                        _git(source, '-C', clone_dest, 'fetch', '--no-tags', '--', url, sha)
                    else:
                        _git(temp, 'fetch', '--no-tags', '--', url, sha)
                    _git(temp, 'config', 'core.filemode', 'false')
                    _git(temp, '-c', 'core.autocrlf=false', 'checkout', '--detach', sha)
                    temp.rename(dest)
                except Exception:
                    shutil.rmtree(temp)
                    raise
            verify([read])
            out.append(read)
    return out
