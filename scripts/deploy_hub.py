#!/usr/bin/env python3
"""Pin the live hub to a deployment worktree that is moved deliberately, never by a landing.

    python scripts/deploy_hub.py --deploy <dir> --receipt <gate-receipt.json>   # move <dir> to main's tip
    python scripts/deploy_hub.py --deploy <dir> --commit <sha> --force          # skip the main/gate checks
    python scripts/deploy_hub.py --deploy <dir> --receipt r.json --restart      # also sync deps and restart the hub

The hub finds `hub` and `drover` through its working directory (the scheduled task runs `uv run --project
<HubDir> python -m hub` from HubDir's parent), and every child it spawns takes its code root from the loaded
package's own location.  So a hub registered from <dir> runs and spawns only <dir>'s code, while landings keep
advancing the source checkout.  Steps: (a) the commit must be on the branch (default main) and a passing gate
receipt for exactly that commit must be given, or --force; (b) <dir> (a detached `git worktree`) is moved to it,
refusing a dirty <dir>; (c) the restart steps are printed, or run with --restart.  --restart stops only the pids
listening on the hub's UI port (a plain taskkill, never a tree kill, so a detached cleanup survives), syncs the
hub venv first, then starts the scheduled task.  Paths come from arguments; nothing is machine-specific.
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

SENTINELS = {'LISTENERS', 'STOP', 'START'}


class DeployError(Exception):
    pass


def git(repo, *args, check=True):
    r = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if check and r.returncode:
        raise DeployError(f'git {" ".join(args)} failed: {r.stderr.strip()}')
    return r


def resolve(source, commit, branch):
    sha = git(source, 'rev-parse', '--verify', '--quiet', f'{commit or branch}^{{commit}}', check=False).stdout.strip()
    if not sha:
        raise DeployError(f'cannot resolve {commit or branch} in {source}')
    return sha


def check_commit(source, sha, branch, receipt, force):
    """Why this commit may not be deployed ([] = it may)."""
    if force:
        return []
    found = []
    if git(source, 'merge-base', '--is-ancestor', sha, branch, check=False).returncode:
        found.append(f'{sha[:12]} is not on {branch}')
    if not receipt:
        found.append('no gate receipt given (--receipt FILE); pass --force to deploy without one')
        return found
    try:
        data = json.loads(Path(receipt).read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        return found + [f'cannot read receipt {receipt}: {error}']
    if not isinstance(data, dict) or data.get('sha') != sha:
        found.append(f'receipt is for {str(data.get("sha") if isinstance(data, dict) else None)[:12]}, not {sha[:12]}')
    elif not data.get('passed'):
        found.append('the receipt says the gate did not pass')
    return found


def move_worktree(source, deploy, sha):
    deploy = Path(deploy)
    if not deploy.exists():
        git(source, 'worktree', 'add', '--detach', str(deploy), sha)
        return 'created'
    if git(deploy, 'rev-parse', '--git-dir', check=False).returncode:
        raise DeployError(f'{deploy} exists and is not a git worktree')
    if git(deploy, 'status', '--porcelain', '--untracked-files=no').stdout.strip():
        raise DeployError(f'{deploy} has uncommitted changes; clean it first')
    git(deploy, 'checkout', '--detach', sha)
    return 'moved'


def default_run(argv, **kw):
    """Run argv; the LISTENERS/STOP/START sentinels become the Windows commands for the hub's port and task."""
    argv = [str(a) for a in argv]
    if argv[0] in SENTINELS:
        if os.name != 'nt':
            raise DeployError('--restart drives the Windows hub; run it from Windows (or restart the hub by hand)')
        if argv[0] == 'LISTENERS':
            ps = (f"(Get-NetTCPConnection -State Listen -LocalPort {int(argv[1])} -ErrorAction SilentlyContinue "
                  "| Select-Object -ExpandProperty OwningProcess -Unique) -join ' '")
            argv = ['powershell.exe', '-NoProfile', '-Command', ps]
        elif argv[0] == 'STOP':
            argv = ['taskkill.exe', '/PID', argv[1], '/F']          # no /T: children stay alive
        else:
            argv = ['powershell.exe', '-NoProfile', '-Command', f"Start-ScheduledTask -TaskName '{argv[1]}'"]
    return subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL, **kw)


def steps(deploy, task):
    hub = Path(deploy) / 'hub'
    return [f'uv sync --project "{hub}"            (only when hub/pyproject.toml or uv.lock changed)',
            f'stop the hub: kill only the pid listening on the UI port (taskkill /PID <pid> /F, never /T)',
            f'one time only, point the task at the deployment: '
            f'powershell -File "{hub / "scripts" / "Install-DroverHub.ps1"}" -HubDir "{hub}"',
            f"Start-ScheduledTask -TaskName {task}"]


def restart(run, deploy, uv, port, task):
    hub = Path(deploy) / 'hub'
    done = run([uv, 'sync', '--project', str(hub)])
    if done.returncode:
        raise DeployError(f'uv sync failed: {(done.stderr or "").strip()}; the old hub is still running')
    pids = [p for p in (run(['LISTENERS', port]).stdout or '').split() if p.isdigit()]
    for pid in pids:
        run(['STOP', pid])
    run(['START', task])
    return pids


def main(argv=None, run=default_run):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--source', default=str(Path(__file__).resolve().parents[1]), help='the checkout Drover lands into')
    ap.add_argument('--deploy', default=os.environ.get('DROVER_HUB_DEPLOY'), help='deployment worktree (or DROVER_HUB_DEPLOY)')
    ap.add_argument('--commit', help='commit to deploy (default: the branch tip)')
    ap.add_argument('--branch', default='main')
    ap.add_argument('--receipt', help='gate receipt JSON for exactly this commit ({"sha":..., "passed": true})')
    ap.add_argument('--force', action='store_true', help='skip the on-branch and gate-receipt checks')
    ap.add_argument('--restart', action='store_true', help='sync the hub venv, stop the hub pid, start the task')
    ap.add_argument('--port', default='8765', help='hub UI port (hub.ui_port)')
    ap.add_argument('--task', default='DroverHub', help='scheduled task name')
    ap.add_argument('--uv', default='uv')
    a = ap.parse_args(argv)
    if not a.deploy:
        ap.error('--deploy (or DROVER_HUB_DEPLOY) is required')
    try:
        sha = resolve(a.source, a.commit, a.branch)
        bad = check_commit(a.source, sha, a.branch, a.receipt, a.force)
        if bad:
            raise DeployError('refusing to deploy:\n  ' + '\n  '.join(bad))
        print(f'{move_worktree(a.source, a.deploy, sha)} {a.deploy} at {sha}')
        if a.restart:
            pids = restart(run, a.deploy, a.uv, a.port, a.task)
            print(f'stopped hub pid(s) {pids or "none"}; started task {a.task}')
        else:
            print('Not restarted. To switch the running hub:')
            for line in steps(a.deploy, a.task):
                print('  ' + line)
    except DeployError as error:
        print(error)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
