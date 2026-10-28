"""Placement paths operations."""
import os
import re
import sys
from pathlib import Path



IS_WINDOWS = sys.platform == "win32"


WIN_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$", re.S)


MNT_RE = re.compile(r"^/mnt/([a-zA-Z])(?:/(.*))?$")


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
    if is_linux_native(path):
        return path  # An ext4 worktree has no Windows spelling, even when its repository is on Windows.
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


def is_linux_native(path):
    return str(path).startswith("/") and not MNT_RE.match(str(path))


def wsl_worktrees_root(paths=None, nt=None):
    """The WSL-side root for worktrees (`paths.wsl_worktrees`), as an absolute Linux path. `~` is the home of
    `paths.wsl_user` (else the Windows user name) when this host is Windows, else this user's own home."""
    from . import config
    paths = paths or config.get()["paths"]
    root = str(paths["wsl_worktrees"]).rstrip("/") or "/"
    if root == "~" or root.startswith("~/"):
        if IS_WINDOWS if nt is None else nt:
            user = paths.get("wsl_user") or os.environ.get("USERNAME") or os.environ.get("USER") or "user"
            root = ("/root" if user == "root" else f"/home/{user}") + root[1:]
        else:
            root = os.path.expanduser(root)
    return root


def routes_through_wsl(path, nt=None):
    return (IS_WINDOWS if nt is None else nt) and is_linux_native(path)


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


def cleanup_path_side(path):
    return 'windows' if drive(str(path)) else worktree_side(str(path))
