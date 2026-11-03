"""What a pane launch puts on disk and in argv: the private credential, the launcher, the agent's flags, the trust proof.

The credential travels by private file only. A Windows file gets an ACL before it holds anything. A WSL file sits on
ext4 with mode 600, because the CLI refuses a drvfs file, so the secret goes to it on stdin. Only paths cross a pane.
"""
import getpass
import json
import os
import re
import shlex
import tomllib
from pathlib import Path

from . import config, pane_status_hook, placement, process
from .launch import _reject_permission_bypass

NATIVE_COMPACT = 350_000
WSL_TIMEOUT_S = 15
SANDBOXES = ('read-only', 'workspace-write')
WSL_PRIVATE = ('umask 077; mkdir -p "$HOME/.drover/panes" && set -C && f="$HOME/.drover/panes/$1" && '
               'cat > "$f" && printf %s "$f"')
GATEWAY = "$(ip route show default | awk '{print $3; exit}')"


def _write_windows(path, text):
    """Nothing secret is on disk until the ACL holds, and O_EXCL never writes through a file already there."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    try:
        grant = ['icacls', str(path), '/inheritance:r', '/grant:r', f'{getpass.getuser()}:F']
        done = process.run(process.Command(grant, WSL_TIMEOUT_S))
        if not done.ok:         # a run that did not end is a refusal too, and the file goes
            raise OSError(done.detail or 'icacls refused the private file')
        with open(path, 'w', encoding='utf-8', newline='\n') as out:
            out.write(text)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return str(path)


def _write_wsl(name, text):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', name):
        raise ValueError('bad private file name')
    argv = process.wsl_argv(['-d', placement.distro(), '--exec', 'bash', '-c', WSL_PRIVATE, 'bash', name])
    done = process.run(process.Command(argv, WSL_TIMEOUT_S, stdin=text))     # a str goes as UTF-8, as before
    if not done.ok:
        raise OSError(done.detail or 'the private file was not written in WSL')
    written = done.out.decode('utf-8').strip()      # strict, as encoding='utf-8' read it
    if not written:
        raise OSError('the private file was not written in WSL')
    return written


def write_private(path, text, *, placement) -> str:
    """The path the agent reads. `path` is the file on Windows; for WSL only its name is used, under ~/.drover/panes."""
    return _write_wsl(Path(path).name, text) if placement == 'wsl' else _write_windows(path, text)


def _wsl_read(args):
    done = process.run(process.Command(process.wsl_argv(['-d', placement.distro(), '--exec', *args]), WSL_TIMEOUT_S))
    if not done.ok:
        raise OSError(done.detail or f'the WSL read failed with exit status {done.code}')
    return done.out.decode('utf-8')     # strict: undecodable output is a ValueError, so untrusted


def _read_config(home):
    if home.startswith('~/'):
        return ['sh', '-c', 'exec cat -- "$HOME/$1"', 'sh', home[2:] + '/config.toml']
    return ['cat', '--', home + '/config.toml']


def repo_trusted(cwd, seat, *, read=None) -> bool:
    """`cwd` is the root of a checkout whose main repo the seat's Codex marks trusted. Any doubt or error is False.

    Both reads run in WSL, where a Codex seat lives, so every path compared is in WSL form. `read(args)` is the seam.
    """
    read = read or _wsl_read
    try:
        path = placement.to_wsl_path(cwd).rstrip('/')
        home = config.seat_env(seat).get('CODEX_HOME')
        found = read(['git', '-C', path, 'rev-parse', '--path-format=absolute', '--show-toplevel',
                      '--git-common-dir']).splitlines()[:2] if home else []
        if len(found) < 2 or found[0] != path or not found[1].endswith('/.git'):    # a subfolder, a bare repo or a submodule
            return False
        projects = tomllib.loads(read(_read_config(home.rstrip('/')))).get('projects')
        if not isinstance(projects, dict):
            return False
        entry = {placement.to_wsl_path(key).rstrip('/'): value for key, value in projects.items()}.get(found[1].removesuffix('/.git'))
        return isinstance(entry, dict) and entry.get('trust_level') == 'trusted'
    except (OSError, ValueError):
        return False


def agent_args(spec, *, model, effort, settings=None, resume=None) -> list:
    """The agent's flags: model, effort, compaction, a Codex seat's sandbox, the folders it may also write. A bypass raises ValueError."""
    roots = spec.get('run_config', {}).get('sandbox_writable_roots', [])
    if spec['backend'] == 'claude':
        args = ['--resume', resume] if resume else []
        args += ['--model', model, '--effort', effort, '--autocompact', f'{NATIVE_COMPACT // 1000}k']
        args += [item for root in roots for item in ('--add-dir', root)]
        if settings:
            args += ['--settings', settings]
    else:
        args = ['resume', resume] if resume else []
        args += ['--model', model, '-c', f'model_reasoning_effort={effort}', '-c', f'model_context_window={NATIVE_COMPACT}',
                 '-c', f'model_auto_compact_token_limit={NATIVE_COMPACT}', '-c', 'check_for_update_on_startup=false']
        sandbox = spec.get('sandbox')
        if sandbox and sandbox not in SANDBOXES:
            raise ValueError('sandbox is not a seat setting')
        args += ['-s', sandbox] if sandbox else []
        args += ['-c', f'sandbox_workspace_write.writable_roots={json.dumps(roots)}'] if roots and sandbox == 'workspace-write' else []
    _reject_permission_bypass(args)
    return args


def hook_settings(kind) -> str:
    """Claude's `--settings` for a WSL pane: only the status hook, on every event it reports."""
    command = f'python3 {placement.to_wsl_path(pane_status_hook.__file__)} --agent {kind}'.replace('\\', '/')
    entry = [dict(hooks=[dict(type='command', command=command)])]
    return json.dumps(dict(hooks={event: entry for event in pane_status_hook.STATES}), indent=1)


def _ps(value):
    return "'" + str(value).replace("'", "''") + "'"


def _sh_path(value):
    value = str(value)
    return '"$HOME"/' + shlex.quote(value[2:]) if value.startswith('~/') else shlex.quote(placement.to_wsl_path(value))


def _windows_launcher(env, hub_url, credential):
    values = {**env, 'DROVER_HUB_URL': hub_url, 'DROVER_CREDENTIAL_FILE': credential}
    return ''.join(f'$env:{name} = {_ps(value)}\n' for name, value in values.items())


def _wsl_launcher(script, distro):
    names = ':'.join(pane_status_hook.WSLENV_NAMES)
    return (f"$env:WSLENV = {_ps(names)} + $(if ($env:WSLENV) {{ ':' + $env:WSLENV }})\n"
            f'wsl.exe -d {distro} -e bash -li {_ps(placement.to_wsl_path(script))}\n')


def _wsl_script(env, port, run_id, cwd, argv):
    lines = ['#!/bin/bash']
    lines += [f'export {name}={_sh_path(value)}' for name, value in env.items()]
    lines += [f'export DROVER_HUB_URL="http://{GATEWAY}:{int(port)}"',
              f'export DROVER_CREDENTIAL_FILE="$HOME"/.drover/panes/run-{int(run_id)}.credential',
              f'cd {shlex.quote(placement.to_wsl_path(cwd))} || exit 1', 'exec ' + shlex.join(argv)]
    return '\n'.join(lines) + '\n'


def write_launcher(folder, spec, seat, args, *, credential, port, distro) -> str:
    """Write the launcher, which holds paths and never the secret, and return the path `pane run` carries."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    env = config.seat_env(seat)
    launcher = folder / 'launch.ps1'
    if spec['placement'] == 'wsl':
        script = folder / 'launch.sh'
        command = [spec['backend'], *args]
        script.write_text(_wsl_script(env, port, spec['run_id'], spec['cwd'], command), encoding='utf-8', newline='\n')
        text = _wsl_launcher(script, distro)
    else:
        text = _windows_launcher(env, f'http://127.0.0.1:{int(port)}', credential)
    launcher.write_text(text, encoding='utf-8')
    return str(launcher)
