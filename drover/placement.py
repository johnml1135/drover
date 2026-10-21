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
    if not status.get('pid'):
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
        command = WSL(distro_name).command(['python3', '-c', code, json.dumps(status)], cd=drover_dir)
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
    return '/'


def _cleanup_path(path, windows):
    text = str(path).strip().strip('"\'')
    if windows:
        import ntpath
        return ntpath.normpath(text).replace('\\', '/').rstrip('/').casefold()
    return str(Path(text).resolve()).rstrip('/') or '/'


def cleanup_processes(*, proc_root=None, cim=None, windows=None):
    """Snapshot once; unknown process visibility always retains candidates."""
    return _CleanupProcesses(proc_root=proc_root, cim=cim, windows=windows)


def _cim_rows():
    """(cwd, command line) for every process via CIM; raises on failure or timeout. CIM exposes no cwd."""
    out = subprocess.run(
        ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
         'Get-CimInstance Win32_Process | Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress'],
        capture_output=True, text=True, timeout=30, errors='replace', **NO_WINDOW)
    if out.returncode or not out.stdout.strip():
        raise OSError('CIM query failed: ' + out.stderr.strip()[:200])
    data = json.loads(out.stdout)
    data = [data] if isinstance(data, dict) else data
    return [(None, ' '.join(filter(None, [d.get('ExecutablePath'), d.get('CommandLine')]))) for d in data
            if d.get('ProcessId') != os.getpid()]


class _CleanupProcesses:
    """One snapshot of running processes per run. A worktree is busy when any process has its cwd there or names its
    path on its command line. If the snapshot cannot be taken (no /proc, CIM failed or timed out) everything is
    busy: unknown fails safe, and keeps the worktree.

    `windows` forces the CIM path (tests inject `cim`); the default follows os.name.
    """

    def __init__(self, proc_root=None, windows=None, cim=None):
        self.rows, self.unknown = [], False
        windows = IS_WINDOWS if windows is None else windows
        self.windows = windows
        if windows:
            try:
                self.rows = (cim or _cim_rows)()
            except (OSError, ValueError, subprocess.SubprocessError):
                self.unknown = True
            return
        self.root = Path(proc_root if proc_root is not None else '/proc')
        if not self.root.is_dir():
            self.unknown = True
            return
        try:
            entries = list(self.root.iterdir())
        except OSError:
            self.unknown = True
            return
        for entry in entries:
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            try:
                cmd = (entry / 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
            except FileNotFoundError:
                if entry.exists():
                    self.unknown = True
                continue
            except OSError:
                self.unknown = True
                cmd = None
            try:
                cwd = str((entry / 'cwd').resolve(strict=True))
            except FileNotFoundError:
                # Kernel threads have neither userspace argv nor cwd; live userspace
                # processes with invisible cwd do not provide an idle proof.
                if cmd and entry.exists():
                    self.unknown = True
                continue
            except OSError:
                self.unknown = True
                cwd = None
            self.rows.append((cwd, cmd))

    def busy(self, worktree):
        if self.unknown:
            return True
        worktree = _cleanup_path(worktree, self.windows)
        for cwd, cmd in self.rows:
            if cwd is not None and (_cleanup_path(cwd, self.windows) == worktree or _cleanup_path(cwd, self.windows).startswith(worktree + '/')):
                return True
            if cmd is None and cwd is None:
                return True  # cannot prove safety
            if cmd:
                text = cmd.replace('\\', '/')
                text = text.casefold() if self.windows else text
                target = worktree.casefold() if self.windows else worktree
                if re.search(r"(?:^|[\s\"'=])" + re.escape(target) + r'(?=$|[/\s"\'])', text):
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
CLEANUP_SCRATCH_ROOTS = ('~/.claude/scratchpad', '~/.codex/scratchpad',)


def detected_cleanup_policy(state_dir):
    """Editable machine defaults for installed tools and existing scratch roots."""
    import tempfile
    side = 'windows' if IS_WINDOWS else 'wsl'
    state = Path(state_dir).expanduser().absolute()
    roots = [tempfile.gettempdir(), str(state / 'scratch')]
    roots.extend(str(Path(p).expanduser().absolute()) for p in CLEANUP_SCRATCH_ROOTS
                 if Path(p).expanduser().is_dir())
    return dict(temp_roots=list(dict.fromkeys(roots)), report_roots=[], caches=[
        dict(argv=list(argv), cwd=str(state), placement=side)
        for tool, argv in CLEANUP_CACHE_COMMANDS if shutil.which(tool)])


def trim_argv():
    """No privilege prompts: Windows runs the pinned WSL distro's root trim."""
    if IS_WINDOWS:
        return ['wsl.exe', '-d', distro(), '-u', 'root', '--', 'fstrim', '-v', '/']
    return ['fstrim', '-v', '/'] if os.geteuid() == 0 else None


def cleanup_runtime(cfg):
    from .disk import _free
    from .disk_cleanup import CleanupRuntime
    return CleanupRuntime(time.time, _free, cleanup_call, disk_toast)


def cleanup_call(side: str, request: dict) -> dict:
    """Run a fixed JSON worker; only declared paths are translated across sides."""
    if side not in ('windows', 'wsl'):
        raise ValueError('invalid cleanup side')
    if not IS_WINDOWS and side == 'windows':
        return dict(items=[], errors=[dict(reason_code='side_unavailable')], reclaimed_bytes=0)
    import copy
    import tempfile
    request = copy.deepcopy(request)
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
            request['paths'][key] = [convert(p) for p in request['paths'].get(key, [])]
        for cache in request['paths'].get('caches', []):
            cache['cwd'] = convert(cache['cwd'])
        argv = ['wsl.exe', '-d', distro(), '--', 'python3']
        root = convert(root)
    else:
        argv = [sys.executable]
    # Pin the application root independently of cwd or the hub's environment.
    code = 'import runpy,sys;sys.path.insert(0,sys.argv[1]);runpy.run_module("drover.disk_side",run_name="__main__")'
    done = subprocess.run([*argv, '-c', code, root], input=json.dumps(request),
                          capture_output=True, text=True, timeout=600, shell=False)
    if done.returncode:
        raise RuntimeError('cleanup worker failed: ' + done.stderr[-2000:])
    return json.loads(done.stdout)


def cleanup_side():
    return 'windows' if IS_WINDOWS else 'wsl'


def cleanup_link(path):
    """Symlinks, Windows junctions and reparse points never authorize traversal."""
    import stat
    info = Path(path).lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def cleanup_scope(request):
    """Keep only this physical drive; path spelling/translation lives here."""
    import copy
    import tempfile
    scoped = copy.deepcopy(request)
    def belongs(path):
        return cleanup_contains(request, path)
    # Repositories may have worktrees on another drive. Stages scope every candidate,
    # rather than dropping the repository that owns that drive's registrations.
    for name in ('temp_roots', 'report_roots'):
        roots = request['paths'].get(name, [])
        if name == 'temp_roots':
            roots = [*roots, tempfile.gettempdir(), str(Path(request['state_dir']) / 'scratch')]
        scoped['paths'][name] = list(dict.fromkeys(p for p in roots if belongs(p)))
    scoped['paths']['caches'] = [entry for entry in request['paths'].get('caches', [])
                                if entry['placement'] == cleanup_side() and belongs(entry['cwd'])]
    return scoped


def cleanup_contains(request, path):
    """Whether this concrete candidate belongs to the requested physical drive."""
    declared = request['drive']['path']
    host = physical_disk_path(path)
    drive_host = physical_disk_path(declared) if declared is not None else None
    if host is not None and drive_host is not None:
        return str(host).casefold() == str(drive_host).casefold()
    # A parent may provide an exact native scope (also used by hermetic workers).
    # A known different physical mapping always wins over lexical containment.
    return bool(declared and Path(path).is_absolute() and Path(path).is_relative_to(Path(declared)))


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
    done = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                          input=json.dumps(notice), capture_output=True, text=True, timeout=15, shell=False)
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
            birth = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
        except FileNotFoundError:
            birth = 'absent'
        except OSError:
            birth = None
    return dict(pid=pid, birth=birth, side=cleanup_side())


def cleanup_claim_alive(identity):
    if identity.get('side') != cleanup_side() or not identity.get('birth'):
        return None
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
