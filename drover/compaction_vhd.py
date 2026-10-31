"""Shrink a stopped distro's VHDX: the procedure that worked on this machine, every command through `run`.

Mount-VHD -ReadOnly, Optimize-VHD -Mode Full, Dismount-VHD, always dismounting. Mounting a disk a running distro still
holds fails with 0x80070020, so it first waits for the file and refuses while any distro runs. This module reads
wsl.exe directly: the hold's own gates would answer "not running" to the job that set them.
"""
import os
import subprocess
import time

from .placement_exec import NO_WINDOW
from .placement_host import decode_output

STEP_TIMEOUT_S = 120
OPTIMIZE_TIMEOUT_S = 100 * 60       # under the hold's 2 hours, which is stamped just before this step
RELEASE_WAIT_S = 44                 # `wsl --shutdown` returns before the VM lets go of the file
RELEASE_POLL_S = 2
GIB = 1024 ** 3


def _size(path):
    try:
        return os.path.getsize(path) / GIB
    except OSError:
        return None


def _quoted(path):
    return "'" + str(path).replace("'", "''") + "'"


def _pwsh(run, script, timeout=STEP_TIMEOUT_S, label=None):
    """Run one script; the error text when it failed, else None."""
    label = label or script.split()[0]
    try:
        done = run(['pwsh', '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', script], capture_output=True,
                   text=True, timeout=timeout, **NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as error:
        return f'{label}: {error}'
    if done.returncode:
        return f'{label} exited {done.returncode}: {str(done.stderr or "")[-300:].strip()}'
    return None


def _running(run):
    """Why the disk cannot be mounted because of a distro, else None."""
    try:
        done = run(['wsl.exe', '-l', '--running', '-q'], capture_output=True, timeout=20, **NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as error:
        return f'cannot tell whether a distro is running: {error}'
    names = decode_output(done.stdout).split() if done.returncode == 0 else []   # none running exits non-zero
    return f'a distro is running before mount: {" ".join(names)}' if names else None


def _released(vhdx, run, sleep):
    """Wait until nothing has the file open; the error text when something still does."""
    probe = f"$s = [IO.File]::Open({_quoted(vhdx)}, 'Open', 'Read', 'None'); $s.Close()"
    waited = 0
    while True:
        failed = _pwsh(run, probe, 30)
        if failed is None:
            return None
        if waited >= RELEASE_WAIT_S:
            return f'the disk is still held after shutdown: {failed}'
        sleep(RELEASE_POLL_S)
        waited += RELEASE_POLL_S


def dismount(vhdx, *, run=subprocess.run):
    """Detach the disk if it is attached. The error text, else None: a disk that is not attached, after a failed mount
    or a reboot, is already dismounted."""
    path = _quoted(vhdx)
    return _pwsh(run, f'if ((Test-Path -LiteralPath {path}) -and (Get-VHD -Path {path} -ErrorAction Stop).Attached) '
                      f'{{ Dismount-VHD -Path {path} -ErrorAction Stop }}', label='Dismount-VHD')


def compact(vhdx: str, *, run=subprocess.run, sleep=time.sleep, size=_size, before_mount=lambda: None) -> dict:
    """Compact a released disk. `before_mount` runs just before the mount, so a crash after it can be recovered.
    `dismounted` is False only when the disk may still be attached."""
    answer = dict(status='failed', before_gb=size(vhdx), after_gb=None, errors=[], dismounted=True)
    refused = _released(vhdx, run, sleep) or _running(run)
    if refused:
        answer['errors'].append(refused)
        return answer
    before_mount()
    answer['dismounted'] = False
    path = _quoted(vhdx)
    try:
        failed = _pwsh(run, f'Mount-VHD -Path {path} -ReadOnly -NoDriveLetter -ErrorAction Stop') or \
            _pwsh(run, f'Optimize-VHD -Path {path} -Mode Full -ErrorAction Stop', OPTIMIZE_TIMEOUT_S)
        if failed:
            answer['errors'].append(failed)
    finally:
        left = dismount(vhdx, run=run)
    answer['after_gb'] = size(vhdx)
    if left:
        answer['errors'].append(left)
    answer['dismounted'] = left is None
    if not answer['errors']:
        answer['status'] = 'compacted'
    return answer
