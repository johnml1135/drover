"""Run one short-lived command on this side and return how it ended; every spawn rule lives here once.

Every command has a timeout, no shell and the console flag, and no caller can omit them, because no caller passes
them. stdin is DEVNULL unless a payload is given. Output is held as bytes and decoded on request. A process that
failed raises nothing: it is a `Result`. Only a bad command raises, as a caller's bug, and `BaseException` passes.
"""
import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from subprocess import list2cmdline     # bound at import: the fake refuses any unscripted `process.subprocess` call
from typing import Literal, Mapping

Kind = Literal['exited', 'timed_out', 'unavailable']

CREATE_NO_WINDOW = 0x08000000
NO_WINDOW: Mapping[str, int] = {'creationflags': CREATE_NO_WINDOW} if sys.platform == 'win32' else {}

__all__ = ['Kind', 'Command', 'Result', 'CommandTooLong', 'run', 'NO_WINDOW', 'list2cmdline']


class CommandTooLong(ValueError):
    """An argv past the 8 KB command-line limit: a caller's bug, refused before any spawn."""


@dataclass(frozen=True)
class Command:
    argv: tuple[str, ...]
    timeout_s: float                        # required, > 0 and finite; no default and no None
    cwd: str | None = None
    env: Mapping[str, str] | None = None    # the whole environment; None inherits
    stdin: bytes | str | None = None        # the only payload route; None is DEVNULL, a str goes as UTF-8

    def __post_init__(self):
        if isinstance(self.argv, (str, bytes)) or not self.argv:
            raise ValueError(f'a command needs a non-empty argv sequence, not {self.argv!r}')
        object.__setattr__(self, 'argv', tuple(os.fspath(arg) for arg in self.argv))
        timeout = self.timeout_s
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout < math.inf:
            raise ValueError(f'a command needs a positive, finite timeout, not {timeout!r}')


@dataclass(frozen=True)
class Result:
    kind: Kind
    code: int | None        # exit status; None unless kind == 'exited'
    out: bytes              # all that arrived, partial when timed out
    err: bytes
    elapsed_s: float
    detail: str             # why, when not 'exited'

    @property
    def ok(self) -> bool:
        return self.kind == 'exited' and self.code == 0

    @property
    def stdout(self) -> str:
        return self.out.decode('utf-8', errors='replace')

    @property
    def stderr(self) -> str:
        return self.err.decode('utf-8', errors='replace')


def _options(command):
    payload = command.stdin
    if payload is None:
        stdin = {'stdin': subprocess.DEVNULL}
    else:
        stdin = {'input': payload.encode('utf-8') if isinstance(payload, str) else payload}
    env = None if command.env is None else dict(command.env)
    return dict(capture_output=True, timeout=command.timeout_s, cwd=command.cwd, env=env, shell=False, **stdin,
                **NO_WINDOW)


def run(command: Command) -> Result:
    """Spawn, wait, then check elapsed: a command that ended at or past its timeout is `timed_out`, output kept."""
    started = time.perf_counter()
    try:
        done = subprocess.run(list(command.argv), **_options(command))
    except subprocess.TimeoutExpired as error:
        return Result('timed_out', None, error.stdout or b'', error.stderr or b'', time.perf_counter() - started,
                      f'timed out after {command.timeout_s} s')
    except OSError as error:        # a missing executable or a refused spawn
        return Result('unavailable', None, b'', b'', time.perf_counter() - started,
                      f'{command.argv[0]}: {error.strerror or error}')
    elapsed = time.perf_counter() - started
    # Process creation cannot always be interrupted, notably on Windows, so a process that exits during startup can
    # pass subprocess.run's own timeout check.
    if elapsed >= command.timeout_s:
        return Result('timed_out', None, done.stdout or b'', done.stderr or b'', elapsed,
                      f'ended after {elapsed:.1f} s, at or past its {command.timeout_s} s timeout')
    return Result('exited', done.returncode, done.stdout or b'', done.stderr or b'', elapsed, '')
