"""Isolated, immutable read checkouts and fail-closed end-of-run verification.

Never switch the owner's checkout. Cache by repository, ref and commit; a moving
branch gets a new snapshot. These are read inputs, never task worktrees to land.
"""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading

from . import config, repos, task_context
from .winproc import NO_WINDOW

_LOCK = threading.Lock()


def check_read_roots(cwd, paths):
    """Reject inputs covered by the Codex worker's writable workspace root."""
    workspace = Path(repos.native(cwd)).resolve()

    def parts(path):
        values = path.parts
        # Windows drive mounts can be case insensitive even when the hub is
        # running in WSL. Resolve symlinks first, then compare conservatively.
        if os.name == 'nt' or (len(values) > 2 and values[1] == 'mnt' and len(values[2]) == 1):
            return tuple(p.casefold() for p in values)
        return values

    writable = parts(workspace)
    for path in paths:
        read = parts(Path(repos.native(path)).resolve())
        if read[:len(writable)] == writable or writable[:len(read)] == read:
            raise ValueError(f'read-only checkout inside or containing writable workspace: {path}')


def _git(path, *args):
    r = subprocess.run(['git', '--no-optional-locks', '-C', str(path), *args],
                       capture_output=True, text=True, timeout=60, **NO_WINDOW)
    if r.returncode:
        raise ValueError(f'read checkout: {r.stderr.strip()[-250:]}')
    return r.stdout.strip()


def make_writable(path):
    """For removing a snapshot we created (also used by temporary-directory tests)."""
    path = Path(path)
    if not path.exists():
        return
    for root, dirs, files in os.walk(path):
        Path(root).chmod(0o700)
        for name in files:
            p = Path(root) / name
            if not p.is_symlink():
                p.chmod(0o600)


def _readonly(path):
    # Defense in depth only; Codex's filesystem sandbox enforces read access.
    for root, dirs, files in os.walk(path, topdown=False):
        for name in files:
            p = Path(root) / name
            if not p.is_symlink():
                p.chmod(p.stat().st_mode & ~0o222)
        Path(root).chmod(0o555)


def verify(reads):
    for read in reads:
        path = read['path']
        if _git(path, 'rev-parse', 'HEAD') != read['sha'] or _git(
                path, 'status', '--porcelain', '--untracked-files=all', '--ignored=matching'):
            raise ValueError(f'read-only checkout changed: {path}')


def prepare(entries, configured=None, root=None):
    configured = repos.load_all() if configured is None else configured
    root = Path(root or config.state_path('read-checkouts'))
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
            key = hashlib.sha256(f'{source}\0{branch}\0{sha}'.encode()).hexdigest()[:24]
            dest = root / key
            read = dict(entry=entry, path=str(dest), sha=sha)
            if not dest.exists():
                root.mkdir(parents=True, exist_ok=True)
                temp = Path(tempfile.mkdtemp(prefix='read-', dir=root))
                try:
                    _git(temp, 'init', '--quiet')
                    _git(temp, 'config', 'core.filemode', 'false')
                    _git(temp, 'fetch', '--depth=1', '--no-tags', '--', source, sha)
                    _git(temp, '-c', 'core.autocrlf=false', 'checkout', '--detach', sha)
                    _readonly(temp)
                    temp.rename(dest)
                except Exception:
                    make_writable(temp)
                    shutil.rmtree(temp)
                    raise
            verify([read])
            out.append(read)
    return out
