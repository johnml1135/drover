"""Placement git operations."""
import os
import subprocess
import sys
import json
import contextvars
import threading
import time
from pathlib import Path

from .placement_paths import (
    IS_WINDOWS,
    MNT_RE,
    WIN_RE,
    drive,
    native,
    routes_through_wsl,
    to_wsl_path,
)
from .placement_exec import (
    NO_WINDOW,
    WSL,
    Windows,
    distro,
)


git_spawns = 0
git_spawn_lock = threading.Lock()
request_git = contextvars.ContextVar("request_git", default=None)

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

def _git_command(repo, args, nt=None, distro=None, side=None):
    """`side="wsl"` runs the WSL git against a Windows-spelled repo (via /mnt/<drive>) for work that creates or
    repairs an ext4 worktree: its pointers must be written by the side that will open them."""
    repo = str(repo)
    if routes_through_wsl(repo, nt) or (side == "wsl" and (IS_WINDOWS if nt is None else nt)):
        return WSL(distro).command(["git", "-C", to_wsl_path(repo), *_map_paths(args)]), {}
    repo = native(repo)
    prefix, env = _git_location(repo)
    return Windows().command(["git", "-C", repo, *prefix, *args]), env

def git_argv(repo, args, nt=None, distro=None, side=None):
    """The argv that runs `git -C repo *args` from this host."""
    return _git_command(repo, args, nt, distro, side)[0]

def git(repo, *args, timeout=None, env=None, runner=None, side=None):
    """subprocess result of git in `repo`; a missing wsl.exe/git is a failed command (rc 127), not an exception."""
    global git_spawns
    try:
        argv, extra = _git_command(repo, args, side=side)
        if extra:
            env = {**(os.environ if env is None else env), **extra}
        with git_spawn_lock:
            git_spawns += 1
        count = request_git.get()
        if count is not None:
            count["spawns"] += 1
        started = time.perf_counter() if timeout is not None else None
        result = (runner or subprocess.run)(argv, text=True, capture_output=True, encoding="utf-8", errors="replace",
                                            timeout=timeout, env=env, **NO_WINDOW)
        # Process creation cannot always be interrupted (notably on Windows). A process
        # that exits during startup can bypass subprocess.run's timeout check entirely.
        if timeout is not None and time.perf_counter() - started >= timeout:
            raise subprocess.TimeoutExpired(argv, timeout, output=result.stdout, stderr=result.stderr)
        return result
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

def _native_metadata_io(directory, filename, content):
    """Avoid a Python startup per native ownership check; match the WSL worker's atomic IO."""
    import tempfile
    path = Path(directory) / filename
    if content is None:
        return path.read_text(encoding='utf-8') or None
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
        return content or None
    finally:
        if name is not None:
            os.unlink(name)


def worktree_metadata(worktree: str, filename: str, content: str | None = None) -> str | None:
    """Read/write a linked worktree's admin file on its owning host, replacing atomically."""
    if not filename or filename in ('.', '..') or any(c in filename for c in '/\\\0:'):
        raise ValueError('metadata filename must be a basename')
    try:
        discovered = git(worktree, 'rev-parse', '--path-format=absolute',
                         '--absolute-git-dir', '--git-common-dir', timeout=30)
        directories = discovered.stdout.strip().splitlines()
        if discovered.returncode or len(directories) != 2 or not all(directories) or directories[0] == directories[1]:
            raise OSError('cannot resolve linked worktree admin directory')
        directory = directories[0]
        if not routes_through_wsl(worktree):
            return _native_metadata_io(native(directory), filename, content)
        argv = WSL().command(['python3', '-c', _METADATA_IO, directory, filename])
        result = subprocess.run(argv, input=json.dumps(content), text=True, encoding='utf-8',
                                capture_output=True, timeout=30, **NO_WINDOW)
        if result.returncode:
            raise OSError(result.stderr.strip() or 'worktree metadata IO failed')
        return result.stdout or None
    except (OSError, ValueError, subprocess.SubprocessError):
        if content is not None:
            raise
        return None
