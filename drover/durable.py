"""Publish, move and remove files so that a brief Windows reader cannot lose the write, with the wait stated by the caller.

Stdlib only: `compaction_hold`, the WSL cleanup worker and the runner all import it. `durable` makes no directory.
`private` keeps mkstemp's 0600 on POSIX, for a site that published it before `durable`; the default is the umask's mode.
"""
import contextlib
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

POSIX = os.name == 'posix'
_UMASK = os.umask(0)        # read once at load: setting it back per write would race every thread that creates a file
os.umask(_UMASK)


@dataclass(frozen=True)
class Wait:
    seconds: float
    step_s: float = 0.05


NO_WAIT, ROUTINE, ORDINARY, RELEASE, TERMINAL = Wait(0), Wait(0.5), Wait(2.0), Wait(5.0), Wait(30.0)


def _outlast(attempt, wait):
    """`attempt()`, repeated while a reader refuses it. The clock starts at the first refusal, so a call that lands reads none."""
    deadline = None
    while True:
        try:
            return attempt()
        except PermissionError:
            now = time.monotonic()
            deadline = now + wait.seconds if deadline is None else deadline
            if now >= deadline:
                raise
            time.sleep(wait.step_s)


def _discard(temporary):
    """The temp goes after any failure. The caller hears that failure, not this one."""
    with contextlib.suppress(OSError):
        remove(temporary, wait=ROUTINE)


def _land(path, content, mode, sync, wait, private, **text):
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.', suffix='.tmp')
    try:
        with os.fdopen(descriptor, mode, **text) as stream:
            stream.write(content)
            if sync:
                stream.flush()
                os.fsync(stream.fileno())
        if POSIX and not private:
            os.chmod(temporary, 0o666 & ~_UMASK)    # mkstemp's 0600 is the temp's, not the published file's
        _outlast(lambda: os.replace(temporary, path), wait)
    except BaseException:
        _discard(temporary)
        raise


def write_bytes(path, data: bytes, *, sync: bool, wait: Wait = ORDINARY, private: bool = False) -> None:
    _land(path, data, 'wb', sync, wait, private)


def write_text(path, text: str, *, sync: bool, wait: Wait = ORDINARY, newline: str | None = None, private: bool = False) -> None:
    _land(path, text, 'w', sync, wait, private, encoding='utf-8', newline=newline)


def write_json(path, value, *, sync: bool, wait: Wait = ORDINARY, indent: int | None = 1, allow_nan: bool = True,
               private: bool = False) -> None:
    write_text(path, json.dumps(value, indent=indent, allow_nan=allow_nan), sync=sync, wait=wait, private=private)


def move(source, target, *, wait: Wait = NO_WAIT) -> None:
    """A rename of a file this caller owns. No temp handling; retries only when wait says so."""
    _outlast(lambda: os.replace(source, target), wait)


def remove(path, *, wait: Wait = RELEASE, missing_ok: bool = True) -> None:
    try:
        _outlast(lambda: os.unlink(path), wait)
    except FileNotFoundError:
        if not missing_ok:
            raise


def rmdir(path, *, wait: Wait = RELEASE) -> None:
    _outlast(lambda: os.rmdir(path), wait)
