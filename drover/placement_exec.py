"""Placement exec operations."""
import os
import re
import subprocess
import sys
import shutil

from .placement_paths import (
    IS_WINDOWS,
    is_linux_native,
    native,
    to_windows_path,
    to_wsl_path,
)
from .placement_tree import (
    PosixTree,
    WindowsTree,
)


TELEMETRY_OPTOUT = dict.fromkeys(('POWERSHELL_TELEMETRY_OPTOUT', 'DOTNET_CLI_TELEMETRY_OPTOUT',
    'MSBUILDTELEMETRYOPTOUT', 'TESTINGPLATFORM_TELEMETRY_OPTOUT', 'AVALONIA_TELEMETRY_OPTOUT', 'DOTNET_NOLOGO'), '1')
for _name, _value in TELEMETRY_OPTOUT.items(): os.environ.setdefault(_name, _value)
_wslenv = [v for v in os.environ.get('WSLENV', '').split(':') if v and v.split('/')[0] not in TELEMETRY_OPTOUT]
os.environ['WSLENV'] = ':'.join([*_wslenv, *TELEMETRY_OPTOUT])


CREATE_NO_WINDOW = 0x08000000


CREATE_SUSPENDED = 0x4


CREATE_NEW_PROCESS_GROUP = 0x200


CREATE_BREAKAWAY_FROM_JOB = 0x01000000


NO_WINDOW = {"creationflags": CREATE_NO_WINDOW} if IS_WINDOWS else {}


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


def spawn_worker(argv, out_f, stdin_f, cwd, env):
    """(proc, tree). The worker's stdout and stderr both go to out_f."""
    kw = dict(stdin=stdin_f, stdout=out_f, stderr=subprocess.STDOUT, cwd=cwd, env=env)
    adapter = Windows() if IS_WINDOWS else WSL()
    return adapter.spawn(argv, **kw)


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
        env = {**{k: os.environ.get(k, v) for k, v in TELEMETRY_OPTOUT.items()}, **(env or {})}
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
