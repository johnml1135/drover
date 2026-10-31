"""Disk-cleanup preflight: filesystem and repository probes that must pass before a configuration is used."""
from pathlib import Path

from . import placement
from .config_defaults import ConfigError
from .disk_paths import repository_tree


def check_cache_folders(cfg):
    """Refuse a validated configuration whose folder caches could reach a repository, worktree or state path."""
    folders = [c['path'] for c in cfg['disk_cleanup']['caches'] if 'path' in c]
    if not folders:
        return
    from . import config, repos
    protected = [str(config.state_dir(cfg)), cfg['paths']['repos_root'], cfg['paths']['worktrees']]
    try:
        for machine_file in config.repos_dir(cfg).glob('*.toml'):
            machine = repos.read_machine(machine_file)
            protected.extend(machine.get(k) for k in ('repo', 'worktrees'))
            if machine.get('repo') and not machine.get('worktrees') and not cfg['paths']['worktrees']:
                protected.append(machine['repo'].rstrip('\\/') + '.worktrees')
        for folder in folders:
            concrete = Path(placement.native(folder))
            if repository_tree(concrete):
                raise ConfigError('folder cache overlaps protected repository')
            parts = placement.cleanup_parts(folder)
            if any(part in ('.git', '.venv') for part in parts):
                raise ConfigError('folder cache overlaps protected path')
            for root in filter(None, protected):
                other = placement.cleanup_parts(root)
                if parts[:len(other)] == other or other[:len(parts)] == parts:
                    raise ConfigError('folder cache overlaps protected repository, worktree or state path')
    except (OSError, ValueError) as error:
        raise ConfigError(str(error)) from error
