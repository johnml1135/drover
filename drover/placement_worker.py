"""Placement worker operations."""
import os
import sys
import json
import time

from . import process
from .placement_exec import locale_text
from .placement_paths import (
    IS_WINDOWS,
)

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes
    from .placement_tree import _k32, _EXT, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, JOB_EXTENDED_LIMIT_CLASS


class CleanupWorkerError(RuntimeError):
    termination_confirmed = False


class _CleanupJob:
    """Parent-held Windows job: retain ownership until active membership is zero."""

    def __init__(self, token):
        self.handle = _k32.CreateJobObjectW(None, 'DroverCleanup-' + token)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = _EXT()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _k32.SetInformationJobObject(self.handle, JOB_EXTENDED_LIMIT_CLASS,
                                          ctypes.byref(info), ctypes.sizeof(info)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def terminate(self):
        if not _k32.TerminateJobObject(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())
        class Accounting(ctypes.Structure):
            _fields_ = [(n, ctypes.c_int64) for n in ('user', 'kernel', 'period_user', 'period_kernel')] + [
                (n, wintypes.DWORD) for n in ('faults', 'total', 'active', 'terminated')]
        deadline = time.monotonic() + 3
        while True:
            info = Accounting()
            if not _k32.QueryInformationJobObject(self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if not info.active:
                return True
            if time.monotonic() >= deadline:
                return False
            # Resolver: Windows completes termination of every job member.
            time.sleep(.05)

    def close(self):
        _k32.CloseHandle(self.handle)


def cleanup_worker(request):
    """Own the worker group/job before any stage can spawn children."""
    from . import disk_state
    lower_priority()
    cap = (request.get('paths') or {}).get('cpu_limit')
    if cap:
        limit_cpus(cap)
    if IS_WINDOWS:
        # The caller holds this named job until every member has exited. Join before
        # executing the stage, so no deletion subprocess can escape containment.
        job = _k32.OpenJobObjectW(1, False, 'DroverCleanup-' + request['_worker_id'])
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not _k32.AssignProcessToJobObject(job, -1):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            _k32.CloseHandle(job)
    else:
        try:
            os.setsid()
        except OSError as error:
            import errno
            if error.errno != errno.EPERM or os.getpid() != os.getsid(0):
                raise
            # wsl.exe already established this session and group. Its group id
            # is our pid, so the existing termination proof covers descendants.
    status = dict(pid=os.getpid(), runner_pid=os.getpid(), run_id=request['_worker_id'],
                  request={key: request[key] for key in ('stage', 'side', 'apply', 'drive', 'repos') if key in request},
                  context=dict(cwd=os.getcwd(), uid=os.getuid() if hasattr(os, 'getuid') else None,
                               distro=os.environ.get('WSL_DISTRO_NAME'),
                               vhdx_dir=os.environ.get('DROVER_VHDX_DIR')))
    name = 'worker-' + request['_worker_id'] + '.json'
    disk_state.save_file(request['state_dir'], name, status)
    try:
        # The stage modules load once the claim is written, so their import spends no start-up time, and a failed
        # import is a worker failure with its claim saved.
        from . import disk_side
        # The rows leave as lines while the stage runs, ending with its `end` line, so the claim keeps no result.
        disk_side.serve(request, out=sys.stdout.buffer)
    except BaseException as error:
        disk_state.save_file(request['state_dir'], name, dict(status, error=str(error)))
        raise


# No application import: a broken startup must still be independently verifiable.
_CLEANUP_STARTUP_PROOF = r'''import os, sys
from pathlib import Path
token = sys.argv[1].encode()
me = os.getpid()
def marked(pid):
    # True when this process carries the token in argv or the marker in a readable environ.
    base = Path('/proc') / str(pid)
    if token in (base / 'cmdline').read_bytes().split(b'\0'):
        return True
    try:
        return b'DROVER_RUN_ID=' + token in (base / 'environ').read_bytes().split(b'\0')
    except PermissionError:
        return False
def started_by_drover(stat_text):
    # Walk ppid to init (bounded); an unreadable or vanished link is unknown and fails closed (OSError).
    ppid = int(stat_text.rsplit(')', 1)[1].split()[1])
    for _ in range(64):
        if ppid <= 1:
            return False
        if ppid != me and marked(ppid):
            return True
        ppid = int((Path('/proc') / str(ppid) / 'stat').read_text().rsplit(')', 1)[1].split()[1])
    return False
for entry in Path('/proc').iterdir():
    if not entry.name.isdigit() or int(entry.name) == os.getpid():
        continue
    try:
        if entry.stat().st_uid != os.getuid():
            continue
        stat_text = (entry / 'stat').read_text()
        if stat_text.rsplit(')', 1)[1].split()[0] == 'Z':
            continue
        if token in (entry / 'cmdline').read_bytes().split(b'\0'):
            sys.exit(1)
        try:
            if b'DROVER_RUN_ID=' + token in (entry / 'environ').read_bytes().split(b'\0'):
                sys.exit(1)
        except PermissionError:
            # Unreadable environ and no token in argv: Drover's own only if a Drover process started it.
            if started_by_drover(stat_text):
                sys.exit(1)
    except FileNotFoundError:
        continue
    except OSError:
        sys.exit(2)
'''


def disk_toast(notice: dict) -> None:
    """Best-effort current interactive user transport; durable delivery is separate."""
    if not IS_WINDOWS:
        raise OSError('Windows toast unsupported on this placement')
    script = r'''
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] > $null
$n = $input | Out-String | ConvertFrom-Json
$xml = [Windows.Data.Xml.Dom.XmlDocument]::new()
$xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text></text><text></text></binding></visual></toast>')
$nodes = $xml.GetElementsByTagName('text')
$nodes.Item(0).InnerText = $n.title
$nodes.Item(1).InnerText = $n.body
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Drover').Show([Windows.UI.Notifications.ToastNotification]::new($xml))
'''
    done = process.run(process.Command(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script], 15,
                                       stdin=json.dumps(notice)))
    if done.kind == 'timed_out':
        raise OSError('toast delivery timed out')
    if done.kind == 'unavailable':
        raise OSError(done.detail)
    if done.code:
        raise OSError('toast unavailable: ' + locale_text(done.err)[-1000:])


BELOW_NORMAL_PRIORITY_CLASS = 0x4000


CLEANUP_NICE = 10


def lower_priority(windows=None, set_class=None):
    """Run this process, and so its children, at BelowNormal (Windows) or nice 10 (WSL/Linux). Best effort: returns
    whether it took."""
    windows = IS_WINDOWS if windows is None else windows
    try:
        if windows:
            if set_class is None:
                def set_class(value):
                    if not _k32.SetPriorityClass(_k32.GetCurrentProcess(), value):
                        raise ctypes.WinError(ctypes.get_last_error())
            set_class(BELOW_NORMAL_PRIORITY_CLASS)
        else:
            os.nice(CLEANUP_NICE)
    except (OSError, AttributeError):
        return False
    return True


def _allowed_cpus(windows):
    """The CPUs this process may run on now, ascending. An inherited affinity is not always 0..N-1."""
    if not windows:
        return sorted(os.sched_getaffinity(0))
    mask, system = ctypes.c_size_t(), ctypes.c_size_t()
    if not _k32.GetProcessAffinityMask(_k32.GetCurrentProcess(), ctypes.byref(mask), ctypes.byref(system)):
        raise ctypes.WinError(ctypes.get_last_error())
    return [cpu for cpu in range(mask.value.bit_length()) if mask.value >> cpu & 1]


def limit_cpus(count, *, windows=None, set_mask=None, available=None):
    """Pin this process, and so its children, to the first `count` of the CPUs it may use. Best effort.

    `available` is those CPUs by number. Returns how many are in effect: the smaller of `count` and those available, or
    all of them if pinning failed."""
    windows = IS_WINDOWS if windows is None else windows
    if available is None:
        try:
            available = _allowed_cpus(windows)
        except (OSError, AttributeError):
            available = range(os.cpu_count() or 1)
    available = sorted(available)
    limit = max(1, min(count, len(available)))
    if limit >= len(available):
        return len(available)
    chosen = available[:limit]
    try:
        if windows:
            if set_mask is None:
                def set_mask(mask):
                    if not _k32.SetProcessAffinityMask(_k32.GetCurrentProcess(), mask):
                        raise ctypes.WinError(ctypes.get_last_error())
            set_mask(sum(1 << cpu for cpu in chosen))
        else:
            (set_mask or (lambda cpus: os.sched_setaffinity(0, cpus)))(set(chosen))
    except (OSError, AttributeError, OverflowError):
        return len(available)
    return limit
