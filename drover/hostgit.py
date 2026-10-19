"""One place that decides how the hub reaches a git working directory from the OS it runs on.

A Windows hub cannot open /home/... (WSL-only) and cannot treat /mnt/<d>/ as a path, and a WSL process cannot open
R:\\... . Routing choice: a WSL-only path runs git through `wsl.exe -d <distro> --exec git` (the same wsl.exe form the
runner and gates use), because a \\\\wsl.localhost UNC path breaks git's ownership and lock handling. A /mnt/<d>/ path
on Windows (or a drive path on WSL) is just translated to the native spelling."""
import os
import subprocess

from . import config
from .winproc import NO_WINDOW


def is_linux_native(path):
    p = str(path)
    return p.startswith('/') and not config.MNT_RE.match(p)


def routes_through_wsl(path, nt=None):
    """True when `path` can only be opened inside WSL and the caller is the Windows hub."""
    return (os.name == "nt" if nt is None else nt) and is_linux_native(path)


def _wsl_arg(arg):
    from .runner import to_wsl_path
    return to_wsl_path(str(arg))


# Arguments that follow these are paths; subcommand positionals of `worktree add|repair|remove|move` are too.
_PATH_OPTS = {"--git-dir", "--work-tree", "-C", "--output", "-o"}
_WORKTREE_PATH_SUBS = {"add", "repair", "remove", "move", "lock", "unlock"}


def _map_paths(args):
    """Translate only path arguments to WSL form; messages, refs and revisions pass through unchanged."""
    args, out, i = [str(a) for a in args], [], 0
    worktree_sub = len(args) > 1 and "worktree" in args[:3] and args[args.index("worktree") + 1] in _WORKTREE_PATH_SUBS
    start = args.index("worktree") + 2 if worktree_sub else 0
    mapped = False
    skip_value = {"-b", "-B", "--reason", "-m"}
    while i < len(args):
        a = args[i]
        if a in _PATH_OPTS and i + 1 < len(args):
            out += [a, _wsl_arg(args[i + 1])]
            i += 2
            continue
        if a in skip_value and i + 1 < len(args):
            out += [a, args[i + 1]]
            i += 2
            continue
        if a.startswith("--git-dir=") or a.startswith("--work-tree="):
            k, _, v = a.partition("=")
            a = f"{k}={_wsl_arg(v)}"
        elif worktree_sub and i >= start and not mapped and not a.startswith("-"):
            a, mapped = _wsl_arg(a), True
        out.append(a)
        i += 1
    return out


def argv(repo, args, nt=None, distro=None):
    """The argv that runs `git -C repo *args` from this host."""
    repo = str(repo)
    if routes_through_wsl(repo, nt):
        distro = distro or config.get()["paths"]["wsl_distro"]
        return ["wsl.exe", "-d", distro, "--exec", "git", "-C", repo, *_map_paths(args)]
    return ["git", "-C", config.native(repo), *args]


def run(repo, *args, timeout=None, env=None, runner=None):
    """subprocess result of git in `repo`; a missing wsl.exe/git is a failed command (rc 127), not an exception."""
    try:
        return (runner or subprocess.run)(argv(repo, args), text=True, capture_output=True, errors="replace", 
                                           timeout=timeout, env=env, **NO_WINDOW)
    except FileNotFoundError as e:
        return subprocess.CompletedProcess(args, 127, "", f"git unavailable: {e}")


def reachable(path, runner=None):
    """True when `path` is an openable git working directory from this host (a missing one is False, never an error)."""
    path = str(path)
    if not path:
        return False
    if routes_through_wsl(path):
        return run(path, "rev-parse", "--git-dir", timeout=30, runner=runner).returncode == 0
    return os.path.exists(config.native(path))
