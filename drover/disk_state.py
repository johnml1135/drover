"""Durable disk facts, cross-kernel directory locks and restoration evidence.

Locks never expire by age. A lock is reclaimed only when its holder is proven dead: same host, same side
(windows/wsl) and the recorded pid is absent or now has a different birth time (pid reuse). Anything unprovable
(other side or host, old or unreadable owner.json) fails closed and needs operator inspection.
State locks cover only file IO; callers perform subprocess work outside them.
"""
import json
import os
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path


def holder_dead(owner: dict) -> bool:
    """True only when the recorded holder is provably gone; every doubt answers False."""
    from . import placement
    birth = owner.get('birth')
    if (owner.get('host') != socket.gethostname() or owner.get('side') != placement.cleanup_side()
            or not birth or birth == 'absent'):
        return False
    try:
        return placement.cleanup_claim_alive(dict(pid=owner.get('pid'), birth=birth, side=owner['side'])) is False
    except (OSError, subprocess.SubprocessError):
        return False


_HOOK = None  # tests inject interleavings here


def _step(name):
    if _HOOK is not None:
        _HOOK(name)


def guard_path(path: Path) -> Path:
    return path.with_name(path.name + '.reclaim')


def lock_blocker(state_dir: Path):
    """The owner record of a cleanup lock that currently blocks a new run, else None.

    A missing lock, or a holder proven dead (the next acquire reclaims it), does not block. An unreadable owner
    reports an empty record so callers still see that something holds the lock."""
    path = Path(state_dir) / 'disk-cleanup.lock'
    stuck = guard_path(path).exists()  # a reclaim guard is never cleared automatically: surface it
    if not path.exists():
        return {'reclaim_guard': str(guard_path(path))} if stuck else None
    try:
        owner = json.loads((path / 'owner.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        owner = {}
    if not isinstance(owner, dict):
        owner = {}
    if stuck:
        return {**owner, 'reclaim_guard': str(guard_path(path))}
    return None if owner and holder_dead(owner) else owner


class CleanupLock:
    """Nonblocking machine-wide lock shared by native and WSL callers."""

    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / 'disk-cleanup.lock'
        self.token = uuid.uuid4().hex
        self.acquired = False

    def _owner(self):
        try:
            owner = json.loads((self.path / 'owner.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None
        return owner if isinstance(owner, dict) else None

    def _write_owner(self):
        from . import placement
        try:
            identity = placement.cleanup_identity()
        except (OSError, subprocess.SubprocessError):
            identity = dict(birth=None, side=placement.cleanup_side())
        (self.path / 'owner.json').write_text(json.dumps({
            'token': self.token, 'pid': os.getpid(), 'host': socket.gethostname(),
            'created': time.time(), 'side': identity.get('side'), 'birth': identity.get('birth')}),
            encoding='utf-8')

    def _reclaim(self, stale):
        """Replace a provably dead holder. Only the holder of the `.reclaim` guard may move the lock, so two
        reclaimers can never both act; an existing guard fails closed and is never reclaimed automatically."""
        guard = guard_path(self.path)
        _step('before_guard')
        try:
            guard.mkdir()
        except FileExistsError:
            raise self._busy() from None
        try:
            _step('after_guard')
            current = self._owner()
            if current is None or current.get('token') != stale.get('token') or not holder_dead(current):
                raise self._busy()
            tomb = self.path.with_name(f'{self.path.name}.stale-{self.token}')
            try:
                os.rename(self.path, tomb)  # never restored: a tombstone is only ever the dead holder
            except OSError:
                raise self._busy() from None
            _step('after_rename')
            try:
                (tomb / 'owner.json').unlink(missing_ok=True)
                tomb.rmdir()
            except OSError:
                pass
            try:
                self.path.mkdir()  # a fresh acquirer may have won the gap; then this lock is theirs
            except FileExistsError:
                raise self._busy() from None
            self.acquired = True
            self._write_owner()
        except BaseException:
            if self.acquired:
                (self.path / 'owner.json').unlink(missing_ok=True)
                self.path.rmdir()
                self.acquired = False
            raise
        finally:
            guard.rmdir()

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.mkdir()
        except FileExistsError:
            stale = self._owner()
            if stale is None or not holder_dead(stale):
                raise self._busy() from None
            self._reclaim(stale)
            return self
        self.acquired = True
        try:
            self._write_owner()
        except BaseException:
            (self.path / 'owner.json').unlink(missing_ok=True)
            self.path.rmdir()
            self.acquired = False
            raise
        return self

    def _busy(self):
        try:
            holder = (self.path / 'owner.json').read_text(encoding='utf-8')
        except OSError:
            holder = 'holder diagnostics unavailable'
        return BlockingIOError(f'cleanup lock busy: {self.path}: {holder}')

    def __exit__(self, *exc):
        if exc and getattr(exc[1], 'termination_confirmed', True) is False:
            return  # Unconfirmed worker loss requires operator inspection; never unlock.
        if self.acquired:
            owner = self.path / 'owner.json'
            # Never unlink a replacement holder's directory.
            if json.loads(owner.read_text(encoding='utf-8')).get('token') != self.token:
                raise RuntimeError(f'cleanup lock ownership changed: {self.path}')
            owner.unlink()
            self.path.rmdir()
            self.acquired = False


class _StateLock(CleanupLock):
    def __init__(self, state_dir):
        super().__init__(state_dir)
        self.path = Path(state_dir) / 'disk' / 'state.lock'


def _read(state_dir):
    path = Path(state_dir) / 'disk' / 'state.json'
    try:
        text = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return {'v': 1}
    try:
        state = json.loads(text)
    except (ValueError, UnicodeError) as error:
        raise ValueError(f'corrupt disk state: {path}') from error
    if not isinstance(state, dict) or type(state.get('v')) is not int or state['v'] != 1:
        raise ValueError(f'unsupported disk state version: {path}')
    return state


def read(state_dir: Path) -> dict:
    with _StateLock(state_dir):
        return _read(state_dir)


def _write(state_dir, state):
    if not isinstance(state, dict) or type(state.get('v')) is not int or state['v'] != 1:
        raise ValueError('disk state requires version 1')
    payload = json.dumps(state, allow_nan=False)
    directory = Path(state_dir) / 'disk'
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(directory / 'state.json')
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write(state_dir: Path, state: dict) -> None:
    with _StateLock(state_dir):
        _read(state_dir)  # Refuse to overwrite corrupt/unknown state.
        _write(state_dir, state)


def append_manifest(state_dir: Path, record: dict) -> None:
    """Append durable intent/completion; a failed fsync propagates before mutation."""
    if record.get('v') != 1 or record.get('phase') not in ('planned', 'completed', 'failed'):
        raise ValueError('manifest requires version 1 and a journal phase')
    payload = json.dumps(record, allow_nan=False) + '\n'
    with _StateLock(state_dir):
        with (Path(state_dir) / 'disk-cleanup-manifest.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())


def update(state_dir, change):
    """Apply a short state-only transaction; never run subprocesses in change."""
    with _StateLock(state_dir):
        state = _read(state_dir)
        answer = change(state)
        _write(state_dir, state)
        return answer


def save_file(state_dir, name, value):
    """Independent durable fact (critical token or result), even if state.lock is busy."""
    directory = Path(state_dir) / 'disk'
    directory.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(directory / name)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
