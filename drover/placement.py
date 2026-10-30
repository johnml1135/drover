"""Placement: Windows and WSL command execution, paths and process ownership.

Commands are argument lists. Only explicitly identified paths are translated;
configured worker/check argv is never rewritten. The hub binds its live settings
once; all callers then resolve the same ledger override above configuration.
"""
import os
import re
import subprocess
import sys
import json
import signal
import shutil
import time
from contextlib import contextmanager
from pathlib import Path

from .placement_paths import (
    IS_WINDOWS, MNT_RE, WIN_RE, _ordinal_case, _ordinal_char, canonical, cleanup_link, cleanup_normalize,
    cleanup_parts, cleanup_path, cleanup_path_side, cleanup_side, config_path, drive, is_linux_native,
    native, path_parts, routes_through_wsl, sandbox_roots, to_windows_path, to_wsl_path,
    windows_fold_uncertain, windows_system_path, worktree_side, wsl_worktrees_root,
)
from .placement_tree import (
    KILL_AFTER_S, PosixTree, WindowsTree,
)
from .placement_exec import (
    CREATE_BREAKAWAY_FROM_JOB, CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW, CREATE_SUSPENDED, NO_WINDOW,
    TELEMETRY_OPTOUT, WSL, Windows, _name, _value, _wslenv, bind_settings, ci_command,
    ci_invocation, ci_path, ci_runner_command,
    detach, distro, executable, native_runner_cwd, spawn_worker, wsl_control,
)
from .placement_git import (
    _METADATA_IO, _PATH_OPTS, _WORKTREE_PATH_SUBS, _git_command, _git_location, _map_paths, git,
    git_argv, prepare_worktree, reachable, relativize_gitdir, status_args, worktree_metadata,
)
from .placement_process import (
    ScanError, _cim_rows, _cleanup_cim, _powershell, _proc_cmdlines_linux,
    _proc_cmdlines_windows, stop_ci_children, _read_windows_birth, _split_windows_cmdline,
    _windows_birth, _windows_cwd, _windows_processes, cleanup_identity, cleanup_process_record,
    process_cmdlines,
)
from .placement_proof import (
    _CleanupProcesses, _PROCESS_IGNORE_R11_WINDOWS, _PROCESS_IGNORE_WSL, _process_os_location,
    cleanup_processes, default_process_ignore, seed_cleanup_process_policy,
)
from .placement_host import (
    CLEANUP_CACHE_COMMANDS, CLEANUP_CACHE_FOLDERS, CLEANUP_SCRATCH_ROOTS, LXSS, REG, _VHDX_CACHE,
    _vhdx_dir, _vhdx_reading, _wsl_vhdx_paths, cleanup_vhdx_report, decode_output, default_disk_trackers,
    detect_distros, detect_vhdx_drives, detected_cleanup_policy, is_drvfs, linux_mount, parse_distros,
    physical_disk_path, trim_argv, vhdx_dir, wsl_zoneinfo_path,
)
from .placement_worker import (
    BELOW_NORMAL_PRIORITY_CLASS, CLEANUP_NICE, CleanupWorkerError, _CLEANUP_STARTUP_PROOF, _CleanupJob,
    cleanup_worker, disk_toast, lower_priority,
)

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes
    from .placement_tree import _k32, _BASIC, _IOC, _EXT, _resume, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, JOB_BASIC_AND_IO_ACCOUNTING_CLASS, JOB_EXTENDED_LIMIT_CLASS


def cleanup_runtime(cfg):
    from .disk import _free
    from .disk_cleanup import CleanupRuntime
    from .resources import cleanup_pressure
    return CleanupRuntime(time.time, _free, cleanup_call, disk_toast, lambda: cleanup_pressure(cfg))


def cleanup_call(side: str, request: dict) -> dict:
    """Run a fixed JSON worker; only declared paths are translated across sides."""
    if side not in ('windows', 'wsl'):
        raise ValueError('invalid cleanup side')
    if not IS_WINDOWS and side == 'windows':
        errors = [dict(reason_code='side_unavailable')] if os.environ.get('WSL_DISTRO_NAME') else []
        return dict(items=[], errors=errors, reclaimed_bytes=0)
    import copy
    import uuid
    from . import disk_state
    request = copy.deepcopy(request)
    original_state = request['state_dir']
    token = uuid.uuid4().hex
    request['_worker_id'] = token
    request['side'] = side
    root = str(Path(__file__).resolve().parents[1])
    if IS_WINDOWS and side == 'wsl':
        convert = to_wsl_path
        request['state_dir'] = convert(request['state_dir'])
        snapshot = request.get('tasks') or {}
        if snapshot:
            snapshot['active_worktrees'] = [convert(p) for p in snapshot.get('active_worktrees', [])]
            for task in snapshot.get('tasks', []):
                if task.get('worktree'):
                    task['worktree'] = convert(task['worktree'])
        for repo in request['repos']:
            for key in ('repo', 'worktrees'):
                if repo.get(key):
                    repo[key] = convert(repo[key])
        for key in ('temp_roots', 'report_roots'):
            request['paths'][key] = [convert(p) for p in request['paths'].get(key, [])
                                     if cleanup_path_side(p) == side]
        request['paths']['caches'] = [c for c in request['paths'].get('caches', [])
                                      if cleanup_path_side(c.get('path', c.get('cwd'))) == side]
        for cache in request['paths'].get('caches', []):
            key = 'path' if 'path' in cache else 'cwd'
            cache[key] = convert(cache[key])
        name = distro()
        # Resolve HKCU on the Windows hub. A non-interactive WSL worker may not
        # have Windows interop available to run reg.exe, even though Git and its
        # ext4 worktrees are accessible. Use the existing physical-disk override
        # for this invocation only; never infer the host from the requested drive.
        try:
            host = detect_vhdx_drives().get(name)
        except (ImportError, OSError):
            host = None
        letter = drive(host + '/') if host else None
        worker_env = ['DROVER_RUN_ID=' + token]
        if letter:
            worker_env.append('DROVER_VHDX_DIR=' + to_wsl_path(letter + ':/'))
        argv = wsl_control(['-d', name, '--', 'env', *worker_env, 'python3'])
        root = convert(root)
    else:
        argv = [sys.executable]
    # Pin the application root independently of cwd or the hub's environment.
    code = ('import json,sys;sys.path.insert(0,sys.argv[1]);'
            'from drover.placement import cleanup_worker;cleanup_worker(json.load(sys.stdin))')
    job, done, status, answer, pending = None, None, None, None, None
    evidence = {}
    reason, output, detail = 'worker_failed', '', ''
    confirmed, startup_failure = False, False
    def text(value):
        return value.decode(errors='replace') if isinstance(value, bytes) else value or ''
    try:
        job = _CleanupJob(token) if IS_WINDOWS and side == 'windows' else None
        command = [*argv, '-c', code, root, token]
        if IS_WINDOWS and side == 'wsl':
            command = wsl_control(command[1:])
        done = subprocess.run(command, input=json.dumps(request),
                              capture_output=True, text=True, timeout=request['paths'].get('worker_timeout_s', 1800), shell=False,
                              env={**os.environ, 'DROVER_RUN_ID': token})
        output, detail = done.stdout, text(done.stderr)
        if not done.returncode:
            answer = json.loads(done.stdout)
            if not isinstance(answer, dict):
                answer = None
                raise ValueError('invalid cleanup worker result')
    except subprocess.TimeoutExpired as error:
        reason, output, detail = 'worker_timeout', error.stdout, text(error.stderr)
    except OSError as error:
        reason, detail = 'worker_unavailable', str(error)
    except BaseException as error:
        detail = str(error) or type(error).__name__
        # Invalid JSON remains an ordinary worker failure; cancellation and decode
        # failures propagate only after the containment has been drained.
        if not isinstance(error, ValueError) or isinstance(error, UnicodeError):
            pending = error
    finally:
        try:
            try:
                try:
                    status = json.loads((Path(original_state) / 'disk' / ('worker-' + token + '.json')).read_text(encoding='utf-8'))
                except FileNotFoundError:
                    # The identity file is the destructive-work claim: cleanup_worker
                    # must persist it before entering serve(). A completed, failed
                    # subprocess with no claim never entered any stage. run() reaped
                    # it; unlike timeout/cancellation this proves startup termination.
                    if done is None or not done.returncode:
                        raise
                    startup_failure = True
                    reason = 'worker_error'
                    status = None
                if not startup_failure and (not isinstance(status, dict) or type(status.get('pid')) is not int or status['pid'] <= 0 or \
                        status.get('run_id') != token):
                    raise ValueError('worker identity unavailable')
            finally:
                # Drain the job even if reading worker identity is interrupted.
                if job:
                    confirmed = job.terminate()
            if startup_failure and not job:
                if IS_WINDOWS and side == 'wsl':
                    # Reaping wsl.exe alone does not prove the Linux worker exited.
                    # Verify absence of this execution's marker on the owning side.
                    proof = subprocess.run(WSL().command(['python3', '-c', _CLEANUP_STARTUP_PROOF, token]),
                                           capture_output=True, timeout=30, **NO_WINDOW)
                    confirmed = proof.returncode == 0
                else:
                    confirmed = True
            elif not job:
                confirmed = recover_ci_children(side, status, str(Path(__file__).resolve().parents[1]))
        except BaseException as error:
            confirmed = False
            detail += '; termination: ' + (str(error) or type(error).__name__)
        finally:
            if job:
                try:
                    job.close()
                except BaseException as error:
                    confirmed = False
                    detail += '; job close: ' + (str(error) or type(error).__name__)
        if not confirmed or answer is None or pending is not None:
            evidence = dict(reason_code='worker_termination_unconfirmed' if answer is not None else reason,
                            output=text(output)[-4000:], error=text(detail)[-4000:],
                            termination_confirmed=confirmed, work_started=not startup_failure)
            try:
                disk_state.save_file(original_state, 'worker-error-' + token + '.json', evidence)
            except BaseException as error:
                evidence['persistence_error'] = str(error) or type(error).__name__
                if confirmed and not isinstance(error, OSError):
                    raise
            if not confirmed:
                raise CleanupWorkerError(json.dumps(evidence))
    if pending is not None:
        raise pending
    if answer is not None:
        return answer
    return dict(items=[], errors=[evidence], reclaimed_bytes=0)


def cleanup_scope(request):
    """Keep only this physical drive; path spelling/translation lives here."""
    import copy
    scoped = copy.deepcopy(request)
    scoped['scope_errors'] = []
    if physical_disk_path(request['drive']['path']) is None:
        scoped['scope_errors'].append(dict(path=request['drive']['path'], reason_code='physical_disk_unknown'))
    def belongs(path):
        if physical_disk_path(path) is None or physical_disk_path(request['drive']['path']) is None:
            scoped['scope_errors'].append(dict(path=str(path), reason_code='physical_disk_unknown'))
            return False
        return cleanup_contains(request, path)
    # Repositories may have worktrees on another drive. Stages scope every candidate,
    # rather than dropping the repository that owns that drive's registrations.
    for name in ('temp_roots', 'report_roots'):
        roots = request['paths'].get(name, [])
        scoped['paths'][name] = list(dict.fromkeys(native(p) for p in roots if belongs(p)))
    scoped['paths']['caches'] = [entry for entry in scoped['paths'].get('caches', [])
                                if entry['placement'] == cleanup_side() and belongs(entry.get('path', entry.get('cwd')))]
    for entry in scoped['paths']['caches']:
        key = 'path' if 'path' in entry else 'cwd'
        entry[key] = native(entry[key])
    return scoped


def cleanup_contains(request, path):
    """Whether this concrete candidate belongs to the requested physical drive."""
    if cleanup_path_side(path) != cleanup_side():
        return False
    declared = request['drive']['path']
    host = physical_disk_path(path)
    drive_host = physical_disk_path(declared) if declared is not None else None
    if host is not None and drive_host is not None:
        return cleanup_normalize(host) == cleanup_normalize(drive_host)
    return False


def cleanup_claim_alive(identity):
    if (identity.get('side') != cleanup_side() or type(identity.get('pid')) is not int
            or identity['pid'] <= 0 or not identity.get('birth')):
        return None
    if identity.get('birth') == 'absent':
        return False
    if identity['side'] == 'wsl' and ':' not in identity['birth']:
        return None  # Old start ticks without boot identity cannot prove a process dead.
    current = cleanup_identity(identity['pid'])
    return None if not current['birth'] else current['birth'] == identity['birth']


def cleanup_enqueue(cfg, claim):
    """Detached fixed cleanup entry point pinned independently of the hub cwd."""
    code = ('import json,sys;sys.path.insert(0,sys.argv[1]);'
            'from drover.disk_cleanup import run;from drover.placement import lower_priority;lower_priority();'
            'run(json.loads(sys.argv[2]),mode="triggered",apply=sys.argv[3]=="1")')
    argv = [sys.executable, '-c', code, str(Path(__file__).resolve().parents[1]),
            json.dumps(cfg), '1' if claim['applied'] else '0', claim['id']]
    from . import config
    log = Path(config.state_dir(cfg)) / 'disk' / 'cleanup.log'
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open('ab') as stream:
        proc, _ = detach(argv, os.environ.copy(), stream)
    try:
        identity = cleanup_identity(proc.pid)
    except (subprocess.SubprocessError, OSError):
        identity = None   # the process is running: a slow query must not release its claim and start a second one
    if not identity or identity.get('birth') in (None, 'absent'):
        identity = dict(pid=proc.pid, birth=None, side=cleanup_side())   # resolved later by the hub tick
    return dict(identity=identity)


def cleanup_free(path):
    """Read the owning side's physical drive, never a virtual filesystem substitute."""
    physical = physical_disk_path(str(path))
    if physical is None:
        return None
    try:
        return shutil.disk_usage(physical).free / 1024 ** 3
    except OSError:
        return None


def add_worktree(repo, dest, *tail, run=None, error=RuntimeError):
    """Create a worktree; old git may use absolute pointers only on a native filesystem."""
    if run is None:
        def run(repo, *args, check=False, **kw):
            return git(repo, *args, timeout=60, **kw)
    # An ext4 destination is written by the WSL git so its `.git` file and the repository's `gitdir` record are
    # paths WSL can resolve; Windows git would turn the Linux path into a Windows one.
    side = {'side': 'wsl'} if routes_through_wsl(dest) else {}
    # Locked at birth with the creating side's name so the other OS's `git worktree prune` cannot drop it.
    lock = ('--lock', '--reason', 'wsl' if routes_through_wsl(dest) or not IS_WINDOWS else 'windows')
    r = run(repo, '-c', 'core.autocrlf=false', 'worktree', 'add', *lock, '--relative-paths', *tail, check=False, **side)
    rejected = re.search(r'(unknown|unrecognized|unsupported|invalid) (option|argument|switch)[^\n]*relative-paths',
                         r.stderr, re.IGNORECASE)
    if r.returncode and rejected:
        if not is_linux_native(dest):
            raise error('this git has no `worktree add --relative-paths`, and a worktree under '
                        f'{dest} is opened from both Windows and WSL: update git (2.48 or newer)')
        r = run(repo, '-c', 'core.autocrlf=false', 'worktree', 'add', *lock, *tail, check=False, **side)
    if r.returncode:
        raise error(f'git worktree add failed: {r.stderr.strip()[-300:]}')


def disk_schedule_plan(cfg: dict, *, apply: bool = False) -> dict:
    """Generate only: pin interpreter, import root, machine home and shared state."""
    if not IS_WINDOWS and os.environ.get('WSL_DISTRO_NAME'):
        raise ValueError('Install disk schedules using native Windows Python so the physical reader survives WSL shutdown')
    from . import config
    import datetime
    import getpass
    import xml.etree.ElementTree as ET
    files, commands = {}, []
    root = str(Path(__file__).resolve().parents[1])
    home, state = str(config.home().absolute()), str(config.state_dir(cfg).absolute())
    code = ('import os,runpy,sys;sys.path.insert(0,sys.argv[1]);'
            'os.environ["DROVER_HOME"]=sys.argv[2];'
            'os.environ["DROVER_PATHS_STATE_DIR"]=sys.argv[3];'
            'sys.argv=["drover",*sys.argv[4:]];runpy.run_module("drover",run_name="__main__")')
    mode = '--apply' if apply else '--dry-run'
    actions = [('DroverDiskTracker', 'drover-disk-tracker', ['disk-tracker', 'tick', mode]),
               ('DroverDiskCleanup', 'drover-disk-cleanup', ['disk-cleanup', '--mode', 'daily', mode])]
    for name, unit, args in actions:
        argv = [str(Path(sys.executable).absolute()), '-c', code, root, home, state, *args]
        if IS_WINDOWS:
            ns = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
            ET.register_namespace('', ns)
            def node(parent, tag, text=None, **attributes):
                element = ET.SubElement(parent, '{' + ns + '}' + tag, attributes)
                element.text = text
                return element
            task = ET.Element('{' + ns + '}Task', version='1.4')
            triggers = node(task, 'Triggers')
            start = datetime.datetime.now().date().isoformat() + 'T03:00:00'
            if unit.endswith('tracker'):
                trigger = node(triggers, 'TimeTrigger')
                repetition = node(trigger, 'Repetition')
                node(repetition, 'Interval', 'PT15M')
                node(repetition, 'StopAtDurationEnd', 'false')
            else:
                trigger = node(triggers, 'CalendarTrigger')
            node(trigger, 'StartBoundary', start)
            node(trigger, 'Enabled', 'true')
            if not unit.endswith('tracker'):
                node(node(trigger, 'ScheduleByDay'), 'DaysInterval', '1')
            user = os.environ.get('USERNAME') or getpass.getuser()
            domain = os.environ.get('USERDOMAIN')
            user = domain + chr(92) + user if domain else user
            principal = node(node(task, 'Principals'), 'Principal', id='CurrentUser')
            node(principal, 'UserId', user)
            node(principal, 'LogonType', 'InteractiveToken')
            node(principal, 'RunLevel', 'LeastPrivilege')
            settings = node(task, 'Settings')
            for key, value in [('MultipleInstancesPolicy', 'IgnoreNew'), ('StartWhenAvailable', 'true'),
                               ('DisallowStartIfOnBatteries', 'false'), ('StopIfGoingOnBatteries', 'false'),
                               ('ExecutionTimeLimit', 'PT0S')]:
                node(settings, key, value)
            action = node(node(task, 'Actions', Context='CurrentUser'), 'Exec')
            node(action, 'Command', argv[0])
            node(action, 'Arguments', subprocess.list2cmdline(argv[1:]))
            node(action, 'WorkingDirectory', root)
            path = str(Path(state) / 'disk' / (name + '.xml'))
            files[path] = ET.tostring(task, encoding='unicode', xml_declaration=True)
            commands.append(['schtasks.exe', '/Create', '/TN', name, '/XML', path, '/F'])
        else:
            directory = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'systemd' / 'user'
            def quote(value):
                # systemd performs its own percent and dollar expansion, without a shell.
                return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$').replace('\n', '\\n').replace('\r', '\\r') + '"'
            command = ' '.join(quote(value) for value in argv)
            files[str(directory / (unit + '.service'))] = (
                '[Unit]\nDescription=' + name + '\n[Service]\nType=oneshot\nExecStart=' + command + '\n')
            calendar = '*-*-* *:0/15:00' if unit.endswith('tracker') else '*-*-* 03:00:00'
            files[str(directory / (unit + '.timer'))] = (
                '[Unit]\nDescription=' + name + '\n[Timer]\nOnCalendar=' + calendar +
                '\nPersistent=true\n[Install]\nWantedBy=timers.target\n')
    if not IS_WINDOWS:
        commands = [['systemctl', '--user', 'daemon-reload'],
                    ['systemctl', '--user', 'enable', '--now', 'drover-disk-tracker.timer', 'drover-disk-cleanup.timer']]
    return dict(files=files, argv=commands)


def unlock_worktree(repo, path):
    """Release only this creator's side marker; owner notes remain locked."""
    expected = 'wsl' if routes_through_wsl(path) or not IS_WINDOWS else 'windows'
    side = {'side': 'wsl'} if expected == 'wsl' else {}      # only the WSL git spells ext4 worktrees correctly
    listed = git(repo, 'worktree', 'list', '--porcelain', '-z', timeout=60, **side)
    if listed.returncode:
        return
    for block in listed.stdout.split('\0\0'):
        fields = dict(field.partition(' ')[::2] for field in block.split('\0') if field)
        if fields.get('worktree') and cleanup_normalize(fields['worktree']) == cleanup_normalize(path) and fields.get('locked') == expected:
            git(repo, 'worktree', 'unlock', str(path), timeout=60, **side)


def recover_ci_children(platform, status, drover_dir, distro_name=None):
    """Confirm child termination on the execution's placement, including remote WSL."""
    if IS_WINDOWS and platform == 'wsl':
        code = ('import json,sys; from drover.placement import stop_ci_children; '
                'sys.exit(0 if stop_ci_children(json.loads(sys.argv[1])) else 1)')
        command = WSL(distro_name).command(['python3', '-c', code, json.dumps({key: status.get(key) for key in ('pid', 'runner_pid', 'run_id')})], cd=drover_dir)
        result = subprocess.run(command, capture_output=True, timeout=90,
                                stdin=subprocess.DEVNULL, **NO_WINDOW)
        if result.returncode not in (0, 1):
            raise ScanError('cannot confirm WSL CI child termination')
        return result.returncode == 0
    return stop_ci_children(status)
