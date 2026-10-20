"""Placement: Windows and WSL command execution, paths and process ownership.

Commands are argument lists. Only explicitly identified paths are translated;
configured worker/check argv is never rewritten. The hub binds its live settings
once; all callers then resolve the same ledger override above configuration.
"""
import os
import re
import subprocess
import sys
import json
import signal
import shutil
import time
from pathlib import Path

KILL_AFTER_S = 15


class ScanError(RuntimeError):
    """Process discovery failed; absence is unknown."""


IS_WINDOWS = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000
CREATE_SUSPENDED = 0x4
CREATE_NEW_PROCESS_GROUP = 0x200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
NO_WINDOW = {"creationflags": CREATE_NO_WINDOW} if IS_WINDOWS else {}
WIN_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$", re.S)
MNT_RE = re.compile(r"^/mnt/([a-zA-Z])(?:/(.*))?$")
_settings = lambda: {}


def bind_settings(provider):
    global _settings
    previous, _settings = _settings, provider
    return previous


def distro(default=None, settings=None):
    from . import config
    values = _settings() if settings is None else settings
    default = default() if callable(default) else default
    return values.get("wsl_distro") or (default if default is not None else config.get()["paths"]["wsl_distro"])


def to_wsl_path(arg):
    arg = str(arg)
    m = WIN_RE.match(arg.removeprefix("\\\\?\\"))
    if m:
        return f"/mnt/{m.group(1).lower()}/" + m.group(2).replace("\\", "/")
    if arg.startswith("--") and "=" in arg:
        k, _, v = arg.partition("=")
        return f"{k}={to_wsl_path(v)}"
    return arg


def to_windows_path(path):
    path = str(path)
    m = MNT_RE.match(path)
    return f"{m.group(1).upper()}:\\" + (m.group(2) or "").replace("/", "\\") if m else path


def native(path):
    path = os.path.expanduser(str(path))
    return to_windows_path(path) if IS_WINDOWS else to_wsl_path(path)


def canonical(path):
    return str(path) if routes_through_wsl(path) else str(Path(native(path)).resolve())


def path_parts(path):
    """Resolved components for overlap checks, respecting drive-mount case insensitivity."""
    path = Path(native(path)).resolve()
    return tuple(p.casefold() for p in path.parts) if IS_WINDOWS or MNT_RE.match(str(path)) else path.parts


def config_path(path, reference):
    """Spell a discovered path like the repository's configured path."""
    path = str(path)
    return to_windows_path(path).replace("/", "\\") if WIN_RE.match(str(reference)) else path


def drive(path):
    """The Windows drive letter for either path spelling, or None for a Linux filesystem."""
    match = WIN_RE.match(str(path).removeprefix("\\\\?\\")) or MNT_RE.match(str(path))
    return match.group(1).upper() if match else None


def windows_system_path(relative):
    root = os.environ.get("SystemRoot", r"C:\Windows")
    return native(root.rstrip("\\/") + "/System32/" + relative)


def sandbox_roots(roots):
    """POSIX sandbox mounts have no native Windows counterpart."""
    return () if IS_WINDOWS else roots


def status_args(repo):
    """Tracked status; commit guards separately discover untracked paths with ls-files."""
    return ["status", "--porcelain", "--untracked-files=no"]


def _git_location(repo):
    """Override foreign absolute gitdir pointers without editing shared metadata."""
    marker = Path(repo) / ".git"
    if marker.is_dir():
        return [], {}
    try:
        text = marker.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, IsADirectoryError):
        return [], {}
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"cannot read git metadata for {repo}: {exc}") from exc
    if not text.startswith("gitdir: "):
        raise ValueError(f"invalid git metadata pointer for {repo}")
    if native(text[8:]) == text[8:]:
        return [], {}
    try:
        gitdir = Path(native(text[8:])).resolve(strict=True)
        common_file = gitdir / "commondir"
        common = ((gitdir / native(common_file.read_text(encoding="utf-8").strip())).resolve(strict=True)
                  if common_file.exists() else gitdir)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"cannot resolve git metadata for {repo}: {exc}") from exc
    return [f"--git-dir={gitdir}", f"--work-tree={repo}"], {"GIT_COMMON_DIR": str(common)}


def prepare_worktree(repo):
    """Make a foreign gitfile portable before checks run their own Git commands."""
    if _git_location(native(repo))[0] and not relativize_gitdir(native(repo)):
        raise OSError(f"cannot make gitdir portable for {repo}")


def is_linux_native(path):
    return str(path).startswith("/") and not MNT_RE.match(str(path))


def routes_through_wsl(path, nt=None):
    return (IS_WINDOWS if nt is None else nt) and is_linux_native(path)


class Windows:
    path = staticmethod(to_windows_path)

    def executables(self, names):
        """Native Windows tools, when this process can inspect that platform."""
        return {name: shutil.which(name) for name in names} if IS_WINDOWS else {}

    def command(self, argv):
        return list(argv)

    def spawn(self, argv, **kw):
        if WindowsTree is None:
            raise OSError("Windows process ownership requires Windows")
        proc = subprocess.Popen(argv, creationflags=CREATE_NEW_PROCESS_GROUP | CREATE_SUSPENDED | CREATE_NO_WINDOW, **kw)
        return proc, WindowsTree(proc)


class WSL:
    path = staticmethod(to_wsl_path)

    def __init__(self, default=None):
        self.default = default

    def command(self, argv, cd=None, env=None):
        name = distro(self.default)
        if not re.fullmatch(r"[A-Za-z0-9._-]+", str(name)) or name.startswith("-"):
            raise ValueError(f"bad WSL distro name {name!r}")
        prefix = wsl_control(["-d", name])
        if cd:
            prefix += ["--cd", self.path(cd)]
        if env:
            if any(not re.fullmatch(r"[A-Z][A-Z0-9_]*", str(k)) for k in env):
                raise ValueError("bad environment variable name")
            argv = ["/usr/bin/env", *[f"{k}={self.path(v)}" for k, v in env.items()], *argv]
        return prefix + ["--exec", *argv]

    def spawn(self, argv, **kw):
        proc = subprocess.Popen(argv, start_new_session=True, **kw)
        return proc, PosixTree(proc)


def wsl_control(args):
    return ["wsl.exe", *args]


# Arguments that follow these are paths; subcommand positionals of `worktree add|repair|remove|move` are too.
_PATH_OPTS = {"--git-dir", "--work-tree", "-C", "--output", "-o"}
_WORKTREE_PATH_SUBS = {"add", "repair", "remove", "move", "lock", "unlock"}


def _map_paths(args):
    """Translate only path arguments to WSL form; messages, refs and revisions pass through unchanged."""
    args, out = [str(a) for a in args], []
    sub = args.index("worktree") + 1 if "worktree" in args[:3] else len(args)
    worktree_path = sub < len(args) and args[sub] in _WORKTREE_PATH_SUBS
    value_for, mapped = None, False
    for i, arg in enumerate(args):
        if value_for:
            out.append(to_wsl_path(arg) if value_for in _PATH_OPTS else arg)
            value_for = None
            continue
        if arg in _PATH_OPTS | {"-b", "-B", "--reason", "-m"}:
            value_for = arg
        elif arg.startswith(("--git-dir=", "--work-tree=")):
            key, _, value = arg.partition("=")
            arg = f"{key}={to_wsl_path(value)}"
        elif worktree_path and i > sub and not mapped and not arg.startswith("-"):
            arg, mapped = to_wsl_path(arg), True
        out.append(arg)
    return out


def _git_command(repo, args, nt=None, distro=None):
    repo = str(repo)
    if routes_through_wsl(repo, nt):
        return WSL(distro).command(["git", "-C", repo, *_map_paths(args)]), {}
    repo = native(repo)
    prefix, env = _git_location(repo)
    return Windows().command(["git", "-C", repo, *prefix, *args]), env


def git_argv(repo, args, nt=None, distro=None):
    """The argv that runs `git -C repo *args` from this host."""
    return _git_command(repo, args, nt, distro)[0]


def git(repo, *args, timeout=None, env=None, runner=None):
    """subprocess result of git in `repo`; a missing wsl.exe/git is a failed command (rc 127), not an exception."""
    try:
        argv, extra = _git_command(repo, args)
        if extra:
            env = {**(os.environ if env is None else env), **extra}
        return (runner or subprocess.run)(argv, text=True, capture_output=True, encoding="utf-8", errors="replace",
                                           timeout=timeout, env=env, **NO_WINDOW)
    except FileNotFoundError as e:
        return subprocess.CompletedProcess(args, 127, "", f"git unavailable: {e}")


def reachable(path, runner=None):
    """True when `path` is an openable git working directory from this host (a missing one is False, never an error)."""
    path = str(path)
    if not path:
        return False
    if routes_through_wsl(path):
        return git(path, "rev-parse", "--git-dir", timeout=30, runner=runner).returncode == 0
    return os.path.exists(native(path))


def relativize_gitdir(path):
    dotgit = Path(path) / ".git"
    try:
        pointer = dotgit.read_text(encoding="utf-8").strip().removeprefix("gitdir: ")
    except (OSError, UnicodeDecodeError):
        return True
    if not (Path(pointer).is_absolute() or WIN_RE.match(pointer) or MNT_RE.match(pointer)):
        return True
    admin = native(pointer)
    if drive(native(path)) != drive(admin):
        return False
    try:
        if not os.path.isdir(admin):
            return False
        rel = os.path.relpath(admin, path).replace("\\", "/")
        dotgit.write_text(f"gitdir: {rel}\n", encoding="utf-8")
    except (OSError, ValueError):
        return False
    return True


# ---- process tree -----------------------------------------------------------------------------------------
class PosixTree:
    def __init__(self, proc):
        self.proc, self.pgid = proc, proc.pid

    def _group_alive(self):
        try:
            os.killpg(self.pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _sig(self, sig):
        try:
            os.killpg(self.pgid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def kill(self, grace=KILL_AFTER_S):
        """SIGTERM the group, then SIGKILL whatever is left after grace seconds."""
        self._sig(signal.SIGTERM)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            self.proc.poll()            # reap the leader so a zombie does not keep the group "alive"
            if not self._group_alive():
                break
            time.sleep(0.05)
        if self._group_alive():          # not after the group is gone: the pgid could be reused
            self._sig(signal.SIGKILL)
        self.proc.wait()

    def reap_stragglers(self):
        """The worker exited; end anything it left behind in the group."""
        if self._group_alive():
            self._sig(signal.SIGKILL)

    def metrics(self):
        from .metrics import proc_group
        return proc_group(self.pgid)

    def close(self):
        pass


WindowsTree = None
if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    class _BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _IOC(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("ro", "wo", "oo", "rb", "wb", "ob")]

    class _EXT(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BASIC), ("IoInfo", _IOC), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JOB_BASIC_AND_IO_ACCOUNTING_CLASS = 8
    JOB_EXTENDED_LIMIT_CLASS = 9

    def _resume(handle):
        nt = ctypes.WinDLL("ntdll")
        nt.NtResumeProcess.argtypes = [wintypes.HANDLE]
        return nt.NtResumeProcess(int(handle))

    class WindowsTree:
        """A Job Object that kills its whole tree on TerminateJobObject, or when the runner dies (KILL_ON_CLOSE)."""

        def __init__(self, proc):
            self.proc, self.job = proc, None
            try:
                self.job = self._make_job(proc)
                if _resume(proc._handle) != 0:      # the worker started suspended so no child escapes the job
                    raise OSError("could not resume the suspended worker")
            except BaseException:
                self.close_and_kill()
                raise

        @staticmethod
        def _make_job(proc):
            job = _k32.CreateJobObjectW(None, None)
            if not job:
                raise ctypes.WinError(ctypes.get_last_error())
            info = _EXT()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not _k32.SetInformationJobObject(job, JOB_EXTENDED_LIMIT_CLASS, ctypes.byref(info),
                                                ctypes.sizeof(info)) \
                    or not _k32.AssignProcessToJobObject(job, int(proc._handle)):
                err = ctypes.get_last_error()
                _k32.CloseHandle(job)
                raise ctypes.WinError(err)
            return job

        def metrics(self):
            class Accounting(ctypes.Structure):
                _fields_ = [(n, ctypes.c_int64) for n in ("user", "kernel", "period_user", "period_kernel")] + [
                    (n, wintypes.DWORD) for n in ("faults", "total", "active", "terminated")]
            class AccountingAndIO(ctypes.Structure):
                _fields_ = [("BasicInfo", Accounting), ("IoInfo", _IOC)]
            accounting, ext = AccountingAndIO(), _EXT()
            for kind, obj in ((JOB_BASIC_AND_IO_ACCOUNTING_CLASS, accounting), (JOB_EXTENDED_LIMIT_CLASS, ext)):
                if not _k32.QueryInformationJobObject(self.job, kind, ctypes.byref(obj), ctypes.sizeof(obj), None):
                    raise ctypes.WinError(ctypes.get_last_error())
            basic = accounting.BasicInfo
            return ((basic.user + basic.kernel) / 10_000_000, ext.PeakJobMemoryUsed,
                    accounting.IoInfo.rb + accounting.IoInfo.wb)

        def close_and_kill(self):
            if self.job:
                _k32.TerminateJobObject(self.job, 1)
                _k32.CloseHandle(self.job)
                self.job = None
            self.proc.kill()
            self.proc.wait()

        def kill(self, grace=KILL_AFTER_S):
            if self.job:
                _k32.TerminateJobObject(self.job, 1)
            try:
                self.proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()

        def reap_stragglers(self):
            if self.job:
                _k32.TerminateJobObject(self.job, 1)

        def close(self):
            if self.job:
                _k32.CloseHandle(self.job)
                self.job = None


def spawn_worker(argv, out_f, stdin_f, cwd, env):
    """(proc, tree). The worker's stdout and stderr both go to out_f."""
    kw = dict(stdin=stdin_f, stdout=out_f, stderr=subprocess.STDOUT, cwd=cwd, env=env)
    adapter = Windows() if IS_WINDOWS else WSL()
    return adapter.spawn(argv, **kw)


def _proc_cmdlines_linux():
    for e in os.scandir("/proc"):
        if e.name.isdigit():
            try:
                raw = Path(e.path, "cmdline").read_bytes()
            except OSError:
                continue
            yield int(e.name), [a.decode("utf-8", errors="replace") for a in raw.split(b"\0") if a]


def _split_windows_cmdline(line):
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


def detach(cmd, env, log):
    in_job = False
    kw = dict(stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True)
    if IS_WINDOWS:
        # Keep the runner hidden and independent of the hub when its parent job allows breakaway.
        flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        try:
            proc = subprocess.Popen(cmd, env=env, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **kw)
        except OSError:         # the parent's job does not allow breakaway: the runner dies with that job
            in_job = True
            log.write(b"warning: breakaway from the parent job was refused; this runner is still in it\n")
            log.flush()
            proc = subprocess.Popen(cmd, env={**env, "DROVER_DETACHED_IN_JOB": "1"}, creationflags=flags, **kw)
    else:
        proc = subprocess.Popen(cmd, env=env, start_new_session=True, **kw)
    return proc, in_job


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


def vhdx_dir():
    """WSL path of the directory holding this distro's ext4.vhdx, or None."""
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
