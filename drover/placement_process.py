"""Placement process operations."""
import os
import subprocess
import sys
import json
import signal
import time
from contextlib import contextmanager
from pathlib import Path

from .placement_paths import (
    IS_WINDOWS,
    cleanup_side,
)
from .placement_exec import (
    NO_WINDOW,
    WSL,
)

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes


class ScanError(RuntimeError):
    """Process discovery failed; absence is unknown."""


def _proc_cmdlines_linux():
    for e in os.scandir("/proc"):
        if e.name.isdigit():
            try:
                raw = Path(e.path, "cmdline").read_bytes()
            except OSError:
                continue
            yield int(e.name), [a.decode("utf-8", errors="replace") for a in raw.split(b"\0") if a]


def _split_windows_cmdline(line):
    if sys.platform != 'win32':
        # CommandLineToArgvW quoting for Windows evidence inspected from WSL.
        # Apostrophes are ordinary characters; only backslashes before a double
        # quote are escapes. Native Windows uses the OS parser below.
        args, token, quoted, index = [], [], False, 0
        if line[:1] in (' ', '\t'):
            args.append('')
        while index < len(line):
            if line[index] in ' \t' and not quoted:
                index += 1
                continue
            token = []
            while index < len(line) and (quoted or line[index] not in ' \t'):
                slashes = 0
                while index < len(line) and line[index] == '\\':
                    slashes += 1
                    index += 1
                if index < len(line) and line[index] == '"':
                    token.extend('\\' * (slashes // 2))
                    if slashes % 2:
                        token.append('"')
                    elif quoted and line[index:index + 2] == '""':
                        token.append('"')
                        index += 1
                    else:
                        quoted = not quoted
                else:
                    token.extend('\\' * slashes)
                    if index == len(line) or (not quoted and line[index] in ' \t'):
                        break
                    token.append(line[index])
                index += 1
            args.append(''.join(token))
        return args
    n = ctypes.c_int()
    shell32 = ctypes.WinDLL("shell32")
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    ptr = shell32.CommandLineToArgvW(line, ctypes.byref(n))
    if not ptr:
        return []
    try:
        return [ptr[i] for i in range(n.value)]
    finally:
        k = ctypes.WinDLL("kernel32")
        k.LocalFree.argtypes = [ctypes.c_void_p]
        k.LocalFree(ptr)


def _powershell():
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    p = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(p) if p.exists() else "powershell"


def _proc_cmdlines_windows():
    """(pid, argv, parent pid) rows from CIM. Raises ScanError when the list cannot be read."""
    script = ("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
              "Get-CimInstance Win32_Process | Select ProcessId,ParentProcessId,CommandLine | ConvertTo-Json")
    try:
        r = subprocess.run([_powershell(), "-NoProfile", "-Command", script], capture_output=True, timeout=60, **NO_WINDOW)
        if r.returncode != 0:
            raise ScanError(f"powershell exited {r.returncode}")
        text = r.stdout.decode("utf-8", errors="replace").strip()
        rows = json.loads(text)
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        raise ScanError(f"cannot list processes: {e!r}") from e
    rows = [rows] if isinstance(rows, dict) else rows
    if not isinstance(rows, list) or not rows:
        raise ScanError("process list came back empty")
    return [(row["ProcessId"], _split_windows_cmdline(row["CommandLine"]), row.get("ParentProcessId"))
            for row in rows if isinstance(row, dict) and row.get("CommandLine") and isinstance(row.get("ProcessId"), int)]


def process_cmdlines():
    """Yield native process (pid, argv[, parent pid]) rows, or raise ScanError."""
    return _proc_cmdlines_windows() if IS_WINDOWS else _proc_cmdlines_linux()


def stop_ci_children(status):
    """After confirmed runner loss, stop its POSIX group or await Windows job closure.

    Return False when process ownership or termination cannot be confirmed. Zombies
    cannot execute or touch the checkout and need no further signalling.
    """
    if not status.get('pid') or not status.get('run_id'):
        return False  # no recorded child identity: termination remains unknown
    deadline = time.monotonic() + 3
    signalled = False
    while True:
        if IS_WINDOWS:
            # KILL_ON_JOB_CLOSE ends all children when their runner dies. Include
            # processes without readable command lines in the confirmation scan.
            script = ('Get-CimInstance Win32_Process | Select ProcessId,ParentProcessId | ConvertTo-Json')
            result = subprocess.run([_powershell(), '-NoProfile', '-Command', script],
                                    capture_output=True, text=True, timeout=60,
                                    stdin=subprocess.DEVNULL, **NO_WINDOW)
            if result.returncode:
                raise ScanError('cannot confirm CI job closure')
            rows = json.loads(result.stdout)
            rows = [rows] if isinstance(rows, dict) else rows
            if not isinstance(rows, list) or not rows:
                raise ScanError('empty process scan while confirming CI job closure')
            alive = any(row.get('ProcessId') == status.get('pid')
                        or row.get('ParentProcessId') in (status.get('pid'), status.get('runner_pid'))
                        for row in rows)
        else:
            members = []
            for entry in Path('/proc').iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
                    if fields[0] != 'Z' and int(fields[2]) == status.get('pid'):
                        members.append(entry)
                except FileNotFoundError:
                    continue
            alive = bool(members)
            if alive and not signalled:
                marker = ('DROVER_RUN_ID=' + str(status['run_id'])).encode()
                # Never signal a reused group id lacking this execution's marker.
                if not any(marker in (entry / 'environ').read_bytes().split(b'\0') for entry in members):
                    return False
                try:
                    os.killpg(status['pid'], signal.SIGKILL)
                except ProcessLookupError:
                    pass
                signalled = True
        if not alive:
            return True
        if time.monotonic() >= deadline:
            return False
        # Resolver: the kernel finishes terminating the recorded process tree.
        time.sleep(.05)




@contextmanager
def cleanup_process_record(state_dir, cwd=None, pid=None):
    """Publish a Drover process's kernel identity and known cwd for its lifetime."""
    from . import disk_state
    pid = os.getpid() if pid is None else pid
    if IS_WINDOWS:
        birth = _windows_birth(pid)
        identity = dict(pid=pid, side='windows', birth=str(birth) if birth else None)
    else:
        identity = cleanup_identity(pid)
    path = Path(state_dir) / 'disk' / f"process-{identity['side']}-{identity['pid']}.json"
    recorded = False
    try:
        if identity.get('birth') not in (None, '', 'absent'):
            try:
                disk_state.save_file(state_dir, path.name,
                                     dict(identity=identity, cwd=str(Path(cwd or Path.cwd()).absolute())))
                recorded = True
            except OSError:
                pass  # Cleanup remains conservative when the record cannot be published.
        yield
    finally:
        if recorded:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass  # A stale record still requires a matching kernel identity.


def _windows_cwd(pid):
    """Read CurrentDirectory from the process parameters; inaccessible is unknown."""
    import ctypes as c
    from ctypes import wintypes as w
    kernel = c.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.ReadProcessMemory.argtypes = [w.HANDLE, c.c_void_p, c.c_void_p, c.c_size_t, c.POINTER(c.c_size_t)]
    nt = c.WinDLL('ntdll').NtQueryInformationProcess
    nt.argtypes = [w.HANDLE, w.ULONG, c.c_void_p, w.ULONG, c.c_void_p]
    nt.restype = c.c_long
    handle = kernel.OpenProcess(0x0400 | 0x0010, False, pid)
    if not handle:
        return None
    try:
        def read(address, size):
            data = c.create_string_buffer(size)
            count = c.c_size_t()
            if not kernel.ReadProcessMemory(handle, address, data, size, c.byref(count)) or count.value != size:
                raise OSError('process memory unavailable')
            return data.raw
        width = c.sizeof(c.c_void_p)
        basic = (c.c_void_p * 6)()
        if nt(handle, 0, c.byref(basic), c.sizeof(basic), None) < 0:
            return None
        peb = basic[1]
        if width == 8:
            wow = c.c_void_p()
            if nt(handle, 26, c.byref(wow), c.sizeof(wow), None) < 0:
                return None
            if wow.value:
                peb, width = wow.value, 4
        parameters = int.from_bytes(read(peb + (0x20 if width == 8 else 0x10), width), 'little')
        directory = read(parameters + (0x38 if width == 8 else 0x24), 16 if width == 8 else 8)
        length = int.from_bytes(directory[:2], 'little')
        address = int.from_bytes(directory[8:16] if width == 8 else directory[4:8], 'little')
        if not address or not length or length > 65534 or length % 2:
            return None
        return read(address, length).decode('utf-16-le')
    except (OSError, ValueError, OverflowError):
        return None
    finally:
        kernel.CloseHandle(handle)


def _cim_rows(timeout=240, pids=None):
    """One PowerShell pass supplies owner SIDs alongside argv/exe evidence."""
    # Owners come from each process token in this one PowerShell process (no per-process WMI call, no admin).
    # A token that cannot be opened (services, other users, elevated) yields a null owner, which is exactly what
    # GetOwnerSid returned for those processes without elevation; callers already fail closed on a null owner.
    script = r'''$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System; using System.Runtime.InteropServices; using System.Security.Principal;
public static class DroverTok {
  [DllImport("kernel32.dll")] static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
  [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
  [DllImport("advapi32.dll")] static extern bool OpenProcessToken(IntPtr process, uint access, out IntPtr token);
  public static string Owner(uint pid) {
    IntPtr h = OpenProcess(0x1000, false, pid);
    if (h == IntPtr.Zero) return null;
    try {
      IntPtr t;
      if (!OpenProcessToken(h, 8, out t)) return null;
      try { return new WindowsIdentity(t).User.Value; } catch { return null; } finally { CloseHandle(t); }
    } finally { CloseHandle(h); }
  }
}
'@
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$rows = @(Get-CimInstance Win32_Process -Property ProcessId,ParentProcessId,Name,ExecutablePath,CommandLine,SessionId | ForEach-Object {
    [pscustomobject]@{ProcessId=$_.ProcessId; Name=$_.Name; ExecutablePath=$_.ExecutablePath;
                     CommandLine=$_.CommandLine; SessionId=$_.SessionId; OwnerSid=[DroverTok]::Owner([uint32]$_.ProcessId)}
})
@{sid=$sid; processes=$rows} | ConvertTo-Json -Compress -Depth 4
'''
    if pids is not None:
        filters = ' OR '.join(f'ProcessId={int(pid)}' for pid in pids)
        script = script.replace('Get-CimInstance Win32_Process -Property',
                                f"Get-CimInstance Win32_Process -Filter '{filters}' -Property")
    out = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                         capture_output=True, text=True, timeout=timeout, errors='replace', **NO_WINDOW)
    if out.returncode or not out.stdout.strip():
        raise OSError('CIM query failed: ' + out.stderr.strip()[:200])
    data = json.loads(out.stdout)
    if not data.get('sid') or not isinstance(data.get('processes'), list):
        raise ValueError('CIM ownership unavailable')
    return [dict(pid=d['ProcessId'], name=d.get('Name'), session_id=d.get('SessionId'),
                 exe=d.get('ExecutablePath'),
                 command_unreadable=d.get('CommandLine') is None,
                 same_user=d.get('OwnerSid') == data['sid'] if d.get('OwnerSid') else None,
                 cmd=' '.join(filter(None, [d.get('ExecutablePath'), d.get('CommandLine')])))
            for d in data['processes'] if d.get('ProcessId') != os.getpid()]


def _read_windows_birth(pid):
    """Native FILETIME; only ERROR_INVALID_PARAMETER proves a positive PID absent."""
    import ctypes as c
    from ctypes import wintypes as w
    if type(pid) is not int or not 0 < pid <= 0xffffffff:
        raise OSError('invalid process id')
    kernel = c.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.GetProcessTimes.argtypes = [w.HANDLE, *([c.POINTER(w.FILETIME)] * 4)]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        error = c.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: the process ID does not exist.
            raise ProcessLookupError(error, 'process does not exist')
        raise OSError(error, 'cannot open process identity')
    try:
        times = [w.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(c.byref(t) for t in times)):
            raise OSError(c.get_last_error(), 'cannot read process creation time')
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


def _windows_birth(pid):
    """Native FILETIME for process records; an unavailable identity remains None."""
    try:
        return _read_windows_birth(pid)
    except OSError:
        return None


def _windows_processes():
    """Cheap native identities catch processes started or reused after the single CIM query."""
    import ctypes as c
    from ctypes import wintypes as w
    class Entry(c.Structure):
        _fields_ = [('size', w.DWORD), ('usage', w.DWORD), ('pid', w.DWORD),
                    ('heap', c.c_size_t), ('module', w.DWORD), ('threads', w.DWORD),
                    ('parent', w.DWORD), ('priority', w.LONG), ('flags', w.DWORD),
                    ('image', w.WCHAR * 260)]
    kernel = c.WinDLL('kernel32', use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = w.HANDLE
    kernel.Process32FirstW.argtypes = kernel.Process32NextW.argtypes = [w.HANDLE, c.POINTER(Entry)]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == c.c_void_p(-1).value:
        raise c.WinError(c.get_last_error())
    rows, entry = {}, Entry()
    entry.size = c.sizeof(entry)
    try:
        more = kernel.Process32FirstW(snapshot, c.byref(entry))
        while more:
            if entry.pid and entry.pid != os.getpid():
                rows[str(entry.pid)] = [entry.image, _windows_birth(entry.pid)]
            more = kernel.Process32NextW(snapshot, c.byref(entry))
        if c.get_last_error() != 18:  # ERROR_NO_MORE_FILES is the only complete enumeration.
            raise c.WinError(c.get_last_error())
        return rows
    finally:
        kernel.CloseHandle(snapshot)


def _cleanup_cim(request, current=None):
    """Cache one narrow process query per stage/drive decision; rechecks verify identity."""
    from . import disk_state
    request = request or {}
    cache = None
    if request.get('_inventory_id'):
        suffix = ''
        if 'stage' in request:
            import hashlib
            suffix = '-' + hashlib.sha256(json.dumps([request['stage'], request.get('drive')], sort_keys=True).encode()).hexdigest()[:16]
        cache = 'processes-' + request['_inventory_id'] + suffix + '.json'
        try:
            return json.loads((Path(request['state_dir']) / 'disk' / cache).read_text(encoding='utf-8'))
        except FileNotFoundError:
            pass
    timeout = request.get('paths', {}).get('process_query_timeout_s', 240)
    started = time.monotonic()
    try:
        answer = dict(rows=_cim_rows(timeout), identities=current)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        answer = dict(rows=[], error=str(error), elapsed_s=time.monotonic() - started,
                      reason_code='process_query_timeout' if isinstance(error, subprocess.TimeoutExpired)
                      else 'process_visibility_unavailable')
    if cache:
        disk_state.save_file(request['state_dir'], cache, answer)
    return answer


_SELF_IDENTITY = None


def cleanup_identity(pid=None):
    """PID plus kernel creation identity; unknown never proves a claim dead.

    This process's own identity cannot change, so its kernel creation time is read once."""
    global _SELF_IDENTITY
    pid = os.getpid() if pid is None else pid
    if pid == os.getpid() and _SELF_IDENTITY and _SELF_IDENTITY['pid'] == pid:
        return dict(_SELF_IDENTITY)
    if IS_WINDOWS:
        try:
            birth = str(_read_windows_birth(pid))
        except ProcessLookupError:
            birth = 'absent'
        except OSError:
            birth = None
    else:
        try:
            started = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
        except FileNotFoundError:
            birth = 'absent'
        except (OSError, IndexError):
            birth = None
        else:
            try:
                boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
                birth = f'{boot}:{started}' if boot and started else None
            except OSError:
                birth = None
    identity = dict(pid=pid, birth=birth, side=cleanup_side())
    if pid == os.getpid() and birth and birth != 'absent':
        _SELF_IDENTITY = dict(identity)
    return identity
