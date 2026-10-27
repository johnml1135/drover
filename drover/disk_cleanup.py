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
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import config, disk, disk_state, placement, repos


@dataclass(frozen=True)
class CleanupRuntime:
    now: Callable
    read: Callable
    call: Callable
    toast: Callable
    pressure: Callable | None = None   # () -> {'verdict': go|queue|hold, 'reasons': [...]}; hold aborts the run
    evidence: object | None = None
    state: object | None = None


def decide(request, observations, previous_state, now):
    """Pure selection and grace transitions; candidates are never deletion authority.

    Observations contain a report row and optional settlement or quarantine facts.
    Evidence adapters own path/process/Git probes. Only their apply method may
    mutate a checkout, after obtaining fresh evidence and durably journaling it.
    Checks are semantic retention facts; settlement supplies task/ancestry facts.
    Quarantine supplies a canonical key and activity identity. approved_match is
    canonical path equality computed by the adapter, without path probes here.
    Validation supplies fresh identity, nesting and ignored-content facts.
    Neither observations nor previous_state are changed by this function.
    """
    state = copy.deepcopy(previous_state)
    events, decisions = [], []
    for observed in observations:
        row = copy.deepcopy(observed['row'])
        checks = observed.get('checks', {})
        blocked = next((reason for failed, reason in (
            (checks.get('other_side'), 'other_side_or_lock_note'),
            (checks.get('other_drive'), 'other_drive'),
            (checks.get('main'), 'main_checkout'),
            (checks.get('busy'), checks.get('process_reason') or 'busy'),
            (checks.get('protected'), 'protected'),
            (checks.get('missing'), 'missing'),
            (checks.get('live'), 'busy'),
            (bool(row.get('dirty')), 'dirty')) if failed), None)
        if blocked:
            row.update(action='candidate' if blocked == 'missing' else 'keep', reason_code=blocked)
        settlement = observed.get('settlement') if not blocked else None
        if settlement is not None:
            tag = row.get('tag')
            live, terminal = settlement['live'], settlement['terminal']
            lease = bool(tag and tag['v'] == 2 and tag['repo'] == row['name']
                         and tag['lease_until'] > now)
            if live:
                row.update(action='keep', reason_code='busy')
            elif not lease:
                row.update(action='keep', reason_code='orphan')
            elif terminal or settlement['merged']:
                row.update(action='candidate', reason_code='settled')
        quarantine = observed.get('quarantine')
        if quarantine is not None:
            key = quarantine['key']
            if quarantine.get('live'):
                row.update(action='keep', reason_code='live_owner')
            eligible = row['reason_code'] == 'orphan'
            if eligible and quarantine.get('excluded'):
                row.update(reason_code='ignored_content', excluded=quarantine['excluded'])
                eligible = False
            if request['apply']:
                records = state.setdefault('quarantine', {})
                previous = records.get(key)
                fingerprint = quarantine.get('identity')
                if not eligible:
                    if records.pop(key, None) is not None:
                        events.append(dict(kind='quarantine_reset', path=row['path']))
                elif not previous or previous['identity'] != fingerprint:
                    records[key] = dict(identity=fingerprint, since=now)
                    events.append(dict(kind='quarantine_started', path=row['path']))
                elif now - previous['since'] >= 3 * 86400:
                    row.update(action='candidate', reason_code='quarantine_elapsed',
                               quarantine_identity=fingerprint)
        approved = request.get('approved_path')
        if approved:
            if not observed.get('approved_match', row['path'] == approved):
                continue
            if row['reason_code'] in ('unsettled', 'orphan'):
                row['action'] = 'candidate'
        validation = observed.get('validation', {})
        if validation.get('identity_matches') is False:
            row.update(action='keep', reason_code='revalidation_changed')
        elif validation.get('nested'):
            row.update(action='keep', reason_code='nested_worktree')
        elif validation.get('excluded'):
            row.update(action='keep', reason_code='ignored_content', excluded=validation['excluded'])
        decisions.append(row)
    return dict(decisions=decisions, state=state, events=events)


class CleanupState:
    """Durable state and journal adapter; state transforms run under the file lock."""
    def __init__(self, state_dir):
        self.state_dir = state_dir

    def update(self, transform):
        return disk_state.update(self.state_dir, transform)

    def append(self, record):
        disk_state.append_manifest(self.state_dir, record)


def decide_and_store(request, observations, store):
    def update(state):
        answer = decide(request, observations, state, request['now'])
        state.clear()
        state.update(answer['state'])
        return answer
    # Reports must not even write an unchanged state file.
    return store.update(update) if request['apply'] else decide(request, observations, {}, request['now'])


def evaluate(request, runtime):
    """Observe, decide transactionally, then ask the adapter to revalidate/apply."""
    observations = runtime.evidence.observe(request)
    answer = (decide_and_store(request, observations, runtime.state)
              if any('quarantine' in o for o in observations)
              else decide(request, observations, {}, request['now']))
    rows = []
    for row in answer['decisions']:
        if request['apply'] and row['action'] == 'candidate':
            from .disk_worktrees import registration_identity
            row = runtime.evidence.apply(request, row, registration_identity(row))
        rows.append(row)
    return dict(items=rows, errors=[dict(r) for r in rows if r.get('error')],
                reclaimed_bytes=sum(r['bytes'] for r in rows if r['action'] == 'removed'),
                events=answer['events'])


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
    from .disk_worktrees import outputs_at
    machines = _repos(cfg)
    policies = {}
    for machine in machines:
        values = set()
        try:
            values.add(tuple(outputs_at(machine, machine['repo'])))
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
                path.replace(path.with_name('progress.log.1'))
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


def run(cfg: dict | None = None, *, mode='on-demand', apply=False, runtime=None) -> dict:
    cfg = config.get() if cfg is None else cfg
    if mode not in ('on-demand', 'triggered', 'daily'):
        raise ValueError('unknown cleanup mode')
    runtime = placement.cleanup_runtime(cfg) if runtime is None else runtime
    result = dict(mode=mode, applied=apply, drives=[], stages=[], items=[], errors=[], exhausted=False, peak_intermediate_bytes=0)
    state_dir = config.state_dir(cfg)
    tasks = _task_snapshot(state_dir)
    with disk_state.CleanupLock(state_dir):
        progress = _Progress(state_dir)
        key = scope_key(cfg)
        if apply and disk_state.read(state_dir).get('report_scope') != key:
            raise ValueError('complete a dry report for the same cleanup scope before apply')
        claims = {name: episode['claim'] for name, episode in disk_state.read(state_dir).get('episodes', {}).items() if episode.get('claim')}
        machines = _repos(cfg, result['errors'])
        rows = disk.read(cfg, reader=runtime.read)
        inventories = set()
        aborted = None

        def held():
            if runtime.pressure is None:
                return None
            try:
                answer = runtime.pressure()
            except (OSError, ValueError, KeyError, subprocess.SubprocessError):
                return None   # an unreadable machine state never stops a cleanup
            return list(answer.get('reasons') or ['load or RAM at hold']) if answer.get('verdict') == 'hold' else None
        inventory_id = uuid.uuid4().hex
        for row in rows:
            tracker = next(t for t in cfg['trackers'] if t['name'] == row['name'])
            free = row['free_gb']
            drive = dict(row, warn_gb=tracker['warn_gb'])
            result['drives'].append(drive)
            for stage in range(1, 5 if mode == 'daily' else 7):
                # Report all scopes, even when healthy. Unknown readings cannot authorize mutation.
                if apply and mode != 'daily' and free is not None and free >= tracker['warn_gb']:
                    break
                result['stages'].append(dict(drive=row['name'], stage=stage))
                for side in ('windows', 'wsl'):
                    aborted = held()
                    if aborted:
                        break
                    request = dict(stage=stage, mode=mode, apply=apply and free is not None, now=runtime.now(),
                                   drive=drive, repos=machines, paths=cfg['disk_cleanup'],
                                   state_dir=str(state_dir), tasks=tasks, _inventory_id=inventory_id)
                    progress.begin(row['name'], stage, side)
                    try:
                        answer = _valid_result(runtime.call(side, request))
                        progress.end('ok', len(answer['items']), len(answer['errors']))
                        result['peak_intermediate_bytes'] = max(result['peak_intermediate_bytes'], answer.get('peak_intermediate_bytes', 0))
                        if stage == 6:
                            inventories.add((row['name'], side))
                        result['items'].extend(dict(item, stage=stage, side=side) for item in answer['items'])
                        result['errors'].extend(dict(item, stage=stage, side=side) for item in answer['errors'])
                        if stage > 1 and apply and side == 'wsl' and answer['reclaimed_bytes'] >= 1024 ** 3:
                            trimmed = _valid_result(runtime.call('windows', dict(request, stage=1)))
                            result['errors'].extend(trimmed['errors'])
                    except (OSError, ValueError, RuntimeError) as error:
                        progress.end('error', errors=1)
                        result['errors'].append(dict(stage=stage, side=side, error=str(error)))
                        if getattr(error, 'termination_confirmed', True) is False:
                            try:
                                _save_report(state_dir, result)
                            except OSError:
                                pass
                            raise
                if aborted:
                    break
                free = _reading(runtime.read(row['path'])) if row['path'] is not None else None
                drive['post_free_gb'] = free
                drive['level'] = next((level for level in ('critical', 'hold', 'queue', 'warn')
                                       if free is not None and free < tracker[level + '_gb']), 'ok' if free is not None else 'unknown')
            if aborted:
                result.update(aborted='aborted_load', abort_reasons=aborted)
                progress._log(event='aborted_load', reasons=aborted)
                break
            result['exhausted'] |= bool(apply and mode != 'daily' and free is not None and
                                        free < tracker['warn_gb'] and stage == 6)
        if mode == 'triggered':
            for drive in result['drives']:
                claim = claims.get(drive['name'])
                if claim:
                    completed = dict(applied=apply and not aborted, free=drive.get('post_free_gb', drive['free_gb']),
                                     status='aborted_load' if aborted else 'completed')
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
        if apply and not aborted:
            # Budget observation is report-only and independent of destructive stages.
            budgeted = [m for m in machines if m.get('budget_gb')]
            if budgeted:
                for row in rows:
                    for side in ('windows', 'wsl'):
                        if (row['name'], side) in inventories:
                            continue
                        request = dict(stage=6, apply=False, now=runtime.now(), drive=row, repos=budgeted,
                                       paths=cfg['disk_cleanup'], state_dir=str(state_dir), tasks=tasks, _inventory_id=inventory_id)
                        try:
                            answer = _valid_result(runtime.call(side, request))
                            result['peak_intermediate_bytes'] = max(result['peak_intermediate_bytes'], answer.get('peak_intermediate_bytes', 0))
                            result['items'].extend(dict(i, stage=6, side=side) for i in answer['items'])
                            result['errors'].extend(dict(i, stage=6, side=side) for i in answer['errors'])
                        except (OSError, ValueError, RuntimeError) as error:
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
        if not apply and not aborted and all(e.get('reason_code') == 'excluded_repo' for e in result['errors']):
            disk_state.update(state_dir, lambda state: state.update(report_scope=key))
    return result


def remove_approved(cfg: dict, repo: str, path: str, *, runtime=None) -> dict:
    """Explicit owner authorization replaces settlement, never the safety checks."""
    runtime = placement.cleanup_runtime(cfg) if runtime is None else runtime
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
                request = dict(stage=4, apply=True, now=runtime.now(), drive=drive, repos=machines,
                               paths=cfg['disk_cleanup'], state_dir=str(config.state_dir(cfg)),
                               tasks=_task_snapshot(config.state_dir(cfg)), approved_path=path)
                for side in ('windows', 'wsl'):
                    answer = _valid_result(runtime.call(side, request))
                    out['items'].extend(answer['items'])
                    out['errors'].extend(answer['errors'])
            out['ok'] = any(placement.cleanup_parts(i['path']) == placement.cleanup_parts(path) and i['action'] == 'removed' for i in out['items'])
    except (OSError, ValueError, RuntimeError) as error:
        out['errors'].append(dict(path=path, error=str(error)))
    return out


def _report_notices(state_dir, result):
    if not any(i.get('reason_code') == 'merged_live_task' for i in result['items']):
        return
    def prepare(state):
        outbox = state.setdefault('outbox', {})
        for item in result['items']:
            if item.get('reason_code') != 'merged_live_task':
                continue
            ident = 'merged_live_task:' + hashlib.sha256(json.dumps(
                [item.get('repo'), item.get('task_id'), item['path']]).encode()).hexdigest()
            notice = dict(id=ident, kind='merged_live_task', title='Merged work has an open task',
                          body=f"{item.get('task_id')}: {item['path']} retained for its open task.", items=[item])
            if ident not in outbox:
                outbox[ident] = dict(notice=notice, toasted=False, acknowledged=False)
    disk_state.update(state_dir, prepare)


def _notices(cfg, result, runtime, machines):
    state_dir = config.state_dir(cfg)
    def prepare(state):
        episodes = state.setdefault('notice_episodes', {})
        pending = []
        for drive in result['drives']:
            free = drive.get('post_free_gb', drive['free_gb'])
            name = drive['name']
            previous = episodes.get(name)
            if free is not None and free >= drive['warn_gb']:
                episodes.pop(name, None)
            elif result['mode'] != 'daily' and free is not None and free < drive['warn_gb'] and \
                    any(s['drive'] == name and s['stage'] == 6 for s in result['stages']):
                if previous is None or free <= previous['free'] - 1:
                    previous = dict(id='exhaustion:' + uuid.uuid4().hex, free=free)
                    episodes[name] = previous
                    pending.append(dict(id=previous['id'], kind='exhaustion', drive=name,
                                        title=f"Disk cleanup exhausted: {name}",
                                        body=f"{free:.1f} GiB free after all cleanup stages; review retained items."))
        actions = list(dict.fromkeys(p['owner_action'] for row in result['items']
                                     if row.get('reason_code') == 'process_visibility_unavailable'
                                     for p in row.get('processes', []) if p.get('owner_action')))
        if actions:
            for notice in pending:
                drive = next(d for d in result['drives'] if d['name'] == notice['drive'])
                if drive['level'] == 'critical':
                    notice['body'] += '\n' + '\n'.join(actions)
        budgets = state.setdefault('budget_episodes', {})
        for machine in machines:
            if not machine.get('budget_gb'):
                continue
            # Stage workers return distinct owning-side directories, never symlink targets.
            usage = {i['identity']: i['bytes'] for i in result['items']
                     if i.get('reason_code') == 'budget_usage' and i.get('repo') == machine['name']}
            if not usage:
                continue
            name = machine['name']
            if sum(usage.values()) > machine['budget_gb'] * 1024 ** 3:
                if name not in budgets:
                    budgets[name] = 'budget:' + uuid.uuid4().hex
                    pending.append(dict(id=budgets[name], kind='budget', repo=name,
                                        title=f"Repository disk budget exceeded: {name}",
                                        body=f"{sum(usage.values()) / 1024 ** 3:.3f} GiB in repository directories."))
            else:
                budgets.pop(name, None)
        outbox = state.setdefault('outbox', {})
        for notice in pending:
            outbox[notice['id']] = dict(notice=notice, toasted=False, acknowledged=False)
        return pending
    pending = disk_state.update(state_dir, prepare)
    for notice in pending:
        try:
            runtime.toast(notice)
        except (OSError, RuntimeError):
            continue
        try:
            disk_state.update(state_dir, lambda state: state['outbox'][notice['id']].update(toasted=True))
        except BlockingIOError:
            pass  # Transport status never gates durable Inbox delivery.


def _task_snapshot(state_dir):
    try:
        value = json.loads((Path(state_dir) / 'disk' / 'tasks.json').read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def retry_notices(cfg):
    """Drain completion facts deferred by state contention, without rescanning repos."""
    for path in (config.state_dir(cfg) / 'disk').glob('pending-notices-*.json'):
        try:
            pending = json.loads(path.read_text(encoding='utf-8'))
            _report_notices(config.state_dir(cfg), pending['result'])
            if pending['result']['applied']:
                _notices(cfg, pending['result'], placement.cleanup_runtime(cfg), pending['machines'])
        except BlockingIOError:
            return
        path.unlink(missing_ok=True)


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
