"""Disk cleanup runtime: worker calls across sides and the detached cleanup entry point.

Platform facts and process primitives come from placement; nothing in placement calls back into disk.
"""
import copy
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import config, disk_state, placement
from .disk_cleanup_decisions import CleanupRuntime


def cleanup_runtime(cfg):
    from .disk import _free
    from .resources import cleanup_pressure
    return CleanupRuntime(time.time, _free, cleanup_call, placement.disk_toast, lambda: cleanup_pressure(cfg))


def cleanup_call(side: str, request: dict) -> dict:
    """Run a fixed JSON worker; only declared paths are translated across sides."""
    if side not in ('windows', 'wsl'):
        raise ValueError('invalid cleanup side')
    if not placement.IS_WINDOWS and side == 'windows':
        errors = [dict(reason_code='side_unavailable')] if os.environ.get('WSL_DISTRO_NAME') else []
        return dict(items=[], errors=errors, reclaimed_bytes=0)
    request = copy.deepcopy(request)
    original_state = request['state_dir']
    token = uuid.uuid4().hex
    request['_worker_id'] = token
    request['side'] = side
    root = str(Path(__file__).resolve().parents[1])
    if placement.IS_WINDOWS and side == 'wsl':
        convert = placement.to_wsl_path
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
                                     if placement.cleanup_path_side(p) == side]
        request['paths']['caches'] = [c for c in request['paths'].get('caches', [])
                                      if placement.cleanup_path_side(c.get('path', c.get('cwd'))) == side]
        for cache in request['paths'].get('caches', []):
            key = 'path' if 'path' in cache else 'cwd'
            cache[key] = convert(cache[key])
        name = placement.distro()
        # Resolve HKCU on the Windows hub. A non-interactive WSL worker may not
        # have Windows interop available to run reg.exe, even though Git and its
        # ext4 worktrees are accessible. Use the existing physical-disk override
        # for this invocation only; never infer the host from the requested drive.
        try:
            host = placement.detect_vhdx_drives().get(name)
        except (ImportError, OSError):
            host = None
        letter = placement.drive(host + '/') if host else None
        worker_env = ['DROVER_RUN_ID=' + token]
        if letter:
            worker_env.append('DROVER_VHDX_DIR=' + placement.to_wsl_path(letter + ':/'))
        argv = placement.wsl_control(['-d', name, '--', 'env', *worker_env, 'python3'])
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
        job = placement._CleanupJob(token) if placement.IS_WINDOWS and side == 'windows' else None
        command = [*argv, '-c', code, root, token]
        if placement.IS_WINDOWS and side == 'wsl':
            command = placement.wsl_control(command[1:])
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
                if placement.IS_WINDOWS and side == 'wsl':
                    # Reaping wsl.exe alone does not prove the Linux worker exited.
                    # Verify absence of this execution's marker on the owning side.
                    proof = subprocess.run(placement.WSL().command(['python3', '-c', placement._CLEANUP_STARTUP_PROOF, token]),
                                           capture_output=True, timeout=30, **placement.NO_WINDOW)
                    confirmed = proof.returncode == 0
                else:
                    confirmed = True
            elif not job:
                confirmed = placement.recover_ci_children(side, status, str(Path(__file__).resolve().parents[1]))
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
                raise placement.CleanupWorkerError(json.dumps(evidence))
    if pending is not None:
        raise pending
    if answer is not None:
        return answer
    return dict(items=[], errors=[evidence], reclaimed_bytes=0)


def cleanup_enqueue(cfg, claim):
    """Detached fixed cleanup entry point pinned independently of the hub cwd."""
    code = ('import json,sys;sys.path.insert(0,sys.argv[1]);'
            'from drover.disk_cleanup import run;from drover.placement import lower_priority;lower_priority();'
            'run(json.loads(sys.argv[2]),mode="triggered",apply=sys.argv[3]=="1")')
    argv = [sys.executable, '-c', code, str(Path(__file__).resolve().parents[1]),
            json.dumps(cfg), '1' if claim['applied'] else '0', claim['id']]
    log = Path(config.state_dir(cfg)) / 'disk' / 'cleanup.log'
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open('ab') as stream:
        proc, _ = placement.detach(argv, os.environ.copy(), stream)
    try:
        identity = placement.cleanup_identity(proc.pid)
    except (subprocess.SubprocessError, OSError):
        identity = None   # the process is running: a slow query must not release its claim and start a second one
    if not identity or identity.get('birth') in (None, 'absent'):
        identity = dict(pid=proc.pid, birth=None, side=placement.cleanup_side())   # resolved later by the hub tick
    return dict(identity=identity)
