"""Isolated read checkouts and fail-closed end-of-run verification.

Never switch the owner's checkout. Key snapshots by repository, ref and commit;
a moving branch gets a new snapshot. Each clone retains history independently. These are read inputs, never task worktrees to land.
Each prepare keeps the newest KEEP snapshots per (source, branch), the pinned ones and its own, and removes the rest.
"""
from collections.abc import Iterable
import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile
import threading
import time

from . import config, durable, git_client, placement, repos, task_context

_LOCK = threading.Lock()      # in-process only: one dispatcher per state dir (dispatcher.lock) is what makes it enough
KEEP = 2                                     # snapshots kept per (source, branch), newest by last use
INDEX = '.index.json'                        # key -> {source, branch, sha, used, seq}; outside every checkout
_KEY = re.compile(r'[0-9a-f]{24}')
TEMP_AGE_S = 3600                            # an unfinished `read-*` clone this old is left over
ADOPTED_S = 86400                            # a snapshot found beside an unreadable index is kept this long
_log = logging.getLogger(__name__)


def check_read_roots(cwd, paths):
    """Reject inputs covered by the Codex worker's writable workspace root."""
    writable = placement.path_parts(cwd)
    for path in paths:
        read = placement.path_parts(path)
        if read[:len(writable)] == writable or writable[:len(read)] == read:
            raise ValueError(f'read-only checkout inside or containing writable workspace: {path}')


def _refused(failed):
    """Any failure, a timeout or no git included, is the ValueError `prepare` falls through on and `verify` refuses on."""
    return ValueError(f'read checkout: {(failed.err.strip() or str(failed))[-250:]}')


def _out(path, *args):
    return git_client.out(path, '--no-optional-locks', *args, timeout_s=git_client.NORMAL, error=_refused).strip()


def verify(reads):
    """One status call carries both the commit (branch.oid header) and any change, including ignored files."""
    for read in reads:
        path = read['path']
        lines = _out(path, 'status', '--porcelain=v2', '--branch', '--untracked-files=all',
                     '--ignored=matching').splitlines()
        head = next((line.split(' ', 2)[2] for line in lines if line.startswith('# branch.oid ')), None)
        if head != read['sha'] or any(not line.startswith('# ') for line in lines):
            raise ValueError(f'read-only checkout changed: {path}')


def _record(value):
    """An index record whose `seq`, `sha` and `adopted` have their types; any other is adopted, not a failure."""
    return isinstance(value, dict) and type(value.get('seq', 0)) is int and isinstance(value.get('sha', ''), str) \
        and type(value.get('adopted', 0)) in (int, float)


def _load(root):
    """(index, unreadable). A missing index is empty: what is in the root is then unindexed, and pruned. An index
    that exists but cannot be read is `unreadable`: a crash cut it short, so nothing is known about what is there."""
    try:
        data = json.loads((root / INDEX).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}, False
    except (OSError, ValueError):
        return {}, True
    if not isinstance(data, dict):
        return {}, True
    # A malformed record says as little as an unreadable index: its folder is adopted, kept for ADOPTED_S.
    return {k: v if _record(v) else {'adopted': time.time(), 'seq': 0} for k, v in data.items() if _KEY.fullmatch(k)}, \
        False


def _adopt(root, index):
    """Beside an unreadable index, record each snapshot folder it no longer names as adopted now, so it is kept for
    ADOPTED_S: it may be an enrolled batch's that no manifest pins yet."""
    now = time.time()
    with os.scandir(root) as entries:
        for entry in entries:
            if _KEY.fullmatch(entry.name) and entry.name not in index and entry.is_dir(follow_symlinks=False):
                index[entry.name] = {'adopted': now, 'seq': 0}


def _newest(index, root, source, branch):
    found = [(rec.get('seq', 0), key) for key, rec in index.items()
             if rec.get('source') == source and rec.get('branch') == branch and rec.get('sha') and (root / key).is_dir()]
    return max(found)[1] if found else None


def _touch(index, key, **rec):
    seq = max((r.get('seq', 0) for r in index.values()), default=0) + 1
    index[key] = {**index.get(key, {}), **rec, 'used': time.time(), 'seq': seq}


def _remove(path):
    """Remove one tree; a read-only file (the old chmod) has its attribute cleared and is retried. Never raises."""
    def retry(func, target, _exc):
        if not os.path.islink(target):          # a link's target may be outside the root
            os.chmod(target, stat.S_IWRITE)
        func(target)
    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=retry)
        else:
            shutil.rmtree(path, onerror=retry)
    except OSError as exc:
        _log.warning('read checkout %s was not removed: %s', path, exc)


def _prune(root, index, keep):
    """Remove every directory in the root but the keep set and the newest KEEP per (source, branch). Links are skipped."""
    groups = {}
    for key, rec in index.items():
        if (root / key).is_dir():
            groups.setdefault((rec.get('source'), rec.get('branch')), []).append((rec.get('seq', 0), key))
    keep = set(keep)
    for (source, branch), ranked in groups.items():
        if source is not None and branch is not None:
            keep.update(key for _, key in sorted(ranked, reverse=True)[:KEEP])
    now = time.time()
    keep.update(key for key, rec in index.items() if now - rec.get('adopted', -ADOPTED_S) < ADOPTED_S)
    with os.scandir(root) as entries:
        listed = list(entries)
    for entry in listed:
        if entry.is_symlink() or (sys.version_info >= (3, 12) and os.path.isjunction(entry.path)):
            continue
        if not entry.is_dir(follow_symlinks=False):
            continue
        if entry.name.startswith('read-'):
            stale = now - entry.stat(follow_symlinks=False).st_mtime > TEMP_AGE_S
        else:
            stale = entry.name not in keep
        if stale:
            _remove(entry.path)
            index.pop(entry.name, None)
    for key in [k for k in index if not (root / k).is_dir()]:
        del index[key]


def prepare(entries, configured=None, root=None, *, pinned: Iterable[str] = (), reuse=False):
    """Read checkouts at each entry's branch tip. `pinned` paths (open runs' manifests) are never pruned.
    `reuse` takes the newest existing snapshot of the branch when there is one, so inspection makes no new clone."""
    configured = repos.load_all() if configured is None else configured
    root = Path(root or config.state_path('read-checkouts')).resolve()
    out = []
    with _LOCK:
        index, unreadable = _load(root)
        for entry in entries:
            name, branch = task_context.split_read(entry)
            prog = configured.get(name.lower())
            if prog is None:
                raise ValueError(f'unknown read repo {name!r}')
            source = repos.native(repos.repo_path(prog))
            branch = branch or repos.default_branch(prog)
            if reuse and (key := _newest(index, root, source, branch)):
                read = dict(entry=entry, path=str(root / key), sha=index[key]['sha'])
                try:
                    verify([read])
                except ValueError:
                    pass                                 # changed or unreadable: take a fresh snapshot below
                else:
                    _touch(index, key)
                    out.append(read)
                    continue
            sha = None
            # Only named branches, never revision syntax or an arbitrary path.
            for ref in (f'refs/heads/{branch}', f'refs/remotes/{repos.remote_name(prog)}/{branch}'):
                try:
                    sha = _out(source, 'rev-parse', '--verify', ref + '^{commit}')
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
                    _out(source, 'clone', '--no-checkout', '--', url, clone_dest)
                    if via_wsl:
                        _out(source, '-C', clone_dest, 'fetch', '--no-tags', '--', url, sha)
                    else:
                        _out(temp, 'fetch', '--no-tags', '--', url, sha)
                    _out(temp, 'config', 'core.filemode', 'false')
                    _out(temp, '-c', 'core.autocrlf=false', 'checkout', '--detach', sha)
                    temp.rename(dest)
                except Exception:
                    shutil.rmtree(temp)
                    raise
            verify([read])
            _touch(index, key, source=source, branch=branch, sha=sha)
            out.append(read)
        if out:
            # Kept by name: a pinned path may spell the root differently from this call.
            keep = {Path(r['path']).name for r in out} | {Path(str(p)).name for p in pinned}
            try:
                if unreadable:      # nothing is known of what is here: prune nothing, and keep what is found a while
                    _log.warning('read checkout index %s is unreadable: nothing pruned, its snapshots kept for %d s',
                                 root / INDEX, ADOPTED_S)
                    _adopt(root, index)
                else:
                    _prune(root, index, keep)
                durable.write_json(root / INDEX, index, sync=True)     # a torn index is what `unreadable` guards
            except Exception as exc:                     # bounding the folder must never fail a prepare
                _log.warning('read checkouts under %s were not pruned: %s', root, exc)
    return out
