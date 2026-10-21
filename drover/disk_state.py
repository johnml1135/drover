"""Durable disk facts, cross-kernel directory locks and restoration evidence.

Locks never expire automatically. A dead holder requires operator inspection.
State locks cover only file IO; callers perform subprocess work outside them.
"""
import json
import os
import socket
import tempfile
import time
import uuid
from pathlib import Path


class CleanupLock:
    """Nonblocking machine-wide lock shared by native and WSL callers."""

    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / 'disk-cleanup.lock'
        self.token = uuid.uuid4().hex
        self.acquired = False

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.mkdir()
        except FileExistsError:
            try:
                holder = (self.path / 'owner.json').read_text(encoding='utf-8')
            except OSError:
                holder = 'holder diagnostics unavailable'
            raise BlockingIOError(f'cleanup lock busy: {self.path}: {holder}') from None
        self.acquired = True
        try:
            (self.path / 'owner.json').write_text(json.dumps({
                'token': self.token, 'pid': os.getpid(), 'host': socket.gethostname(),
                'created': time.time()}), encoding='utf-8')
        except BaseException:
            (self.path / 'owner.json').unlink(missing_ok=True)
            self.path.rmdir()
            self.acquired = False
            raise
        return self

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
