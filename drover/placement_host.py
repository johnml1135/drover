"""Placement host operations."""
import os
import re
import subprocess
import shutil
from pathlib import Path

from .placement_paths import (
    IS_WINDOWS,
    cleanup_normalize,
    drive,
    is_linux_native,
    native,
    to_wsl_path,
)
from .placement_exec import (
    NO_WINDOW,
    distro,
    wsl_control,
)
from .placement_proof import (
    default_process_ignore,
)


def decode_output(raw):
    if isinstance(raw, str):
        return raw
    return raw.decode("utf-16-le" if b"\x00" in raw[:4] else "utf-8", "ignore").replace("\x00", "")


def is_drvfs(path):
    """Whether a resolved local path crosses the WSL DrvFs boundary."""
    return not IS_WINDOWS and str(Path(path).resolve()).startswith("/mnt/")


def wsl_zoneinfo_path(zone):
    """The configured distro's timezone file visible to Windows, or None."""
    if not IS_WINDOWS or any(p in ('', '.', '..') for p in zone.split('/')) or '\\' in zone:
        return None
    return Path('//wsl.localhost') / distro() / 'usr/share/zoneinfo' / zone


LXSS = r'HKCU\Software\Microsoft\Windows\CurrentVersion\Lxss'


REG = native(r'C:\Windows\System32\reg.exe')


_VHDX_CACHE = {}


def vhdx_dir():
    """WSL path of the directory holding this distro's ext4.vhdx, or None (one registry query per process)."""
    key = (os.environ.get('DROVER_VHDX_DIR'), os.environ.get('WSL_DISTRO_NAME'), REG)
    if key not in _VHDX_CACHE:
        _VHDX_CACHE[key] = _vhdx_dir()
    return _VHDX_CACHE[key]


def _vhdx_dir():
    override = os.environ.get('DROVER_VHDX_DIR')
    if override:
        return override
    distro = os.environ.get('WSL_DISTRO_NAME')
    if not distro or not os.path.exists(REG):
        return None
    try:
        out = subprocess.run([REG, 'query', LXSS, '/s'], text=True, capture_output=True, timeout=10, **NO_WINDOW).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    name = base = None
    for line in out.replace('\r', '').splitlines():
        if line.startswith('HKEY_'):
            name = base = None
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0] == 'DistributionName':
            name = parts[2]
        elif len(parts) == 3 and parts[0] == 'BasePath':
            base = parts[2]
        if name == distro and base:
            return to_wsl_path(base) if drive(base) else None
    return None


def detect_vhdx_drives():
    """{distro: 'R:'} for the drive holding each distro's ext4.vhdx, from the Lxss registry key."""
    out = {}
    if IS_WINDOWS:
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Lxss")
            for i in range(winreg.QueryInfoKey(key)[0]):
                with winreg.OpenKey(key, winreg.EnumKey(key, i)) as sub:
                    try:
                        name = winreg.QueryValueEx(sub, "DistributionName")[0]
                        base = winreg.QueryValueEx(sub, "BasePath")[0]
                    except OSError:
                        continue
                    m = re.match(r"^(?:\\\\\?\\)?([A-Za-z]):", base)
                    if m:
                        out[name] = m.group(1).upper() + ":"
        except OSError:
            pass
        return out
    host = vhdx_dir()
    letter = drive(host or "")
    if letter and os.environ.get("WSL_DISTRO_NAME"):
        out[os.environ["WSL_DISTRO_NAME"]] = letter + ":"
    return out


def parse_distros(text):
    """[{'name', 'state', 'version', 'default'}] from `wsl -l -v` output."""
    out = []
    for line in text.replace("\x00", "").splitlines()[1:]:
        m = re.match(r"^\s*(\*)?\s*(\S.*?)\s{2,}(\w+)\s+(\d)\s*$", line)
        if m:
            out.append({"name": m.group(2), "state": m.group(3), "version": int(m.group(4)), "default": bool(m.group(1))})
    return out


def detect_distros():
    if not IS_WINDOWS:
        name = os.environ.get("WSL_DISTRO_NAME")
        return [{"name": name, "state": "Running", "version": 2, "default": True}] if name else []
    try:
        r = subprocess.run(wsl_control(["-l", "-v"]), capture_output=True, timeout=20, **NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_distros(decode_output(r.stdout)) if r and r.returncode == 0 else []


def physical_disk_path(path: str) -> str | None:
    """Native physical root; Linux contents in WSL are charged to their VHDX host drive."""
    letter = drive(path)
    if letter:
        return native(letter + ':/')
    if not is_linux_native(path):
        return None
    if IS_WINDOWS:
        host = detect_vhdx_drives().get(distro())
        letter = drive(host + '/') if host else None
        return native(letter + ':/') if letter else None
    if os.environ.get('WSL_DISTRO_NAME'):
        host = vhdx_dir()
        letter = drive(host) if host else None
        return native(letter + ':/') if letter else None
    return linux_mount(path)


def linux_mount(path):
    from pathlib import PurePosixPath
    target = PurePosixPath(cleanup_normalize(path, False))
    try:
        mounts = []
        for line in Path('/proc/self/mountinfo').read_text().splitlines():
            encoded = line.split()[4]
            mount = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), encoded)
            if target.is_relative_to(PurePosixPath(cleanup_normalize(mount, False))):
                mounts.append(mount)
        return max(mounts, key=len) if mounts else None
    except (OSError, ValueError, IndexError):
        return None


def default_disk_trackers() -> list[dict]:
    """Physical disks for a Windows host (including WSL), or the Linux root."""
    paths = [("h", "H:/"), ("c", "C:/")] if IS_WINDOWS or os.environ.get("WSL_DISTRO_NAME") else [("root", "/")]
    keys = ("warn_gb", "queue_gb", "hold_gb", "critical_gb")
    return [{"kind": "disk", "name": name, "enabled": True, "path": path,
             **dict(zip(keys, (60.0, 40.0, 25.0, 15.0) if drive(path) == "C" else (150.0, 100.0, 50.0, 25.0)))}
            for name, path in paths]


# Detection is installation data, never stage policy. Existing declarations win.
CLEANUP_CACHE_COMMANDS = (
    ('uv', ('uv', 'cache', 'prune')),
    ('pip', ('pip', 'cache', 'purge')),
    ('npm', ('npm', 'cache', 'clean', '--force')),
    ('dotnet', ('dotnet', 'nuget', 'locals', 'http-cache', '--clear')),
)


CLEANUP_CACHE_FOLDERS = (('cargo', 'CARGO_HOME', '~/.cargo', 'registry/cache'),)


CLEANUP_SCRATCH_ROOTS = ('~/.claude/scratchpad', '~/.codex/scratchpad',)


def detected_cleanup_policy(state_dir):
    """Editable machine defaults for installed tools and existing scratch roots."""
    import tempfile
    side = 'windows' if IS_WINDOWS else 'wsl'
    state = Path(state_dir).expanduser().absolute()
    roots = [tempfile.gettempdir(), str(state / 'scratch')]
    roots.extend(str(Path(p).expanduser().absolute()) for p in CLEANUP_SCRATCH_ROOTS
                 if Path(p).expanduser().is_dir())
    return dict(temp_roots=list(dict.fromkeys(roots)), report_roots=[],
                process_ignore=default_process_ignore(), process_query_timeout_s=240, caches=[
        dict(argv=list(argv), cwd=str(state), placement=side)
        for tool, argv in CLEANUP_CACHE_COMMANDS if shutil.which(tool)] + [
        dict(path=str(Path(os.environ.get(variable, fallback)).expanduser().absolute() / suffix),
             placement=side, max_age_days=30)
        for tool, variable, fallback, suffix in CLEANUP_CACHE_FOLDERS if shutil.which(tool)])


def _wsl_vhdx_paths():
    """Registered distro disks; registry access does not require elevation."""
    import ntpath
    import winreg
    paths = {}
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Lxss') as key:
        for index in range(winreg.QueryInfoKey(key)[0]):
            with winreg.OpenKey(key, winreg.EnumKey(key, index)) as sub:
                try:
                    name = winreg.QueryValueEx(sub, 'DistributionName')[0]
                    base = winreg.ExpandEnvironmentStrings(winreg.QueryValueEx(sub, 'BasePath')[0])
                    paths[name] = ntpath.join(base, 'ext4.vhdx')
                except OSError:
                    continue
    return paths


def _vhdx_reading(path):
    import ctypes as c
    from ctypes import wintypes as w
    kernel = c.WinDLL('kernel32', use_last_error=True)
    kernel.GetFileAttributesW.argtypes = [w.LPCWSTR]
    kernel.GetFileAttributesW.restype = w.DWORD
    attributes = kernel.GetFileAttributesW(path)
    if attributes == 0xffffffff:
        raise c.WinError(c.get_last_error())
    kernel.GetCompressedFileSizeW.argtypes = [w.LPCWSTR, c.POINTER(w.DWORD)]
    kernel.GetCompressedFileSizeW.restype = w.DWORD
    high = w.DWORD()
    c.set_last_error(0)
    low = kernel.GetCompressedFileSizeW(path, c.byref(high))
    if low == 0xffffffff and c.get_last_error():
        raise c.WinError(c.get_last_error())
    return dict(sparse=bool(attributes & 0x200), allocated_bytes=(high.value << 32) | low,
                apparent_bytes=os.stat(path).st_size)


def cleanup_vhdx_report(*, paths=None, read=None):
    """Report sparse/allocated disk facts and owner actions; never modify a VHDX."""
    if paths is None and not IS_WINDOWS:
        return []
    try:
        paths = _wsl_vhdx_paths() if paths is None else paths
    except OSError as error:
        return [dict(action='report', reason_code='wsl_vhdx_unavailable', error=str(error))]
    rows = []
    for name, path in paths.items():
        row = dict(action='report', reason_code='wsl_vhdx', distro=name, path=path)
        try:
            row.update((read or _vhdx_reading)(path))
            if row['sparse'] is False:
                distro_arg = subprocess.list2cmdline([name])
                row['owner_action'] = (
                    f'Periodically run `wsl -d {distro_arg} -u root -- fstrim -v /`; to return space, '
                    f'run `wsl --shutdown`, then run `Optimize-VHD -Path "{path}" -Mode Full` '
                    '(requires admin) in elevated PowerShell.')
        except OSError as error:
            row['error'] = str(error)
        rows.append(row)
    return rows


def trim_argv():
    """No privilege prompts: Windows runs the pinned WSL distro's root trim."""
    if IS_WINDOWS:
        return wsl_control(['-d', distro(), '-u', 'root', '--', 'fstrim', '-v', '/'])
    return ['fstrim', '-v', '/'] if os.geteuid() == 0 else None
