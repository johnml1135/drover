"""Worktree creation shared by task runs and landing batches."""
import re

from . import launch, repos


def cross_os(dest):
    """Drive paths are opened by both Windows and WSL, so gitdir pointers must be relative."""
    return not launch.is_linux_native(str(dest)) or repos.WIN_RE.match(str(dest)) is not None


def add(repo, dest, *tail, git, error, shared=None):
    """Create a worktree; old git may use absolute pointers only on a native filesystem."""
    r = git(repo, '-c', 'core.autocrlf=false', 'worktree', 'add', '--relative-paths', *tail, check=False)
    rejected = re.search(r'(unknown|unrecognized|unsupported|invalid) (option|argument|switch)[^\n]*relative-paths',
                         r.stderr, re.IGNORECASE)
    if r.returncode and rejected:
        if shared is None:
            shared = cross_os(dest)
        if shared:
            raise error('this git has no `worktree add --relative-paths`, and a worktree under '
                        f'{dest} is opened from both Windows and WSL: update git (2.48 or newer)')
        r = git(repo, '-c', 'core.autocrlf=false', 'worktree', 'add', *tail, check=False)
    if r.returncode:
        raise error(f'git worktree add failed: {r.stderr.strip()[-300:]}')
