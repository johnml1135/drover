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
from contextlib import contextmanager
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


def executable(name, platform=None):
    """Resolve nested commands before crossing platforms; WSL's worker PATH lacks login tools."""
    platform = platform or ("windows" if IS_WINDOWS else "wsl")
    if platform == "wsl":
        path = _settings().get(f"wsl_bin_{name}")
        if path and is_linux_native(path):
            return path
    return name


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
    if path is None:
        raise TypeError("native(None): no path (a missing worktree must not become the string 'None')")
    path = os.path.expanduser(str(path))
    return to_windows_path(path) if IS_WINDOWS else to_wsl_path(path)


def canonical(path):
    return str(path) if routes_through_wsl(path) else str(Path(native(path)).resolve())


def path_parts(path):
    """Resolved components for overlap checks, respecting drive-mount case insensitivity."""
    resolved = path if routes_through_wsl(path) else Path(native(path)).resolve()
    return cleanup_parts(resolved)


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
        prefix = ["-d", name]
        if cd:
            prefix += ["--cd", self.path(cd)]
        if env:
            if any(not re.fullmatch(r"[A-Z][A-Z0-9_]*", str(k)) for k in env):
                raise ValueError("bad environment variable name")
            argv = ["/usr/bin/env", *[f"{k}={self.path(v)}" for k, v in env.items()], *argv]
        return wsl_control(prefix + ["--exec", *argv])

    def spawn(self, argv, **kw):
        proc = subprocess.Popen(argv, start_new_session=True, **kw)
        return proc, PosixTree(proc)


def wsl_control(args):
    command = ["wsl.exe", *args]
    if len(subprocess.list2cmdline(command).encode('utf-16-le')) >= 8192:
        raise ValueError('WSL command exceeds 8 KB; pass payloads through stdin or files')
    return command


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
    _k32.OpenJobObjectW.restype = wintypes.HANDLE
    _k32.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
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


def ci_command(argv, priority):
    """Apply Linux CI resource controls around verbatim argv when user scopes work.

    Interactive work (P0/P1) gets weight 1000, P2 100, P3 30, P4 10.
    Background P4 work also has an 8 GB memory cap and a 30 minute limit.
    Machines without a usable systemd user bus run directly; the runner still
    owns the process tree and its configured wall and idle timeouts.
    """
    if not sys.platform.startswith('linux') or not shutil.which('systemd-run'):
        return list(argv)
    try:
        probe = subprocess.run(['systemd-run', '--user', '--scope', '--quiet', 'true'],
                               capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return list(argv)
    if probe.returncode:
        return list(argv)
    weight = {'P0': 1000, 'P1': 1000, 'P2': 100, 'P3': 30, 'P4': 10}[priority]
    scope = ['systemd-run', '--user', '--scope', '--quiet', '-p', f'CPUWeight={weight}', '-p', f'IOWeight={weight}']
    inner = list(argv)
    if priority == 'P4':
        scope += ['-p', 'MemoryMax=8G']
        inner = ['timeout', '1800', *inner]
    return [*scope, '--', *inner]


def ci_invocation(platform, script, plan_file, python=None):
    """CI entrypoint argv and path mapping for the selected placement."""
    convert = to_wsl_path if platform == "wsl" else native
    interpreter = "python3" if platform == "wsl" else (python or sys.executable)
    return [interpreter, convert(str(script)), "--plan", convert(str(plan_file))]


def ci_path(platform, path):
    return to_wsl_path(str(path)) if platform == "wsl" else str(native(path))


def ci_runner_command(args, drover_dir, distro_name=None, env=None, windows=IS_WINDOWS):
    """Start a WSL runner from Windows, or directly when already on Linux."""
    args = list(args)
    split = args.index('--') if '--' in args else len(args)
    mapped = [to_wsl_path(str(arg)) for arg in args[:split]] + args[split:]
    if windows:
        return WSL(distro_name).command(['python3', '-m', 'drover.runner', *mapped], cd=drover_dir, env=env), None
    child_env = {**os.environ, **(env or {})}
    child_env['PYTHONPATH'] = os.pathsep.join(filter(None, [str(drover_dir), os.environ.get('PYTHONPATH')]))
    return [sys.executable, '-m', 'drover.runner', *mapped], child_env


def native_runner_cwd(path, windows=IS_WINDOWS):
    if not windows:
        return str(path)
    path = to_windows_path(path)
    if is_linux_native(path):
        raise ValueError(f"worktree {path} is not reachable from Windows")
    return path


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


def recover_ci_children(platform, status, drover_dir, distro_name=None):
    """Confirm child termination on the execution's placement, including remote WSL."""
    if IS_WINDOWS and platform == 'wsl':
        code = ('import json,sys; from drover.placement import stop_ci_children; '
                'sys.exit(0 if stop_ci_children(json.loads(sys.argv[1])) else 1)')
        command = WSL(distro_name).command(['python3', '-c', code, json.dumps({key: status.get(key) for key in ('pid', 'runner_pid', 'run_id')})], cd=drover_dir)
        result = subprocess.run(command, capture_output=True, timeout=90,
                                stdin=subprocess.DEVNULL, **NO_WINDOW)
        if result.returncode not in (0, 1):
            raise ScanError('cannot confirm WSL CI child termination')
        return result.returncode == 0
    return stop_ci_children(status)


# ---- cleanup placement facts -----------------------------------------------------------------------------

def worktree_side(path: str, lock: str | None = None) -> str | None:
    """Owning side from explicit marker or unambiguous raw administrative path."""
    if lock in ('windows', 'wsl'):
        return lock
    if lock:
        return None
    text = str(path)
    if WIN_RE.match(text.removeprefix('\\\\?\\')):
        return 'windows'
    if text.startswith('/') and not text.startswith('//') and not MNT_RE.match(text):
        return 'wsl'
    return None


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


def _ordinal_char(char):
    """NTFS-style fold: BMP only, 1:1, and never a non-ASCII character onto ASCII."""
    upper = char.upper()
    if len(upper) != 1 or ord(char) > 0xFFFF or ord(upper) > 0xFFFF or (ord(char) > 0x7F and ord(upper) < 0x80):
        return char
    return upper


def _ordinal_case(text):
    """Windows character case mapping without Unicode multi-character expansion."""
    return ''.join(map(_ordinal_char, text))


def windows_fold_uncertain(text):
    """True if Python's case rules differ from ours for a character NTFS may or may not fold.

    Fail-closed direction: identity is used for the "is this worktree in use / registered"
    evidence.  Over-folding would only make distinct paths look equal (retain); UNDER-folding
    could make a registered or busy worktree look unrelated, i.e. deletable.  We under-fold
    exactly these characters (non-ASCII mapping to ASCII such as dotless i, long s, Kelvin
    sign, and astral letters), so a path containing one yields unknown evidence -> retain.
    Plain multi-character expansions (sharp s, ligatures) are left alone: NTFS never folds them.
    """
    for char in text:
        upper = char.upper()
        if upper != char and _ordinal_char(char) == char and (
                len(upper) == 1 or ord(char) > 0xFFFF):
            return True
    return False


def cleanup_normalize(path, windows=None):
    """Canonical safety identity for either placement; keep raw paths for link checks."""
    import ntpath
    text = str(path)
    windows = bool(drive(text) or text.startswith(('\\\\', '//')) or
                   (IS_WINDOWS and not is_linux_native(text))) if windows is None else windows
    if windows:
        text = to_windows_path(text).replace('/', '\\')
        if text.lower().startswith('\\\\?\\unc\\'):
            text = '\\\\' + text[8:]
        elif text.startswith('\\\\?\\'):
            text = text[4:]
        return _ordinal_case(ntpath.normpath(text).replace('\\', '/').rstrip('/'))
    import posixpath
    # Only the owning Linux worker can resolve Linux links; the Windows
    # coordinator preserves their case and leaves link validation to that worker.
    return posixpath.normpath(text) if IS_WINDOWS else posixpath.realpath(text)


def _process_os_location(exe, windows):
    if not exe:
        return True
    roots = ([os.environ.get('SystemRoot', r'C:\Windows'),
              os.environ.get('ProgramFiles', r'C:\Program Files'),
              os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'),
              os.environ.get('ProgramData', r'C:\ProgramData') + r'\Microsoft',
              r'C:\Windows\SystemApps'] if windows else ['/usr', '/bin', '/sbin', '/lib', '/lib64', '/opt'])
    path = cleanup_normalize(exe, windows)
    return any(path.startswith(cleanup_normalize(root, windows).rstrip('/') + '/') for root in roots)


_PROCESS_IGNORE_R11_WINDOWS = ['ctfmon.exe', 'TabTip.exe', 'TextInputHost.exe', 'DefenderSessionHelper.exe',
                               'Wacom_TouchUser.exe', 'Taskmgr.exe', 'SecurityHealthSystray.exe', 'sihost.exe']
_PROCESS_IGNORE_WSL = ['systemd', '(sd-pam)']


def default_process_ignore():
    """Owner-editable non-workspace helpers; shells are deliberately excluded."""
    return [*_PROCESS_IGNORE_R11_WINDOWS,
            'csrss.exe', 'winlogon.exe', 'dwm.exe', 'fontdrvhost.exe', 'NVDisplay.Container.exe',
            'RtkAudUService64.exe', 'WacomHost.exe', 'Wacom_Tablet.exe', 'remoting_desktop.exe', *_PROCESS_IGNORE_WSL]


def seed_cleanup_process_policy(policy):
    """Upgrade the exact prior factory list; preserve every owner-modified list."""
    previous = [name for name in default_process_ignore() if name != 'remoting_desktop.exe']
    if 'process_ignore' not in policy or policy['process_ignore'] in (
            _PROCESS_IGNORE_R11_WINDOWS + _PROCESS_IGNORE_WSL, previous):
        policy['process_ignore'] = default_process_ignore()
    policy.setdefault('process_query_timeout_s', 240)


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


def cleanup_processes(*, proc_root=None, cim=None, windows=None, request=None):
    """Snapshot invoking-user visibility; foreign processes still contribute path evidence."""
    proof = _CleanupProcesses(proc_root=proc_root, cim=cim, windows=windows, request=request)
    if request is not None:
        diagnostics = request.setdefault('_process_diagnostics', {})
        for row in proof.unreadable:
            diagnostics[(row['pid'], row['name'])] = row
        if proof.query_failure:
            request.setdefault('_process_query_failures', []).append(proof.query_failure)
    return proof


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
    """One CIM query supplies owner SIDs alongside argv/exe evidence."""
    script = r'''$ErrorActionPreference = 'Stop'
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$rows = @(Get-CimInstance Win32_Process -Property ProcessId,ParentProcessId,Name,ExecutablePath,CommandLine,SessionId | ForEach-Object {
    $ownerSid = $null
    try { $owner = Invoke-CimMethod -InputObject $_ -MethodName GetOwnerSid
          if ($owner.ReturnValue -eq 0) { $ownerSid = $owner.Sid } } catch {}
    [pscustomobject]@{ProcessId=$_.ProcessId; Name=$_.Name; ExecutablePath=$_.ExecutablePath;
                     CommandLine=$_.CommandLine; SessionId=$_.SessionId; OwnerSid=$ownerSid}
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


def _windows_birth(pid):
    """Native FILETIME, shared by process records and cache identity checks."""
    import ctypes as c
    from ctypes import wintypes as w
    kernel = c.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.GetProcessTimes.argtypes = [w.HANDLE, *([c.POINTER(w.FILETIME)] * 4)]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        times = [w.FILETIME() for _ in range(4)]
        if kernel.GetProcessTimes(handle, *(c.byref(t) for t in times)):
            return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


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


class _CleanupProcesses:
    """One invoking-user snapshot. Unknown ownership fails closed; foreign argv still retains named paths."""

    def __init__(self, proc_root=None, windows=None, cim=None, request=None):
        self.rows, self.unreadable, self.out_of_scope, self.unknown = [], [], 0, False
        self.windows = IS_WINDOWS if windows is None else windows
        self.ignored = (request or {}).get('paths', {}).get('process_ignore', default_process_ignore())
        self.ignored = {_ordinal_case(name) if self.windows else name for name in self.ignored}
        self.query_failure = None
        self.request = request or {}
        if self.windows:
            try:
                current = _windows_processes() if not cim and request else None
                self.current = current
                answer = dict(rows=cim()) if cim else _cleanup_cim(request, current)
                self.query_failure = {k: v for k, v in answer.items() if k not in ('rows', 'identities')} or None
                rows = answer['rows']
                self.unknown = bool(self.query_failure) or (not rows and current is None)
                if current is not None:
                    known = {str(row['pid']): row for row in rows}
                    baseline = answer.get('identities') or {}
                    refresh = []
                    for pid, identity in current.items():
                        if pid in known and identity[1] and baseline.get(pid) == identity:
                            continue
                        refresh.append(pid)
                    rows = list(known.values())
                    if refresh:
                        # Never inherit ownership/argv from a PID whose birth is unknown or changed.
                        started = time.monotonic()
                        try:
                            fresh = {str(row['pid']): row for row in _cim_rows(
                                self.request.get('paths', {}).get('process_query_timeout_s', 240), refresh)}
                        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
                            fresh = {}
                            self.query_failure = dict(error=str(error), elapsed_s=time.monotonic() - started,
                                reason_code='process_query_timeout' if isinstance(error, subprocess.TimeoutExpired)
                                else 'process_visibility_unavailable')
                        rows = [row for row in rows if str(row['pid']) not in refresh]
                        try:
                            after = _windows_processes()
                        except OSError as error:
                            after = {}
                            self.query_failure = dict(reason_code='process_visibility_unavailable', error=str(error))
                        fresh = {pid: row for pid, row in fresh.items()
                                 if pid in refresh and current[pid][1]
                                 and after.get(pid) == current[pid]}
                        rows.extend(fresh.values())
                        for pid in refresh:
                            if pid not in fresh:
                                self.unknown = True
                                self._unreadable(int(pid), current[pid][0], evidence='process_identity_unavailable')
                    rows = [row for row in rows if str(row['pid']) in current]
                for row in rows:
                    # Refresh readable cwd evidence even when reusing the run's CIM snapshot.
                    self._record(**dict(row, cwd=row.get('cwd') if 'cwd' in row else _windows_cwd(row['pid'])))
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
                self.unknown = True
                self.query_failure = dict(reason_code='process_visibility_unavailable', error=str(error))
            return
        root = Path(proc_root if proc_root is not None else '/proc')
        try:
            entries = list(root.iterdir())
        except OSError:
            self.unknown = True
            return
        for entry in entries:
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            same_user, name = None, entry.name
            try:
                fields = dict(line.split(':', 1) for line in (entry / 'status').read_text().splitlines() if ':' in line)
                same_user = int(fields['Uid'].split()[0]) == os.getuid()
                name = fields.get('Name', name).strip()
            except (OSError, ValueError, KeyError, IndexError):
                if not entry.exists():
                    continue
            try:
                cmd = [arg.decode(errors='replace') for arg in (entry / 'cmdline').read_bytes().split(b'\0') if arg]
            except OSError:
                cmd = None
            command_unreadable = cmd is None
            cmd = cmd or ''
            exe = None
            try:
                exe = str((entry / 'exe').resolve(strict=True))
            except OSError:
                pass
            try:
                cwd = str((entry / 'cwd').resolve(strict=True))
            except OSError:
                if not entry.exists():
                    continue
                # Kernel threads have no userspace cwd/argv. A known foreign UID
                # can be excluded; unknown ownership remains conservative.
                cwd = None
            self._record(pid=int(entry.name), name=name, same_user=same_user, cwd=cwd, cmd=cmd,
                         exe=exe, command_unreadable=command_unreadable)

    def _unreadable(self, pid, name, **extra):
        row = dict(pid=pid, name=name, **extra)
        if extra.get('evidence') in (None, 'owner') and extra.get('cwd_unreadable', True):
            row['owner_action'] = f'close {name} (pid {pid}) or add it to [disk_cleanup] process_ignore'
        self.unreadable.append(row)

    def _record(self, *, pid, name, same_user, cwd, cmd, session_id=None, exe=None, command_unreadable=False):
        command_unreadable = command_unreadable or cmd is None
        own_cwd = self._own_cwd(pid) if not cwd or same_user is None or command_unreadable else None
        if own_cwd:
            cwd, same_user = cwd or own_cwd, True
        image = re.split(r'[/\\]', name or '')[-1]
        image = _ordinal_case(image) if self.windows else image
        ignored = image in self.ignored and _process_os_location(exe, self.windows)
        foreign = same_user is False or (self.windows and same_user is None and session_id == 0)
        if ignored and (same_user is None or not cwd):
            self.out_of_scope += 1
        elif self.windows and same_user is None and not foreign:
            self.unknown = True
            self._unreadable(pid, name, evidence='owner', cwd_unreadable=not bool(cwd))
        elif not cwd:
            if foreign:
                self.out_of_scope += 1
            else:
                self.unknown = True
                self._unreadable(pid, name)
        elif command_unreadable and not foreign:
            self.unknown = True
            self._unreadable(pid, name, evidence='command_line')
        self.rows.append((cwd, cmd, exe))

    def _own_cwd(self, pid):
        if not self.request.get('state_dir'):
            return None
        side = 'windows' if self.windows else 'wsl'
        path = Path(self.request['state_dir']) / 'disk' / f'process-{side}-{pid}.json'
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
            identity = record['identity']
            if self.windows and getattr(self, 'current', None) is not None:
                birth = (self.current.get(str(pid)) or [None, None])[1]
                current = dict(pid=pid, side=side, birth=str(birth) if birth else None)
            else:
                current = cleanup_identity(pid)
            if (identity.get('pid') == pid and identity.get('side') == side
                    and identity.get('birth') not in (None, '', 'absent')
                    and current == identity):
                return record['cwd']
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            pass
        return None

    @property
    def reason(self):
        return 'process visibility unavailable' if self.unknown else None

    def busy(self, worktree):
        if self.unknown:
            return True
        if self.windows and windows_fold_uncertain(str(worktree)):
            return True  # identity cannot be proven; unknown evidence retains
        worktree = cleanup_normalize(worktree, self.windows)
        def names(path):
            value = cleanup_normalize(path, self.windows)
            return value == worktree or value.startswith(worktree.rstrip('/') + '/')
        for cwd, cmd, exe in self.rows:
            if any(names(path) for path in (cwd, exe) if path):
                return True
            # Preserve quoted paths with spaces and option=value paths. Every path
            # goes through the same identity normalization as cwd and executable.
            if isinstance(cmd, list):
                tokens = cmd
            elif self.windows:
                tokens = _split_windows_cmdline(cmd) if cmd else []
            else:
                tokens = [next(value for value in match.groups() if value is not None)
                          for match in re.finditer(r"\"([^\"]*)\"|'([^']*)'|([^\s\"']+)", cmd or '')]
            for token in tokens:
                for value in (token, token.partition('=')[2]):
                    if value and (drive(value) or value.startswith(('/', '\\\\'))) and names(value):
                        return True
        return False



_METADATA_IO = r'''
import json, os, sys, tempfile
from pathlib import Path
path = Path(sys.argv[1]) / sys.argv[2]
content = json.load(sys.stdin)
if content is None:
    try:
        print(path.read_text(encoding='utf-8'), end='')
    except FileNotFoundError:
        pass
else:
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', delete=False) as stream:
            name = stream.name
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        name = None
        print(content, end='')
    finally:
        if name is not None:
            os.unlink(name)
'''


def worktree_metadata(worktree: str, filename: str, content: str | None = None) -> str | None:
    """Read/write a linked worktree's admin file on its owning host, replacing atomically."""
    if not filename or filename in ('.', '..') or any(c in filename for c in '/\\\0:'):
        raise ValueError('metadata filename must be a basename')
    try:
        admin = git(worktree, 'rev-parse', '--absolute-git-dir', timeout=30)
        common = git(worktree, 'rev-parse', '--path-format=absolute', '--git-common-dir', timeout=30)
        if admin.returncode or common.returncode or not admin.stdout.strip() or \
                admin.stdout.strip() == common.stdout.strip():
            raise OSError('cannot resolve linked worktree admin directory')
        remote = routes_through_wsl(worktree)
        directory = admin.stdout.strip() if remote else native(admin.stdout.strip())
        argv = ['python3' if remote else sys.executable, '-c', _METADATA_IO, directory, filename]
        if remote:
            argv = WSL().command(argv)
        result = subprocess.run(argv, input=json.dumps(content), text=True, encoding='utf-8',
                                capture_output=True, timeout=30, **NO_WINDOW)
        if result.returncode:
            raise OSError(result.stderr.strip() or 'worktree metadata IO failed')
        return result.stdout or None
    except (OSError, ValueError, subprocess.SubprocessError):
        if content is not None:
            raise
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
                row['owner_action'] = (f'wsl --shutdown, then wsl --manage {distro_arg} --set-sparse true '
                                       '(and compact once with Optimize-VHD)')
        except OSError as error:
            row['error'] = str(error)
        rows.append(row)
    return rows


def trim_argv():
    """No privilege prompts: Windows runs the pinned WSL distro's root trim."""
    if IS_WINDOWS:
        return wsl_control(['-d', distro(), '-u', 'root', '--', 'fstrim', '-v', '/'])
    return ['fstrim', '-v', '/'] if os.geteuid() == 0 else None


def cleanup_runtime(cfg):
    from .disk import _free
    from .disk_cleanup import CleanupRuntime
    return CleanupRuntime(time.time, _free, cleanup_call, disk_toast)


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
    from . import disk_side, disk_state
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
    status = dict(pid=os.getpid(), runner_pid=os.getpid(), run_id=request['_worker_id'])
    name = 'worker-' + request['_worker_id'] + '.json'
    disk_state.save_file(request['state_dir'], name, status)
    try:
        answer = disk_side.serve(request)
    except BaseException as error:
        disk_state.save_file(request['state_dir'], name, dict(status, error=str(error)))
        raise
    disk_state.save_file(request['state_dir'], name, dict(status, completed=True, result=answer))
    json.dump(answer, sys.stdout)


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


def cleanup_call(side: str, request: dict) -> dict:
    """Run a fixed JSON worker; only declared paths are translated across sides."""
    if side not in ('windows', 'wsl'):
        raise ValueError('invalid cleanup side')
    if not IS_WINDOWS and side == 'windows':
        errors = [dict(reason_code='side_unavailable')] if os.environ.get('WSL_DISTRO_NAME') else []
        return dict(items=[], errors=errors, reclaimed_bytes=0)
    import copy
    import uuid
    from . import disk_state
    request = copy.deepcopy(request)
    original_state = request['state_dir']
    token = uuid.uuid4().hex
    request['_worker_id'] = token
    request['side'] = side
    root = str(Path(__file__).resolve().parents[1])
    if IS_WINDOWS and side == 'wsl':
        convert = to_wsl_path
        request['state_dir'] = convert(request['state_dir'])
        snapshot = request.get('tasks') or {}
        if snapshot:
            snapshot['active_worktrees'] = [convert(p) for p in snapshot.get('active_worktrees', [])]
            for task in snapshot.get('tasks', []):
                if task.get('worktree'):
                    task['worktree'] = convert(task['worktree'])
        for repo in request['repos']:
            for key in ('repo', 'worktrees'):
                if repo.get(key):
                    repo[key] = convert(repo[key])
        for key in ('temp_roots', 'report_roots'):
            request['paths'][key] = [convert(p) for p in request['paths'].get(key, [])
                                     if cleanup_path_side(p) == side]
        request['paths']['caches'] = [c for c in request['paths'].get('caches', [])
                                      if cleanup_path_side(c.get('path', c.get('cwd'))) == side]
        for cache in request['paths'].get('caches', []):
            key = 'path' if 'path' in cache else 'cwd'
            cache[key] = convert(cache[key])
        argv = wsl_control(['-d', distro(), '--', 'env', 'DROVER_RUN_ID=' + token, 'python3'])
        root = convert(root)
    else:
        argv = [sys.executable]
    # Pin the application root independently of cwd or the hub's environment.
    code = ('import json,sys;sys.path.insert(0,sys.argv[1]);'
            'from drover.placement import cleanup_worker;cleanup_worker(json.load(sys.stdin))')
    job, done, status, answer, pending = None, None, None, None, None
    evidence = {}
    reason, output, detail = 'worker_failed', '', ''
    confirmed, startup_failure = False, False
    def text(value):
        return value.decode(errors='replace') if isinstance(value, bytes) else value or ''
    try:
        job = _CleanupJob(token) if IS_WINDOWS and side == 'windows' else None
        command = [*argv, '-c', code, root, token]
        if IS_WINDOWS and side == 'wsl':
            command = wsl_control(command[1:])
        done = subprocess.run(command, input=json.dumps(request),
                              capture_output=True, text=True, timeout=request['paths'].get('worker_timeout_s', 1800), shell=False,
                              env={**os.environ, 'DROVER_RUN_ID': token})
        output, detail = done.stdout, text(done.stderr)
        if not done.returncode:
            answer = json.loads(done.stdout)
            if not isinstance(answer, dict):
                answer = None
                raise ValueError('invalid cleanup worker result')
    except subprocess.TimeoutExpired as error:
        reason, output, detail = 'worker_timeout', error.stdout, text(error.stderr)
    except OSError as error:
        reason, detail = 'worker_unavailable', str(error)
    except BaseException as error:
        detail = str(error) or type(error).__name__
        # Invalid JSON remains an ordinary worker failure; cancellation and decode
        # failures propagate only after the containment has been drained.
        if not isinstance(error, ValueError) or isinstance(error, UnicodeError):
            pending = error
    finally:
        try:
            try:
                try:
                    status = json.loads((Path(original_state) / 'disk' / ('worker-' + token + '.json')).read_text(encoding='utf-8'))
                except FileNotFoundError:
                    # The identity file is the destructive-work claim: cleanup_worker
                    # must persist it before entering serve(). A completed, failed
                    # subprocess with no claim never entered any stage. run() reaped
                    # it; unlike timeout/cancellation this proves startup termination.
                    if done is None or not done.returncode:
                        raise
                    startup_failure = True
                    reason = 'worker_error'
                    status = None
                if not startup_failure and (not isinstance(status, dict) or type(status.get('pid')) is not int or status['pid'] <= 0 or \
                        status.get('run_id') != token):
                    raise ValueError('worker identity unavailable')
            finally:
                # Drain the job even if reading worker identity is interrupted.
                if job:
                    confirmed = job.terminate()
            if startup_failure and not job:
                if IS_WINDOWS and side == 'wsl':
                    # Reaping wsl.exe alone does not prove the Linux worker exited.
                    # Verify absence of this execution's marker on the owning side.
                    proof = subprocess.run(WSL().command(['python3', '-c', _CLEANUP_STARTUP_PROOF, token]),
                                           capture_output=True, timeout=30, **NO_WINDOW)
                    confirmed = proof.returncode == 0
                else:
                    confirmed = True
            elif not job:
                confirmed = recover_ci_children(side, status, str(Path(__file__).resolve().parents[1]))
        except BaseException as error:
            confirmed = False
            detail += '; termination: ' + (str(error) or type(error).__name__)
        finally:
            if job:
                try:
                    job.close()
                except BaseException as error:
                    confirmed = False
                    detail += '; job close: ' + (str(error) or type(error).__name__)
        if not confirmed or answer is None or pending is not None:
            evidence = dict(reason_code='worker_termination_unconfirmed' if answer is not None else reason,
                            output=text(output)[-4000:], error=text(detail)[-4000:],
                            termination_confirmed=confirmed, work_started=not startup_failure)
            try:
                disk_state.save_file(original_state, 'worker-error-' + token + '.json', evidence)
            except BaseException as error:
                evidence['persistence_error'] = str(error) or type(error).__name__
                if confirmed and not isinstance(error, OSError):
                    raise
            if not confirmed:
                raise CleanupWorkerError(json.dumps(evidence))
    if pending is not None:
        raise pending
    if answer is not None:
        return answer
    return dict(items=[], errors=[evidence], reclaimed_bytes=0)


def cleanup_side():
    return 'windows' if IS_WINDOWS else 'wsl'


def cleanup_path(path):
    """Administrative spelling only: preserve links and even link/.. components."""
    return str(path) if routes_through_wsl(path) else str(Path(native(path)).absolute())


def cleanup_parts(path):
    from pathlib import PurePosixPath
    return PurePosixPath(cleanup_normalize(path)).parts


def cleanup_link(path):
    """Symlinks, Windows junctions and reparse points never authorize traversal."""
    import stat
    info = Path(path).lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def cleanup_scope(request):
    """Keep only this physical drive; path spelling/translation lives here."""
    import copy
    scoped = copy.deepcopy(request)
    scoped['scope_errors'] = []
    if physical_disk_path(request['drive']['path']) is None:
        scoped['scope_errors'].append(dict(path=request['drive']['path'], reason_code='physical_disk_unknown'))
    def belongs(path):
        if physical_disk_path(path) is None or physical_disk_path(request['drive']['path']) is None:
            scoped['scope_errors'].append(dict(path=str(path), reason_code='physical_disk_unknown'))
            return False
        return cleanup_contains(request, path)
    # Repositories may have worktrees on another drive. Stages scope every candidate,
    # rather than dropping the repository that owns that drive's registrations.
    for name in ('temp_roots', 'report_roots'):
        roots = request['paths'].get(name, [])
        scoped['paths'][name] = list(dict.fromkeys(native(p) for p in roots if belongs(p)))
    scoped['paths']['caches'] = [entry for entry in scoped['paths'].get('caches', [])
                                if entry['placement'] == cleanup_side() and belongs(entry.get('path', entry.get('cwd')))]
    for entry in scoped['paths']['caches']:
        key = 'path' if 'path' in entry else 'cwd'
        entry[key] = native(entry[key])
    return scoped


def cleanup_path_side(path):
    return 'windows' if drive(str(path)) else worktree_side(str(path))


def cleanup_contains(request, path):
    """Whether this concrete candidate belongs to the requested physical drive."""
    if cleanup_path_side(path) != cleanup_side():
        return False
    declared = request['drive']['path']
    host = physical_disk_path(path)
    drive_host = physical_disk_path(declared) if declared is not None else None
    if host is not None and drive_host is not None:
        return cleanup_normalize(host) == cleanup_normalize(drive_host)
    return False


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
    try:
        done = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                              input=json.dumps(notice), capture_output=True, text=True, timeout=15, shell=False)
    except subprocess.TimeoutExpired as error:
        raise OSError('toast delivery timed out') from error
    if done.returncode:
        raise OSError('toast unavailable: ' + done.stderr[-1000:])


def cleanup_identity(pid=None):
    """PID plus kernel creation identity; unknown never proves a claim dead."""
    pid = os.getpid() if pid is None else pid
    if IS_WINDOWS:
        done = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                               f'$ErrorActionPreference="Stop"; $p=Get-CimInstance Win32_Process -Filter "ProcessId={int(pid)}"; if ($null -eq $p) {{ "absent" }} else {{ $p.CreationDate.ToFileTimeUtc() }}'],
                              capture_output=True, text=True, timeout=15, **NO_WINDOW)
        birth = done.stdout.strip() if not done.returncode else None
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
    return dict(pid=pid, birth=birth, side=cleanup_side())


def cleanup_claim_alive(identity):
    if (identity.get('side') != cleanup_side() or type(identity.get('pid')) is not int
            or identity['pid'] <= 0 or not identity.get('birth')):
        return None
    if identity.get('birth') == 'absent':
        return False
    if identity['side'] == 'wsl' and ':' not in identity['birth']:
        return None  # Old start ticks without boot identity cannot prove a process dead.
    current = cleanup_identity(identity['pid'])
    return None if not current['birth'] else current['birth'] == identity['birth']


def cleanup_enqueue(cfg, claim):
    """Detached fixed cleanup entry point pinned independently of the hub cwd."""
    code = ('import json,sys;sys.path.insert(0,sys.argv[1]);'
            'from drover.disk_cleanup import run;'
            'run(json.loads(sys.argv[2]),mode="triggered",apply=sys.argv[3]=="1")')
    argv = [sys.executable, '-c', code, str(Path(__file__).resolve().parents[1]),
            json.dumps(cfg), '1' if claim['applied'] else '0', claim['id']]
    from . import config
    log = Path(config.state_dir(cfg)) / 'disk' / 'cleanup.log'
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open('ab') as stream:
        proc, _ = detach(argv, os.environ.copy(), stream)
    return dict(identity=cleanup_identity(proc.pid))


def cleanup_free(path):
    """Read the owning side's physical drive, never a virtual filesystem substitute."""
    physical = physical_disk_path(str(path))
    if physical is None:
        return None
    try:
        return shutil.disk_usage(physical).free / 1024 ** 3
    except OSError:
        return None


def add_worktree(repo, dest, *tail, run=None, error=RuntimeError):
    """Create a worktree; old git may use absolute pointers only on a native filesystem."""
    if run is None:
        def run(repo, *args, check=False):
            return git(repo, *args, timeout=60)
    r = run(repo, '-c', 'core.autocrlf=false', 'worktree', 'add', '--relative-paths', *tail, check=False)
    rejected = re.search(r'(unknown|unrecognized|unsupported|invalid) (option|argument|switch)[^\n]*relative-paths',
                         r.stderr, re.IGNORECASE)
    if r.returncode and rejected:
        if not is_linux_native(dest):
            raise error('this git has no `worktree add --relative-paths`, and a worktree under '
                        f'{dest} is opened from both Windows and WSL: update git (2.48 or newer)')
        r = run(repo, '-c', 'core.autocrlf=false', 'worktree', 'add', *tail, check=False)
    if r.returncode:
        raise error(f'git worktree add failed: {r.stderr.strip()[-300:]}')

    lock = run(repo, 'worktree', 'lock', '--reason',
               'wsl' if routes_through_wsl(dest) or not IS_WINDOWS else 'windows', str(dest), check=False)
    if lock.returncode:
        raise error(f'created {dest} but could not set placement marker: {lock.stderr.strip()[-300:]}')


def disk_schedule_plan(cfg: dict, *, apply: bool = False) -> dict:
    """Generate only: pin interpreter, import root, machine home and shared state."""
    if not IS_WINDOWS and os.environ.get('WSL_DISTRO_NAME'):
        raise ValueError('Install disk schedules using native Windows Python so the physical reader survives WSL shutdown')
    from . import config
    import datetime
    import getpass
    import xml.etree.ElementTree as ET
    files, commands = {}, []
    root = str(Path(__file__).resolve().parents[1])
    home, state = str(config.home().absolute()), str(config.state_dir(cfg).absolute())
    code = ('import os,runpy,sys;sys.path.insert(0,sys.argv[1]);'
            'os.environ["DROVER_HOME"]=sys.argv[2];'
            'os.environ["DROVER_PATHS_STATE_DIR"]=sys.argv[3];'
            'sys.argv=["drover",*sys.argv[4:]];runpy.run_module("drover",run_name="__main__")')
    mode = '--apply' if apply else '--dry-run'
    actions = [('DroverDiskTracker', 'drover-disk-tracker', ['disk-tracker', 'tick', mode]),
               ('DroverDiskCleanup', 'drover-disk-cleanup', ['disk-cleanup', '--mode', 'daily', mode])]
    for name, unit, args in actions:
        argv = [str(Path(sys.executable).absolute()), '-c', code, root, home, state, *args]
        if IS_WINDOWS:
            ns = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
            ET.register_namespace('', ns)
            def node(parent, tag, text=None, **attributes):
                element = ET.SubElement(parent, '{' + ns + '}' + tag, attributes)
                element.text = text
                return element
            task = ET.Element('{' + ns + '}Task', version='1.4')
            triggers = node(task, 'Triggers')
            start = datetime.datetime.now().date().isoformat() + 'T03:00:00'
            if unit.endswith('tracker'):
                trigger = node(triggers, 'TimeTrigger')
                repetition = node(trigger, 'Repetition')
                node(repetition, 'Interval', 'PT15M')
                node(repetition, 'StopAtDurationEnd', 'false')
            else:
                trigger = node(triggers, 'CalendarTrigger')
            node(trigger, 'StartBoundary', start)
            node(trigger, 'Enabled', 'true')
            if not unit.endswith('tracker'):
                node(node(trigger, 'ScheduleByDay'), 'DaysInterval', '1')
            user = os.environ.get('USERNAME') or getpass.getuser()
            domain = os.environ.get('USERDOMAIN')
            user = domain + chr(92) + user if domain else user
            principal = node(node(task, 'Principals'), 'Principal', id='CurrentUser')
            node(principal, 'UserId', user)
            node(principal, 'LogonType', 'InteractiveToken')
            node(principal, 'RunLevel', 'LeastPrivilege')
            settings = node(task, 'Settings')
            for key, value in [('MultipleInstancesPolicy', 'IgnoreNew'), ('StartWhenAvailable', 'true'),
                               ('DisallowStartIfOnBatteries', 'false'), ('StopIfGoingOnBatteries', 'false'),
                               ('ExecutionTimeLimit', 'PT0S')]:
                node(settings, key, value)
            action = node(node(task, 'Actions', Context='CurrentUser'), 'Exec')
            node(action, 'Command', argv[0])
            node(action, 'Arguments', subprocess.list2cmdline(argv[1:]))
            node(action, 'WorkingDirectory', root)
            path = str(Path(state) / 'disk' / (name + '.xml'))
            files[path] = ET.tostring(task, encoding='unicode', xml_declaration=True)
            commands.append(['schtasks.exe', '/Create', '/TN', name, '/XML', path, '/F'])
        else:
            directory = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'systemd' / 'user'
            def quote(value):
                # systemd performs its own percent and dollar expansion, without a shell.
                return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$').replace('\n', '\\n').replace('\r', '\\r') + '"'
            command = ' '.join(quote(value) for value in argv)
            files[str(directory / (unit + '.service'))] = (
                '[Unit]\nDescription=' + name + '\n[Service]\nType=oneshot\nExecStart=' + command + '\n')
            calendar = '*-*-* *:0/15:00' if unit.endswith('tracker') else '*-*-* 03:00:00'
            files[str(directory / (unit + '.timer'))] = (
                '[Unit]\nDescription=' + name + '\n[Timer]\nOnCalendar=' + calendar +
                '\nPersistent=true\n[Install]\nWantedBy=timers.target\n')
    if not IS_WINDOWS:
        commands = [['systemctl', '--user', 'daemon-reload'],
                    ['systemctl', '--user', 'enable', '--now', 'drover-disk-tracker.timer', 'drover-disk-cleanup.timer']]
    return dict(files=files, argv=commands)


def unlock_worktree(repo, path):
    """Release only this creator's side marker; owner notes remain locked."""
    expected = 'wsl' if routes_through_wsl(path) or not IS_WINDOWS else 'windows'
    listed = git(repo, 'worktree', 'list', '--porcelain', '-z', timeout=60)
    if listed.returncode:
        return
    for block in listed.stdout.split('\0\0'):
        fields = dict(field.partition(' ')[::2] for field in block.split('\0') if field)
        if fields.get('worktree') and cleanup_normalize(fields['worktree']) == cleanup_normalize(path) and fields.get('locked') == expected:
            git(repo, 'worktree', 'unlock', str(path), timeout=60)
