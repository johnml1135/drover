"""Deterministic trace of the cleanup orchestration and side decisions; no real process is spawned.

The trace is frozen in placement_trace.json and must read the same on Windows and WSL: every host path is
replaced by a placeholder in raw, JSON-escaped and slash spellings, and separators are normalised.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from drover import config, disk, disk_preflight, disk_runtime, disk_schedule_steps, placement, placement_exec, resources

RUNTIME = disk_runtime     # cleanup_runtime, cleanup_call, cleanup_enqueue
SCHEDULE = disk_schedule_steps
PLAN = 'schedule_plan'
TOKEN = 't0k3n'


def normalize(value, subs):
    if isinstance(value, dict):
        return {normalize(k, subs): normalize(v, subs) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize(v, subs) for v in value]
    if not isinstance(value, str):
        return value
    for raw, placeholder in subs:
        for spelling in (raw.replace('\\', '\\\\'), raw, raw.replace('\\', '/')):
            value = value.replace(spelling, placeholder)
    return re.sub(r'\d{4}-\d{2}-\d{2}T03:00:00', '<DATE>T03:00:00', re.sub(r'\\+', '/', value))


class Recorder:
    """Stands in for subprocess.run: records each command and writes the worker's identity claim."""

    def __init__(self, state, answers):
        self.state, self.answers, self.calls = state, list(answers), []

    def __call__(self, command, **kw):
        answer = self.answers.pop(0)
        call = dict(command=list(command), input=json.loads(kw['input']) if kw.get('input') else None,
                    timeout=kw.get('timeout'), run_id=(kw.get('env') or {}).get('DROVER_RUN_ID'),
                    kwargs=sorted(set(kw) - {'input', 'timeout', 'env', 'creationflags'}))
        self.calls.append(call)
        if isinstance(answer, BaseException):
            raise answer
        if answer.get('claim'):
            target = self.state / 'disk'
            target.mkdir(parents=True, exist_ok=True)
            (target / f'worker-{TOKEN}.json').write_text(json.dumps(dict(pid=4242, run_id=TOKEN)), encoding='utf-8')
        return subprocess.CompletedProcess(command, answer.get('rc', 0), answer.get('out', ''), answer.get('err', ''))


def collect():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp).resolve()
        state = tmp / 'state'
        state.mkdir()
        root = str(Path(RUNTIME.__file__).resolve().parents[1])
        subs = [(sys.executable, '<PY>'), (str(tmp), '<TMP>'), (root, '<ROOT>')]
        trace = {}
        cfg = config.validate({'paths': {'state_dir': str(state)}})
        request = {'state_dir': str(state), 'repos': [], 'tasks': {}, 'paths': {'worker_timeout_s': 11}}
        recovered = []

        def recover(side, status, directory, *rest):
            recovered.append([side, status, directory, *rest])
            return True

        def worker(side, answers, windows=False, request=request):
            """Run cleanup_call under a fake subprocess; return what it did and what it answered."""
            run = Recorder(state, answers)
            recovered.clear()
            patches = [patch.object(placement, 'IS_WINDOWS', windows), patch.object(placement, 'recover_ci_children', recover),
                       patch('subprocess.run', run), patch('uuid.uuid4', return_value=SimpleNamespace(hex=TOKEN))]
            if windows:
                patches += [patch.object(placement, 'to_wsl_path', lambda p: '/w/' + str(p)),
                            patch.object(placement, 'cleanup_path_side', lambda p: 'wsl' if str(p).startswith('/') else 'windows'),
                            patch.object(placement, 'distro', lambda: 'Dist'),
                            patch.object(placement, 'detect_vhdx_drives', lambda: {'Dist': 'R:/disks'})]
            try:
                for item in patches:
                    item.start()
                try:
                    answer = RUNTIME.cleanup_call(side, request)
                except BaseException as error:
                    answer = dict(raised=type(error).__name__, message=str(error))
            finally:
                for item in reversed(patches):
                    item.stop()
            errors = [dict(e, error=e.get('error', '').split('; termination:')[0]) for e in answer.get('errors', [])] \
                if isinstance(answer, dict) and 'errors' in answer else None
            if errors is not None:
                answer = dict(answer, errors=errors)
            if answer.get('raised') == 'CleanupWorkerError':
                evidence = json.loads(answer['message'])
                evidence['error'] = evidence['error'].split('; termination:')[0]
                answer = dict(answer, message=evidence)
            saved = state / 'disk' / f'worker-error-{TOKEN}.json'
            persisted = json.loads(saved.read_text(encoding='utf-8')) if saved.exists() else None
            if persisted:
                persisted['error'] = persisted['error'].split('; termination:')[0]
            for leftover in (state / 'disk').glob('worker-*'):
                leftover.unlink()
            return dict(calls=run.calls, recovered=list(recovered), answer=answer, persisted=persisted)

        ok = dict(rc=0, out=json.dumps(dict(items=[dict(path='x')], errors=[], reclaimed_bytes=7)), claim=True)
        trace['invalid_side'] = worker('mac', [])['answer']
        for name in ('', 'Ubuntu'):
            with patch.object(placement, 'IS_WINDOWS', False), patch.dict(os.environ, {'WSL_DISTRO_NAME': name} if name else {}):
                if not name:
                    os.environ.pop('WSL_DISTRO_NAME', None)
                trace['windows_side_on_' + (name or 'no_wsl')] = RUNTIME.cleanup_call('windows', request)
        trace['native_ok'] = worker('wsl', [ok])
        trace['native_startup_failure'] = worker('wsl', [dict(rc=3, out='partial', err='boom')])
        trace['native_timeout'] = worker('wsl', [subprocess.TimeoutExpired('worker', 11, output=b'out', stderr=b'err')])
        trace['native_bad_json'] = worker('wsl', [dict(rc=0, out='not json', claim=True)])
        translated = {'state_dir': str(state), 'repos': [dict(repo='C:/r', worktrees='D:/w'), dict(repo='')],
                      'tasks': dict(active_worktrees=['E:/a'], tasks=[dict(worktree='E:/t'), dict(id='no-worktree')]),
                      'paths': dict(temp_roots=['/lin/t', 'H:/win/t'], report_roots=['/lin/r'], worker_timeout_s=9,
                                    caches=[dict(path='/lin/c', placement='wsl'), dict(cwd='H:/c', placement='windows')])}
        with patch.object(placement_exec, '_settings', lambda: {'wsl_distro': 'Dist'}):
            trace['windows_to_wsl_ok'] = worker('wsl', [ok], windows=True, request=translated)
            trace['windows_to_wsl_startup_failure'] = worker('wsl', [dict(rc=3), dict(rc=0)], windows=True, request=translated)
            trace['windows_to_wsl_unproven_startup'] = worker('wsl', [dict(rc=3), dict(rc=1)], windows=True, request=translated)
        trace['request_unmodified'] = translated['state_dir'] == str(state) and translated['paths']['temp_roots'][0] == '/lin/t'

        launched = []

        def detach(argv, env, stream):
            launched.append(dict(argv=argv, env_has_path='PATH' in env, stream=Path(stream.name).name))
            return SimpleNamespace(pid=77), False
        enqueue = {}
        for label, identity in (('identity', {'pid': 77, 'birth': '7:abc', 'side': 'wsl'}), ('absent', {'pid': 77, 'birth': 'absent', 'side': 'wsl'}),
                                ('none', None), ('error', OSError('slow'))):
            lookup = (lambda pid: (_ for _ in ()).throw(identity)) if isinstance(identity, BaseException) else (lambda pid: identity)
            launched.clear()
            with patch.object(placement, 'detach', detach), patch.object(placement, 'cleanup_identity', lookup), \
                    patch.object(placement, 'cleanup_side', lambda: 'wsl'):
                answer = RUNTIME.cleanup_enqueue(cfg, dict(applied=label == 'identity', id='claim-' + label))
            call = launched[0]
            enqueue[label] = dict(answer=answer, stream=call['stream'], env_has_path=call['env_has_path'],
                                  argv=call['argv'][:4] + [json.loads(call['argv'][4]) == cfg] + call['argv'][5:])
        enqueue['log_directory'] = sorted(p.name for p in (state / 'disk').glob('cleanup.log'))
        trace['enqueue'] = enqueue

        pressure = []
        with patch.object(resources, 'cleanup_pressure', lambda c: pressure.append(c) or {'verdict': 'go'}):
            runtime = RUNTIME.cleanup_runtime(cfg)
            verdict = runtime.pressure()
        import time
        trace['runtime'] = dict(now_is_time=runtime.now is time.time, read_is_free=runtime.read is disk._free,
                                call_is_cleanup_call=runtime.call is RUNTIME.cleanup_call,
                                toast_is_disk_toast=runtime.toast is placement.disk_toast, pressure=verdict,
                                pressure_got_cfg=pressure == [cfg], evidence=runtime.evidence, state=runtime.state,
                                fields=sorted(vars(runtime)))

        plan = getattr(SCHEDULE, PLAN)
        home = tmp / 'home'
        env = {'XDG_CONFIG_HOME': str(tmp / 'xdg'), 'USERNAME': 'U', 'USERDOMAIN': 'D'}
        schedules = {}
        with patch.object(config, 'home', return_value=home), patch.dict(os.environ, env):
            os.environ.pop('WSL_DISTRO_NAME', None)
            for label, windows in (('linux', False), ('windows', True)):
                with patch.object(placement, 'IS_WINDOWS', windows):
                    for apply in (False, True):
                        schedules[f'{label}_{"apply" if apply else "dry"}'] = plan(cfg, apply=apply)
            with patch.object(placement, 'IS_WINDOWS', False), patch.dict(os.environ, {'WSL_DISTRO_NAME': 'Ubuntu'}):
                try:
                    plan(cfg)
                except ValueError as error:
                    schedules['wsl_host'] = str(error)
        trace['schedule'] = schedules

        # The '.git' / '.venv' part refusal is deliberately not frozen here: on Windows cleanup_parts upper-cases
        # the parts, so the lowercase part check never fires there (a known defect, left alone in a MOVE). It is
        # covered on WSL/Linux by test_placement.ProtectedPathParts. Only repository_tree protects on Windows.
        refusals = {}
        folders = {'state': state / 'inner', 'fine': tmp / 'fine'}
        for label, folder in folders.items():
            policy = dict(cfg['disk_cleanup'], caches=[dict(path=str(folder), placement='wsl')])
            with patch.object(disk_preflight, 'repository_tree', return_value=False):
                try:
                    disk_preflight.check_cache_folders(dict(cfg, disk_cleanup=policy))
                    refusals[label] = 'accepted'
                except config.ConfigError as error:
                    refusals[label] = str(error)
        policy = dict(cfg['disk_cleanup'], caches=[dict(path=str(tmp / 'fine'), placement='wsl')])
        with patch.object(disk_preflight, 'repository_tree', return_value=True):
            try:
                disk_preflight.check_cache_folders(dict(cfg, disk_cleanup=policy))
            except config.ConfigError as error:
                refusals['repository'] = str(error)
        trace['protected_roots'] = refusals
        return normalize(trace, subs)
