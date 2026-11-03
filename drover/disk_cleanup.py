"""Machine-wide staged cleanup; side workers own candidate safety decisions."""
import copy
import hashlib
import json
import math
import os
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from . import config, disk, disk_budget, disk_evidence, disk_runtime, disk_state, durable, placement, repos
from .disk_cleanup_decisions import CleanupRuntime, CleanupState, decide, decide_and_store, evaluate
from .disk_cleanup_notices import _notices, _report_notices, retry_notices
from .disk_stage import StageRequest


def _repos(cfg, errors=None):
    answer = []
    for path in sorted(config.repos_dir(cfg).glob('*.toml')):
        try:
            raw = repos.read_machine(path)
            answer.append(repos.machine_config(repos.load(
                {**raw, 'name': raw.get('name') or path.stem}, machine_path=path)))
        except (OSError, ValueError) as error:
            if errors is not None:
                errors.append(dict(path=str(path), reason_code='excluded_repo', error=str(error),
                                   owner_action=f'commit .drover/config.toml, then drover config migrate-machine {path.stem}'))
    return answer


def scope_key(cfg):
    """A report authorizes this machine and these exact configured scopes only."""
    machines = _repos(cfg)
    policies = {}
    for machine in machines:
        values = set()
        try:
            values.add(tuple(disk_evidence.outputs_at(machine, machine['repo'])))
        except (OSError, ValueError):
            values.add(('<unavailable>',))
        policies[machine['name']] = sorted(values)
    facts = dict(machine=socket.gethostname(), state=str(config.state_dir(cfg)),
                 trackers=cfg['trackers'], paths=cfg['paths'], cleanup=cfg['disk_cleanup'],
                 repos=machines, output_policies=policies, machine_files={str(p): p.read_text(encoding='utf-8') for p in sorted(config.repos_dir(cfg).glob('*.toml'))})
    return hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()


def _valid_result(value):
    if not isinstance(value, dict) or not isinstance(value.get('items'), list) or \
            not isinstance(value.get('errors'), list) or type(value.get('reclaimed_bytes')) is not int or \
            value['reclaimed_bytes'] < 0 or any(not isinstance(i, dict) for i in value['items'] + value['errors']):
        raise ValueError('invalid cleanup worker response')
    return value


def _save_report(state_dir, result):
    from .disk_inventory import bound_report
    bound_report(result)
    disk_state.save_file(state_dir, 'last-report.json', result)


class _Progress:
    """One JSON line per stage start and finish in disk/progress.log, and the current stage in state.json,
    so an operator can tell a long stage from a hung run. Best effort: it never fails the cleanup."""
    LIMIT = 1 << 20

    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self.started = time.time()
        self.current = None

    def _log(self, **line):
        try:
            path = self.state_dir / 'disk' / 'progress.log'
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > self.LIMIT:
                durable.move(path, path.with_name('progress.log.1'))
            with path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(dict(at=time.time(), pid=os.getpid(), **line)) + '\n')
        except OSError:
            pass

    def _mark(self, current):
        try:
            disk_state.update(self.state_dir, lambda state: state.update(progress=current))
        except (OSError, ValueError):
            pass  # contention or unreadable state never gates the run

    def begin(self, drive, stage, side):
        self.current = dict(drive=drive, stage=stage, side=side, started=time.time(), run_started=self.started)
        self._log(event='start', drive=drive, stage=stage, side=side)
        self._mark(self.current)

    def end(self, outcome, items=0, errors=0):
        current = self.current
        if current is not None:
            self._log(event='finish', drive=current['drive'], stage=current['stage'], side=current['side'],
                      elapsed_s=round(time.time() - current['started'], 3), outcome=outcome,
                      items=items, errors=errors)
            self.current = None

    def done(self):
        self._log(event='done', elapsed_s=round(time.time() - self.started, 3))
        self._mark(None)


def _reading(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def _windows_paths(cfg, base):
    """What Windows processes name inside WSL, which the WSL worker cannot see. (paths, unreadable); paths is None
    when the snapshot is unknown or failed."""
    try:
        proof = placement.cleanup_windows_proof(paths=base['policy'], state_dir=base['state_dir'], budget=True)
        return (proof.wsl_paths(disk.tracker_distro(cfg)), proof.unreadable) if proof else (None, [])
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return None, []


def _cut_off(result, stage, side, answer):
    """What a partial answer means for the run. A stage 2, 3 or 6 cut off in a dry run adds `stage_partial`, so the
    check before the stamp refuses it unchanged; an incomplete answer already carries its worker error row. Stages 4
    and 5 stop with an item and no error, so they stamp as today (ruling 7). Any partial stage 6 marks the run's
    budget usage partial, so the notices judge no budget on short rows; the mark stays in `result` for replay."""
    if not answer.get('partial'):
        return
    if stage in (2, 3, 6) and answer.get('complete') and not result['applied']:
        result['errors'].append(dict(stage=stage, side=side, reason_code='stage_partial'))
    if stage == 6:
        result['budget_usage_partial'] = True


def _serve_stage(runtime, progress, result, state_dir, side, request, *, trim=None):
    """Call one destructive stage and merge what it says into `result`. A failure is recorded, unless its termination
    is unconfirmed, which saves the report and raises. `trim` is the stage-1 request to send after a large reclaim.
    Returns the stage's answer, kept even when the trim after it failed; None when the stage itself failed."""
    answer, stage = None, request.stage
    try:
        answer = _valid_result(runtime.call(side, request))
        progress.end('ok', len(answer['items']), len(answer['errors']))
        result['peak_intermediate_bytes'] = max(result['peak_intermediate_bytes'], answer.get('peak_intermediate_bytes', 0))
        result['items'].extend(dict(item, stage=stage, side=side) for item in answer['items'])
        result['errors'].extend(dict(item, stage=stage, side=side) for item in answer['errors'])
        _cut_off(result, stage, side, answer)
        if trim is not None and answer['reclaimed_bytes'] >= 1024 ** 3:
            result['errors'].extend(_valid_result(runtime.call('windows', trim))['errors'])
    except (OSError, ValueError, RuntimeError) as error:
        if answer is None:
            progress.end('error', errors=1)
        result['errors'].append(dict(stage=stage, side=side, error=str(error)))
        if getattr(error, 'termination_confirmed', True) is False:
            try:
                _save_report(state_dir, result)
            except OSError:
                pass
            raise
    return answer


def _budget_purge(cfg, runtime, progress, result, base):
    """Serve a `wsl` claim: the WSL side's output stage alone, whatever the host drive's free space. Returns its
    receipt. WSL lives on the wsl_combined drive; without one no worktree can be proven to be inside it."""
    combined = {t['name'] for t in cfg['trackers'] if t.get('wsl_combined')}
    drive = next((d for d in result['drives'] if d['name'] in combined and d['path'] is not None), None)
    target = disk.wsl_budget(cfg)['target_gb']
    receipt = dict(applied=False, status='completed', budget='no_host', used_gb=None, target_gb=target,
                   reclaimed_bytes=0, shortfall_bytes=None, partial=False)
    if drive is None:
        return receipt
    side = disk_budget.SIDE
    windows_paths, unreadable = _windows_paths(cfg, base)
    if windows_paths is None:
        # Any WSL worktree could be the one a Windows editor or build has open, so this run purges none of them.
        result['items'].append(dict(disk_budget.processes_unknown(drive['path'], target), stage=5, side=side,
                                    count=len(unreadable), processes=unreadable[:20]))
        receipt['budget'] = 'processes_unknown'
        return receipt
    free = drive.get('post_free_gb', drive['free_gb'])
    request = StageRequest(**dict(base, apply=base['apply'] and free is not None), stage=5, side=None, now=runtime.now(),
                           drive=drive, budget=dict(target_gb=target, windows_paths=windows_paths))
    receipt.update(applied=request.apply, budget='failed')
    result['stages'].append(dict(drive=drive['name'], stage=5))
    progress.begin(drive['name'], 5, side)
    answer = _serve_stage(runtime, progress, result, config.state_dir(cfg), side, request,
                          trim=replace(request, stage=1, budget=None) if request.apply else None)
    if answer is not None:
        receipt['reclaimed_bytes'] = answer['reclaimed_bytes']
        if answer.get('budget'):
            receipt.update(budget=answer['budget']['status'], used_gb=answer['budget']['used_gb'],
                           shortfall_bytes=answer['budget']['shortfall_bytes'],
                           partial=answer['budget']['status'] == 'deadline' and answer['reclaimed_bytes'] > 0)
    return receipt


def _finish_wsl(state_dir, claim, receipt, at):
    """Its own receipt, holding no free space: the drive's baselines mean nothing to an episode judged on used GB.
    `partial` records a purge cut off by its deadline with work still done, which tick retries sooner; `reclaimed` lets
    a purge at the stop level come back. Waits count from `at`, when the purge ended, not from the claim."""
    disk_state.save_file(state_dir, 'result-' + claim['id'] + '.json', receipt)
    def finish(state):
        episode = state['episodes']['wsl']
        if (episode.get('claim') or {}).get('id') == claim['id']:
            episode.update(claim=None, partial=receipt['partial'], reclaimed=receipt['reclaimed_bytes'] > 0, last_cleanup=at)
    try:
        disk_state.update(state_dir, finish)
    except BlockingIOError:
        pass


def run(cfg: dict | None = None, *, mode='on-demand', apply=False, runtime=None) -> dict:
    cfg = config.get() if cfg is None else cfg
    if mode not in ('on-demand', 'triggered', 'daily'):
        raise ValueError('unknown cleanup mode')
    runtime = disk_runtime.cleanup_runtime(cfg) if runtime is None else runtime
    result = dict(mode=mode, applied=apply, drives=[], stages=[], items=[], errors=[], exhausted=False, peak_intermediate_bytes=0)
    state_dir = config.state_dir(cfg)
    tasks = _task_snapshot(state_dir)
    with disk_state.CleanupLock(state_dir):
        progress = _Progress(state_dir)
        key = scope_key(cfg)
        if apply and disk_state.read(state_dir).get('report_scope') != key:
            raise ValueError('complete a dry report for the same cleanup scope before apply')
        claims = {name: episode['claim'] for name, episode in disk_state.read(state_dir).get('episodes', {}).items() if episode.get('claim')}
        wsl_claim = claims.get('wsl') if mode == 'triggered' else None
        machines = _repos(cfg, result['errors'])
        rows = disk.read(cfg, reader=runtime.read)
        drive_stages_skipped = False
        inventories = set()
        inventory_id = uuid.uuid4().hex
        for row in rows:
            tracker = next(t for t in cfg['trackers'] if t['name'] == row['name'])
            free = row['free_gb']
            drive = dict(row, warn_gb=tracker['warn_gb'])
            result['drives'].append(drive)
            for stage in range(1, 5 if mode == 'daily' else 7):
                # Report all scopes, even when healthy. Unknown readings cannot authorize mutation.
                # A wsl claim is no reason to run a drive's stages, report-only or not: only a claimed drive runs them.
                if wsl_claim and row['name'] not in claims:
                    drive_stages_skipped = True
                    break
                if apply and mode != 'daily' and free is not None and free >= tracker['warn_gb']:
                    break
                result['stages'].append(dict(drive=row['name'], stage=stage))
                for side in ('windows', 'wsl'):
                    request = StageRequest(stage=stage, side=None, mode=mode, apply=apply and free is not None,
                                           now=runtime.now(), drive=drive, repos=machines, policy=cfg['disk_cleanup'],
                                           state_dir=str(state_dir), tasks=tasks, inventory_id=inventory_id)
                    progress.begin(row['name'], stage, side)
                    answer = _serve_stage(runtime, progress, result, state_dir, side, request,
                                          trim=replace(request, stage=1) if stage > 1 and apply and side == 'wsl' else None)
                    if answer is not None and stage == 6:
                        inventories.add((row['name'], side))
                free = _reading(runtime.read(row['path'])) if row['path'] is not None else None
                drive['post_free_gb'] = free
                drive['level'] = next((level for level in ('critical', 'hold', 'queue', 'warn')
                                       if free is not None and free < tracker[level + '_gb']), 'ok' if free is not None else 'unknown')
            result['exhausted'] |= bool(apply and mode != 'daily' and free is not None and
                                        free < tracker['warn_gb'] and stage == 6)
        if mode == 'triggered':
            for drive in result['drives']:
                claim = claims.get(drive['name'])
                if claim:
                    completed = dict(applied=apply, free=drive.get('post_free_gb', drive['free_gb']), status='completed')
                    disk_state.save_file(state_dir, 'result-' + claim['id'] + '.json', completed)
                    def finish(state):
                        episode = state['episodes'][drive['name']]
                        if (episode.get('claim') or {}).get('id') == claim['id']:
                            episode.update(claim=None)
                            episode['post' if completed['applied'] else 'report_post'] = completed['free']
                    try:
                        disk_state.update(state_dir, finish)
                    except BlockingIOError:
                        pass
            if wsl_claim:
                base = dict(mode=mode, apply=apply, repos=machines, policy=cfg['disk_cleanup'], state_dir=str(state_dir),
                            tasks=tasks, inventory_id=inventory_id)
                result['budget'] = _budget_purge(cfg, runtime, progress, result, base)
                _finish_wsl(state_dir, wsl_claim, result['budget'], runtime.now())
        if apply and not drive_stages_skipped:
            # Budget observation is report-only and independent of destructive stages.
            budgeted = [m for m in machines if m.get('budget_gb')]
            if budgeted:
                for row in rows:
                    for side in ('windows', 'wsl'):
                        if (row['name'], side) in inventories:
                            continue
                        request = StageRequest(stage=6, side=None, mode=None, apply=False, now=runtime.now(), drive=row,
                                               repos=budgeted, policy=cfg['disk_cleanup'], state_dir=str(state_dir),
                                               tasks=tasks, inventory_id=inventory_id)
                        try:
                            answer = _valid_result(runtime.call(side, request))
                            result['peak_intermediate_bytes'] = max(result['peak_intermediate_bytes'], answer.get('peak_intermediate_bytes', 0))
                            result['items'].extend(dict(i, stage=6, side=side) for i in answer['items'])
                            result['errors'].extend(dict(i, stage=6, side=side) for i in answer['errors'])
                            _cut_off(result, 6, side, answer)
                        except (OSError, ValueError, RuntimeError) as error:
                            result['budget_usage_partial'] = True       # no rows from this side: unknown, not zero (O7)
                            result['errors'].append(dict(reason_code='budget_unavailable', side=side, error=str(error)))
                            if getattr(error, 'termination_confirmed', True) is False:
                                try:
                                    _save_report(state_dir, result)
                                except OSError:
                                    pass
                                raise
        progress.done()
        for cache in (state_dir / 'disk').glob('inventory-' + inventory_id + '-*.json'):
            cache.unlink(missing_ok=True)
        for cache in (state_dir / 'disk').glob('processes-' + inventory_id + '*.json'):
            cache.unlink(missing_ok=True)
        from .disk_inventory import bound_report
        bound_report(result)
        # Persist report evidence independently of the short outbox transaction.
        pending = 'pending-notices-' + uuid.uuid4().hex + '.json'
        try:
            disk_state.save_file(state_dir, pending, dict(result=result, machines=machines))
            _report_notices(state_dir, result)
            if apply:
                _notices(cfg, result, runtime, machines)
        except BlockingIOError:
            pass
        except OSError as error:
            result['errors'].append(dict(reason_code='notice_persistence_failed', error=str(error)))
        else:
            (state_dir / 'disk' / pending).unlink(missing_ok=True)
        try:
            _save_report(state_dir, result)
        except OSError as error:
            result['errors'].append(dict(reason_code='report_persistence_failed', error=str(error)))
        # Every stage must have run; one that ended at its deadline leaves unreached worktrees alone, and each removal is re-proven.
        if not apply and not drive_stages_skipped and all(e.get('reason_code') == 'excluded_repo' for e in result['errors']):
            disk_state.update(state_dir, lambda state: state.update(report_scope=key))
    return result


def remove_approved(cfg: dict, repo: str, path: str, *, runtime=None) -> dict:
    """Explicit owner authorization replaces settlement, never the safety checks."""
    runtime = disk_runtime.cleanup_runtime(cfg) if runtime is None else runtime
    out = dict(ok=False, items=[], errors=[])
    try:
        with disk_state.CleanupLock(config.state_dir(cfg)):
            if disk_state.read(config.state_dir(cfg)).get('report_scope') != scope_key(cfg):
                raise ValueError('complete a dry report for the same cleanup scope before apply')
            machines = [m for m in _repos(cfg, out['errors']) if placement.cleanup_parts(m['repo']) == placement.cleanup_parts(repo)]
            if not machines:
                raise ValueError('repository is outside configured cleanup scope')
            for drive in disk.read(cfg, reader=runtime.read):
                if drive['free_gb'] is None:
                    continue
                request = StageRequest(stage=4, side=None, mode=None, apply=True, now=runtime.now(), drive=drive,
                                       repos=machines, policy=cfg['disk_cleanup'],
                                       state_dir=str(config.state_dir(cfg)),
                                       tasks=_task_snapshot(config.state_dir(cfg)), approved_path=path)
                for side in ('windows', 'wsl'):
                    answer = _valid_result(runtime.call(side, request))
                    out['items'].extend(answer['items'])
                    out['errors'].extend(answer['errors'])
            out['ok'] = any(placement.cleanup_parts(i['path']) == placement.cleanup_parts(path) and i['action'] == 'removed' for i in out['items'])
    except (OSError, ValueError, RuntimeError) as error:
        out['errors'].append(dict(path=path, error=str(error)))
    return out


def _task_snapshot(state_dir):
    try:
        value = json.loads((Path(state_dir) / 'disk' / 'tasks.json').read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def main(argv=None):
    import argparse
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ['lock']:
        command = argv[1:]
        if command[:1] == ['--']:
            command = command[1:]
        return locked_command(config.get(), command)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('daily', 'on-demand', 'triggered'), default='on-demand')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--apply', action='store_true')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
        answer = run(mode=args.mode, apply=args.apply)
        if args.json:
            print(json.dumps(answer, indent=2))
        else:
            print(f"Disk cleanup {answer['mode']}: {'applied' if answer['applied'] else 'report only'}")
            for row in answer['items']:
                print(f"{row.get('side', '')} stage {row.get('stage', '')}: {row.get('action', '')} "
                      f"{row.get('path', '')} ({row.get('reason_code', '')})")
            for error in answer['errors']:
                print(json.dumps(error))
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1


def locked_command(cfg: dict, argv: list[str], *, runner=None) -> int:
    """Hold the shared machine lock for the entire unmodified external command."""
    import subprocess
    import sys
    if not argv or not all(isinstance(arg, str) and '\x00' not in arg for arg in argv):
        print('disk-cleanup lock requires literal command argv', file=sys.stderr)
        return 2
    try:
        with disk_state.CleanupLock(config.state_dir(cfg)):
            return (runner or subprocess.run)(list(argv), shell=False).returncode
    except BlockingIOError as error:
        print(f'disk cleanup is already locked: {error}', file=sys.stderr)
        return 75
