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
import time
from pathlib import Path

PYTHON_RE = re.compile(r"(?i)python(w)?(\d+(\.\d+)?)?(\.exe)?")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
KILL_AFTER_S = 15


class ScanError(RuntimeError):
    """Process discovery failed; absence is unknown."""


IS_WINDOWS = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000
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


def config_path(path, reference):
    """Spell a discovered path like the repository's configured path."""
    path = str(path)
    return to_windows_path(path).replace("/", "\\") if WIN_RE.match(str(reference)) else path


def status_args(repo):
    """DrvFs checks only tracked changes: never recursively scan build/untracked trees."""
    untracked = "no" if not IS_WINDOWS and MNT_RE.match(native(repo)) else "normal"
    return ["status", "--porcelain", f"--untracked-files={untracked}"]


def _git_location(repo):
    """Override foreign absolute gitdir pointers without editing shared metadata."""
    try:
        text = (Path(repo) / ".git").read_text(encoding="utf-8").strip()
        if not text.startswith("gitdir: ") or native(text[8:]) == text[8:]:
            return [], {}
        _, gitdir, common = _git_dirs(repo)
    except (OSError, UnicodeError, ValueError):
        return [], {}
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

    def command(self, argv):
        return list(argv)

    def spawn(self, argv, **kw):
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


def wsl_argv(distro, runner_args, cd=None, env=None):
    args = [str(a) for a in runner_args]
    split = args.index("--") if "--" in args else len(args)
    args = [to_wsl_path(a) for a in args[:split]] + args[split:]
    return WSL(distro).command(["python3", "-m", "drover.runner", *args], cd, env)


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


def git_argv(repo, args, nt=None, distro=None):
    """The argv that runs `git -C repo *args` from this host."""
    repo = str(repo)
    if routes_through_wsl(repo, nt):
        return WSL(distro).command(["git", "-C", repo, *_map_paths(args)])
    repo = native(repo)
    prefix, _ = _git_location(repo)
    return Windows().command(["git", "-C", repo, *prefix, *args])


def git(repo, *args, timeout=None, env=None, runner=None):
    """subprocess result of git in `repo`; a missing wsl.exe/git is a failed command (rc 127), not an exception."""
    try:
        _, extra = _git_location(native(repo)) if not routes_through_wsl(repo) else ([], {})
        if extra:
            env = {**(os.environ if env is None else env), **extra}
        return (runner or subprocess.run)(git_argv(repo, args), text=True, capture_output=True, errors="replace",
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
    CREATE_SUSPENDED = 0x4
    CREATE_NEW_PROCESS_GROUP = 0x200
    CREATE_BREAKAWAY_FROM_JOB = 0x01000000

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


def parse_runner_argv(argv):
    """Recognize only a direct Python runner invocation; worker arguments cannot spoof the marker."""
    argv = list(argv)
    if len(argv) < 4 or not PYTHON_RE.fullmatch(re.split(r"[\\/]", argv[0])[-1]) or argv[1:3] != ["-m", "drover.runner"]:
        return None
    rest = argv[3:]
    if "--" in rest:
        rest = rest[:rest.index("--")]
    if rest and rest[0] in ("stop", "find"):
        return None
    for j, a in enumerate(rest):
        if a == "--run-id" and j + 1 < len(rest) and RUN_ID_RE.fullmatch(rest[j + 1]):
            return rest[j + 1]
        if a.startswith("--run-id=") and RUN_ID_RE.fullmatch(a[9:]):
            return a[9:]
    return None


def _runner_runs_dir(argv):
    """The runs directory from a valid runner command line, else None."""
    argv = list(argv)
    if parse_runner_argv(argv) is None:
        return None
    rest = argv[3:]
    if "--" in rest:
        rest = rest[:rest.index("--")]
    for j, arg in enumerate(rest):
        if arg == "--runs-dir" and j + 1 < len(rest):
            return rest[j + 1]
        if arg.startswith("--runs-dir="):
            return arg[len("--runs-dir="):]
    return None


def _same_path(left, right):
    try:
        return os.path.normcase(os.path.abspath(os.path.expanduser(str(left)))) == \
            os.path.normcase(os.path.abspath(os.path.expanduser(str(right))))
    except (OSError, TypeError, ValueError):
        return False


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


def find_runners(run_id=None, cmdlines=None, runs_dir=None):
    """One live leaf runner per run id, optionally restricted to an owner directory.

    Failed discovery raises ScanError; it never means there are no runners.
    """
    if cmdlines is None:
        cmdlines = _proc_cmdlines_windows() if IS_WINDOWS else _proc_cmdlines_linux()
    by_id = {}
    for row in cmdlines:
        pid, argv, ppid = (tuple(row) + (None,))[:3]
        rid = parse_runner_argv(argv)
        if rid and (run_id is None or rid == run_id) and \
                (runs_dir is None or _same_path(_runner_runs_dir(argv), runs_dir)):
            by_id.setdefault(rid, []).append((pid, ppid))
    found = []
    for rid, procs in by_id.items():
        parents = {pp for _, pp in procs}
        leaves = [p for p, _ in procs if p not in parents] or [p for p, _ in procs]
        found.append({"pid": min(leaves), "run_id": rid})
    return sorted(found, key=lambda r: r["pid"])


def detach_process(cmd, env, log):
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

def real_wsl(args: list[str], timeout: float) -> tuple[int, str]:
    """Run wsl.exe; return (returncode, output). Timeout gives (-1, 'timeout')."""
    try:
        p = subprocess.run(wsl_control(args), capture_output=True, timeout=timeout, **NO_WINDOW)
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    except OSError as e:
        return -2, str(e)
    return p.returncode, decode_output(p.stdout + p.stderr).strip()


def real_keepalive(distro: str, seconds: float):
    """Start `wsl.exe -d <distro> --exec sleep <seconds>`; the returned Popen is the keepalive child."""
    return subprocess.Popen(WSL(distro).command(["sleep", str(int(seconds))]),
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **NO_WINDOW)


def wsl_running(default, run=subprocess.run):
    """True when the distro is up right now. `wsl -l --running` does not start anything, unlike `wsl --exec`."""
    try:
        r = run(wsl_control(["-l", "--running", "-q"]), capture_output=True, timeout=20, **NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return False
    names = decode_output(r.stdout).splitlines()
    return r.returncode == 0 and distro(default).lower() in [x.strip().lower() for x in names]


def add_worktree(repo, dest, *tail, git, error):
    """Create a worktree; old git may use absolute pointers only on a native filesystem."""
    r = git(repo, '-c', 'core.autocrlf=false', 'worktree', 'add', '--relative-paths', *tail, check=False)
    rejected = re.search(r'(unknown|unrecognized|unsupported|invalid) (option|argument|switch)[^\n]*relative-paths',
                         r.stderr, re.IGNORECASE)
    if r.returncode and rejected:
        if not is_linux_native(dest):
            raise error('this git has no `worktree add --relative-paths`, and a worktree under '
                        f'{dest} is opened from both Windows and WSL: update git (2.48 or newer)')
        r = git(repo, '-c', 'core.autocrlf=false', 'worktree', 'add', *tail, check=False)
    if r.returncode:
        raise error(f'git worktree add failed: {r.stderr.strip()[-300:]}')

def _git_dirs(cwd):
    workspace = Path(native(cwd)).resolve()

    def resolve(base, value):
        if not value:
            raise ValueError("empty git metadata path")
        path = native(value)
        if os.name != "nt":
            path = path.replace("\\", "/")
        return (base / path).resolve(strict=True)

    try:
        for parent in (workspace, *workspace.parents):
            marker = parent / ".git"
            try:
                marker.lstat()
            except FileNotFoundError:
                continue
            if marker.is_dir():
                gitdir = marker.resolve(strict=True)
            else:
                prefix, sep, target = marker.read_text(encoding="utf-8").strip().partition(":")
                if prefix != "gitdir" or not sep:
                    raise ValueError("invalid git metadata pointer")
                gitdir = resolve(parent, target.strip())
            common_file = gitdir / "commondir"
            try:
                common_file.lstat()
            except FileNotFoundError:
                common = gitdir
            else:
                common = resolve(gitdir, common_file.read_text(encoding="utf-8").strip())
            if not gitdir.is_dir() or not common.is_dir():
                raise ValueError("git metadata is not a directory")
            return workspace, gitdir, common
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot resolve git metadata for Claude sandbox: {exc}") from exc
    return workspace, None, None


def external_git_dirs(cwd):
    workspace, gitdir, common = _git_dirs(cwd)
    return list(dict.fromkeys(str(p) for p in (common, gitdir) if p and not p.is_relative_to(workspace)))


def decode_output(raw):
    if isinstance(raw, str):
        return raw
    return raw.decode("utf-16-le" if b"\x00" in raw[:4] else "utf-8", "ignore").replace("\x00", "")

def scratch_path(value):
    root, path = Path("/tmp/drover-scratch"), Path(value)
    parent = path.parent
    direct = parent == root
    task_scratch = Path(os.environ.get("TMPDIR", ""))
    nested = (not direct and parent == task_scratch and parent.parent == root and parent.is_dir()
              and not parent.is_symlink() and parent.resolve() == parent)
    if (IS_WINDOWS or (not direct and not nested) or root.resolve() != root
            or path.is_symlink() or path.resolve().parent != parent):
        raise ValueError("scratch must be under /tmp/drover-scratch/ or its assigned TMPDIR")
    return path



DOTNET_SHM_ROOT = "/tmp/.dotnet"


def ensure_dotnet_shm(root=DOTNET_SHM_ROOT):
    """Create <root>/shm (sticky, 1777) for the .NET named-mutex shared memory; leave an existing one alone."""
    shm = Path(root) / "shm"
    if shm.is_dir():
        return
    created = not Path(root).is_dir()
    Path(root).mkdir(parents=True, exist_ok=True)
    if created:
        os.chmod(root, 0o1777)      # sticky and world-writable like /tmp; chmod because mkdir's mode obeys the umask
    shm.mkdir(exist_ok=True)
    os.chmod(shm, 0o1777)


def worker_scratch_env(scratch):
    """Environment for a WSL worker's private temp and .NET caches; the global NuGet package cache stays shared."""
    scratch = Path(scratch)
    return {
        "TMPDIR": str(scratch), "TMP": str(scratch), "TEMP": str(scratch),
        "DOTNET_CLI_HOME": str(scratch / "dotnet-home"),
        "NUGET_HTTP_CACHE_PATH": str(scratch / "nuget-http"),
        "NUGET_PLUGINS_CACHE_PATH": str(scratch / "nuget-plugins"),
        "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1",
        "NUGET_PACKAGES": str(Path.home() / ".nuget" / "packages"),
    }


