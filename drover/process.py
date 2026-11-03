"""Run one short-lived command on this side and return how it ended; every spawn rule lives here once.

Every command has a timeout, no shell and the console flag, and no caller can omit them, because no caller passes
them. stdin is DEVNULL unless a payload is given. Output is held as bytes and decoded on request. A process that
failed raises nothing: it is a `Result`. Only a bad command raises, as a caller's bug, and `BaseException` passes.

A timeout ends the whole tree, not only the direct child, and the run returns within a bounded time even when a
grandchild holds the output pipes (Git\\cmd\\git.exe starts mingw64's git.exe so). On Windows the child starts
suspended inside a job object that is killed if this process dies; on POSIX it runs in a new session, which it
leads, with no controlling terminal.
"""
import logging
import math
import os
import signal
import subprocess as _subprocess
import sys
import threading
import time
from dataclasses import dataclass
from subprocess import list2cmdline     # bound at import: the fake refuses any unscripted `process.subprocess` call
from typing import Literal, Mapping

Kind = Literal['exited', 'timed_out', 'unavailable']

_WINDOWS = sys.platform == 'win32'
CREATE_NO_WINDOW = 0x08000000
CREATE_SUSPENDED = 0x4
NO_WINDOW: Mapping[str, int] = {'creationflags': CREATE_NO_WINDOW} if _WINDOWS else {}
DRAIN_S = 2.0           # after the tree is ended, the most a run waits for its pipes to close
_log = logging.getLogger(__name__)
_warned = []

if _WINDOWS:
    import ctypes
    from ctypes import wintypes

    class _Basic(ctypes.Structure):
        _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
                    ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                    ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                    ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD),
                    ('SchedulingClass', wintypes.DWORD)]

    class _Extended(ctypes.Structure):
        _fields_ = [('BasicLimitInformation', _Basic), ('IoInfo', ctypes.c_uint64 * 6),
                    ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                    ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]

    _k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _ntdll = ctypes.WinDLL('ntdll')
    _ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    _ntdll.NtResumeProcess.restype = ctypes.c_long
    KILL_ON_JOB_CLOSE, BREAKAWAY_OK, EXTENDED_LIMIT_CLASS = 0x2000, 0x800, 9

    def _limit(job, flags):
        info = _Extended()
        info.BasicLimitInformation.LimitFlags = flags
        return _k32.SetInformationJobObject(job, EXTENDED_LIMIT_CLASS, ctypes.byref(info), ctypes.sizeof(info))


def _contain(child):
    """Windows: put the suspended child in a kill-on-close job, then resume it. None when no job can be had."""
    if not _WINDOWS:
        return None
    job = _k32.CreateJobObjectW(None, None)
    if job and not (_limit(job, KILL_ON_JOB_CLOSE | BREAKAWAY_OK)
                    and _k32.AssignProcessToJobObject(job, int(child._handle))):
        error = ctypes.get_last_error()
        _k32.CloseHandle(job)
        job = None
    elif not job:
        error = ctypes.get_last_error()
    if job is None and not _warned:
        _warned.append(True)
        _log.warning('process: no job object (error %s), so a timeout ends only the direct child', error)
    if _ntdll.NtResumeProcess(int(child._handle)) != 0:
        _end(child, job)
        _release(job, ended=True)
        child.wait()
        raise OSError('could not resume the suspended child')
    return job


def _end(child, job):
    """End the child's whole tree, or the child alone when it has no job."""
    try:
        if job and _k32.TerminateJobObject(job, 1):
            return
        if not _WINDOWS:
            os.killpg(child.pid, signal.SIGKILL)
            return
    except OSError:
        pass
    try:
        child.kill()
    except OSError:
        pass


def _release(job, ended):
    """Close the job. A tree that finished keeps any lingering descendant alive, as before: lift kill-on-close first."""
    if job:
        if not ended:
            _limit(job, 0)
        _k32.CloseHandle(job)


def _pump(pipe, into):
    def read():
        with pipe:      # the thread closes its own pipe, so a reader still blocked never sees its fd reused
            try:
                while chunk := pipe.read1(65536):
                    into.extend(chunk)
            except (OSError, ValueError):
                pass
    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    return thread


def _feed(pipe, payload):
    def write():
        try:
            with pipe:
                pipe.write(payload)
        except (OSError, ValueError):
            pass
    thread = threading.Thread(target=write, daemon=True)
    thread.start()
    return thread


def _join(child, threads, deadline):
    try:
        child.wait(max(0.0, deadline - time.monotonic()))
    except _subprocess.TimeoutExpired:
        pass
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    return child.returncode is not None and not any(thread.is_alive() for thread in threads)


def _run_tree(argv, *, timeout, capture_output, shell, cwd=None, env=None, stdin=None, input=None, creationflags=0):
    """`subprocess.run(capture_output=True)`, except that a timeout ends the whole tree and drains for `DRAIN_S`."""
    if not capture_output or shell:
        raise ValueError('process spawns capture output and use no shell')
    spawn = dict(cwd=cwd, env=env, stdin=_subprocess.PIPE if input is not None else stdin,
                 stdout=_subprocess.PIPE, stderr=_subprocess.PIPE)
    if _WINDOWS:
        spawn['creationflags'] = creationflags | CREATE_SUSPENDED
    else:
        spawn['start_new_session'] = True
    started = time.monotonic()
    child = _subprocess.Popen(argv, **spawn)
    try:
        job = _contain(child)
    except BaseException:
        for pipe in (child.stdin, child.stdout, child.stderr):
            if pipe:
                pipe.close()
        raise
    out, err = bytearray(), bytearray()
    threads, ended = [], False
    try:
        threads.append(_pump(child.stdout, out))
        threads.append(_pump(child.stderr, err))
        if input is not None:
            threads.append(_feed(child.stdin, input))
        if not _join(child, threads, started + timeout):
            ended = True
            _end(child, job)
            _join(child, threads, time.monotonic() + DRAIN_S)
            raise _subprocess.TimeoutExpired(argv, timeout, output=bytes(out), stderr=bytes(err))
    except _subprocess.TimeoutExpired:
        raise
    except BaseException:       # cancelled, or a thread would not start: the tree goes too
        ended = True
        _end(child, job)
        for pipe in (child.stdout, child.stderr, child.stdin)[len(threads):]:
            if pipe:            # a pipe no thread took; a started thread closes its own
                pipe.close()
        raise
    finally:
        _release(job, ended)
    return _subprocess.CompletedProcess(argv, child.returncode, bytes(out), bytes(err))


class _Spawn:
    """`process.subprocess`: what `run` uses of the real module, with `run` as `_run_tree`. Tests replace this whole
    reference with `FakeCommands`, which scripts `run` with the same options."""
    run = staticmethod(_run_tree)
    DEVNULL, TimeoutExpired = _subprocess.DEVNULL, _subprocess.TimeoutExpired


subprocess = _Spawn()

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
