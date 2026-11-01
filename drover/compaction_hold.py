"""The compaction hold: files in the state directory that any process can read.

The job runs in its own elevated task and ADR-0001 keeps it away from the ledger, so the hold cannot be a setting.
A hold is `disk/compaction-active.json`, written by the job. While it is live, WSL work is held and nothing on a timer
calls wsl.exe. The phase says how far the job has got: `draining` still lets the hub finish WSL runs, and `offline`
(from just before `wsl --shutdown`) refuses every wsl.exe command the hub builds. A hold older than MAX_AGE_S is
ignored, so a crashed job cannot hold work forever. `compaction-drain.json` is the hub's answer to the job about its
runs, and `compaction-panes.json` about agents in WSL panes. `resuming` follows WSL's restart, for those agents.

This module sits below placement: it imports nothing from Drover at load time.
"""
import json
import os
import time
import uuid
from pathlib import Path

HOLD_FILE = 'compaction-active.json'
DRAIN_FILE = 'compaction-drain.json'
MOUNT_FILE = 'compaction-mounted.json'
REPORT_FILE = 'compaction-report.json'
PANE_FILE = 'compaction-panes.json'
MAX_AGE_S = 2 * 3600
WIND_DOWN_S = 300   # headless WSL workers get this long from the start of the hold, then are killed and requeued
ASK_S = 300         # agents in WSL panes get this long from the start of the hold to act on "stop now"
INTERRUPT_S = 60    # and this long after their interruption, then the job's kill
RESUME_S = 180      # from the `resume` step: every agent is back by then, or is a failed resume
RETRIES, RETRY_S = 10, 0.05

clock = time.time


class WslOffline(OSError):
    """WSL is down for compaction. An OSError, so callers treat it as WSL being unavailable."""


def _file(name, state_dir=None):
    if state_dir is None:
        from . import config
        state_dir = config.state_dir()
    return Path(state_dir) / 'disk' / name


def _read(name, state_dir=None):
    try:
        value = json.loads(_file(name, state_dir).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _retried(action):
    """On Windows a file cannot be replaced or removed in the instant another process is reading it."""
    for _ in range(RETRIES - 1):
        try:
            return action()
        except PermissionError:
            time.sleep(RETRY_S)
    return action()


def _write(name, value, state_dir):
    from . import disk_state
    _retried(lambda: disk_state.save_file(state_dir, name, value))


def _remove(name, state_dir):
    _retried(lambda: _file(name, state_dir).unlink(missing_ok=True))


def read(state_dir=None):
    """The stored hold, live or not, else None."""
    return _read(HOLD_FILE, state_dir)


def live(record):
    return bool(record) and type(record.get('at')) in (int, float) and abs(clock() - record['at']) < MAX_AGE_S


def active(state_dir=None):
    """The hold record while it is live, else None."""
    record = read(state_dir)
    return record if live(record) else None


def refuse_when_offline():
    record = active()
    if record and record.get('phase') == 'offline':
        raise WslOffline('WSL is down for the overnight compaction')


def offline(state_dir=None):
    return (active(state_dir) or {}).get('phase') == 'offline'


def claim(state_dir, *, at, pid, birth, dead=lambda record: False):
    """Take the hold, or None while a live holder has it. A stale hold or a dead holder's is replaced."""
    path = _file(HOLD_FILE, state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    current = read(state_dir)
    if current is not None and live(current) and not dead(current):
        return None
    _remove(HOLD_FILE, state_dir)
    _remove(DRAIN_FILE, state_dir)
    _remove(PANE_FILE, state_dir)
    record = dict(id=uuid.uuid4().hex, since=at, at=at, pid=pid, birth=birth, step='hold', phase='draining', steps=['hold'])
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        stream.write(json.dumps(record))
    return record


def save(state_dir, record):
    _write(HOLD_FILE, record, state_dir)


def release(state_dir, token):
    """Drop the hold when `token` still names it."""
    if (read(state_dir) or {}).get('id') == token:
        _remove(HOLD_FILE, state_dir)
        _remove(DRAIN_FILE, state_dir)
        _remove(PANE_FILE, state_dir)


def save_drain(state_dir, answer):
    _write(DRAIN_FILE, answer, state_dir)


def read_drain(state_dir=None):
    return _read(DRAIN_FILE, state_dir)


def save_panes(state_dir, answer):
    _write(PANE_FILE, answer, state_dir)


def read_panes(state_dir=None):
    return _read(PANE_FILE, state_dir)


def note_mount(state_dir, vhdx):
    """Written before mounting: only an elevated run can dismount, so the next run has to know."""
    _write(MOUNT_FILE, dict(vhdx=vhdx), state_dir)


def mounted(state_dir):
    return (_read(MOUNT_FILE, state_dir) or {}).get('vhdx')


def clear_mount(state_dir):
    _remove(MOUNT_FILE, state_dir)
