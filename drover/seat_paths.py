"""Resolve and check account homes on the platform where each seat runs."""
import os
import subprocess
from pathlib import Path

from . import placement


def resolve(path, seat, paths, *, windows=None, run=None):
    windows = placement.IS_WINDOWS if windows is None else windows
    run = run or subprocess.run
    if not path.startswith('~'):
        return path
    if not windows or seat['placement'] != 'wsl':
        return os.path.expanduser(path)
    # Ask the same distro/default user used by the runner; Windows' HOME is unrelated.
    code = 'import os,sys; print(os.path.expanduser(sys.argv[1]))'
    result = run(placement.WSL(placement.distro(paths['wsl_distro'])).command(['python3', '-c', code, path]),
                 capture_output=True, text=True, encoding='utf-8', timeout=10, **placement.NO_WINDOW)
    expanded = result.stdout.strip()
    if result.returncode or not expanded.startswith('/'):
        raise OSError(f'cannot resolve WSL seat home {path!r}')
    return expanded


def resolve_spec(spec, env, distro=None, *, windows=None, run=None):
    """The launch spec with each seat home resolved for the seat's platform and the effective distro."""
    from . import config
    seat = config.seat_by_name(spec.get('seat')) or {'placement': spec.get('placement', 'windows')}
    paths = {**config.get()['paths'], 'wsl_distro': placement.distro(distro)}
    return {**spec, 'env': {key: resolve(value, seat, paths, windows=windows, run=run) for key, value in env.items()}}


def validate(cfg, *, distro=None, windows=None, run=None):
    """Health errors, keyed by seat, using the current distro for both expansion and existence."""
    from . import config
    windows = placement.IS_WINDOWS if windows is None else windows
    run = run or subprocess.run
    paths = {**cfg['paths'], 'wsl_distro': placement.distro(distro or cfg['paths']['wsl_distro'])}
    errors = {}
    for seat in cfg['seats']:
        path = next(iter(config.seat_env(seat).values()))
        try:
            path = resolve(path, seat, paths, windows=windows, run=run)
            if windows and seat['placement'] == 'wsl':
                code = 'import os,sys; print(int(os.path.isdir(sys.argv[1])))'
                result = run(placement.WSL(paths['wsl_distro']).command(['python3', '-c', code, placement.to_wsl_path(path)]),
                             capture_output=True, text=True, encoding='utf-8', timeout=10, **placement.NO_WINDOW)
                exists = result.returncode == 0 and result.stdout.strip() == '1'
            else:
                exists = Path(placement.native(path)).is_dir()
            if not exists:
                raise OSError(f'home does not exist on {seat["placement"]}: {path}')
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            errors[seat['name']] = str(error)
    return errors
