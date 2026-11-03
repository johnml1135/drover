"""Disk-cleanup preflight: filesystem and repository probes that must pass before a configuration is used."""
from pathlib import Path

from . import placement
from .config_defaults import ConfigError

# Folders no build root may be, below a drive letter (any drive) or the Linux root; each is or holds what the system runs.
SYSTEM_WINDOWS = frozenset((('windows',), ('program files',), ('program files (x86)',), ('programdata',), ('users',)))
SYSTEM_LINUX = frozenset((('/', 'usr'), ('/', 'etc'), ('/', 'home'), ('/', 'var')))


def check_cache_folders(cfg):
    """Refuse a validated configuration whose folder caches could reach a repository, worktree or state path, or whose
    build roots lie in a git tree or overlap the home folder, a repository, a worktree root or the state path."""
    folders = [c['path'] for c in cfg['disk_cleanup']['caches'] if 'path' in c]
    builds = cfg['disk_cleanup'].get('build_roots', [])
    if not folders and not builds:
        return
    from . import config, disk_authority, repos     # disk_authority reaches config through repos
    protected = [str(config.state_dir(cfg)), cfg['paths']['repos_root'], cfg['paths']['worktrees']]
    try:
        for machine_file in config.repos_dir(cfg).glob('*.toml'):
            machine = repos.read_machine(machine_file)
            protected.extend(machine.get(k) for k in ('repo', 'worktrees'))
            if machine.get('repo') and not machine.get('worktrees') and not cfg['paths']['worktrees']:
                protected.append(machine['repo'].rstrip('\\/') + '.worktrees')
        for folder in folders:
            concrete = Path(placement.native(folder))
            if disk_authority.repository_tree(concrete):
                raise ConfigError('folder cache overlaps protected repository')
            parts = placement.cleanup_parts(folder)
            if any(part in ('.git', '.venv') for part in parts):
                raise ConfigError('folder cache overlaps protected path')
            for root in filter(None, protected):
                other = placement.cleanup_parts(root)
                if parts[:len(other)] == other or other[:len(parts)] == parts:
                    raise ConfigError('folder cache overlaps protected repository, worktree or state path')
        for entry in builds:
            _build_root(entry, protected, disk_authority.repository_root)
    except (OSError, ValueError) as error:
        raise ConfigError(str(error)) from error


def _build_root(entry, protected, repository_root):
    """One build root: never a drive or filesystem root, a system folder, home or a folder holding it, never in or
    holding a repository, worktree root or the state path, never in a git tree. Only a root on this side is probed for a repository above it, so loading a configuration never
    wakes WSL; the stage's rule checks each unit's ancestors again before anything goes."""
    parts = placement.cleanup_parts(entry['path'])
    folded = tuple(part.casefold() for part in parts)
    if any(part in ('.git', '.venv') for part in folded):
        raise ConfigError(f"build root {entry['path']!r} is in a protected path")
    if len(parts) <= 1:
        raise ConfigError(f"build root {entry['path']!r} is a drive or filesystem root")
    drive = folded[0].endswith(':')
    if folded[1:] in SYSTEM_WINDOWS if drive else folded in SYSTEM_LINUX:
        raise ConfigError(f"build root {entry['path']!r} is a system folder")
    here = entry['placement'] == placement.cleanup_side()
    home = placement.cleanup_parts(str(Path.home())) if here else ()
    # Off its side the home folder is known by its shape alone: C:\Users\<u>, /home/<u>, /root.
    shaped = folded[1:2] == ('users',) and len(folded) == 3 if drive else \
        folded == ('/', 'root') or folded[:2] == ('/', 'home') and len(folded) == 3
    if (here and home[:len(parts)] == parts) or shaped:
        raise ConfigError(f"build root {entry['path']!r} is or holds the home folder")
    for root in filter(None, protected):
        other = placement.cleanup_parts(root)
        if parts[:len(other)] == other or other[:len(parts)] == parts:
            raise ConfigError(f"build root {entry['path']!r} overlaps a repository, worktree or state path")
    if here:
        concrete = Path(placement.native(entry['path'])).absolute()
        if any(repository_root(parent) for parent in (concrete, *concrete.parents)):
            raise ConfigError(f"build root {entry['path']!r} is in a git tree")
