"""Machine-wide staged cleanup; side workers own candidate safety decisions."""
import hashlib
import json
import math
import socket
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


def _repos(cfg, errors=None):
    answer = []
    for path in sorted(config.repos_dir(cfg).glob('*.toml')):
        try:
            raw = repos.read_machine(path)
            answer.append(repos.load({**raw, 'name': raw.get('name') or path.stem}, machine_path=path))
        except (OSError, ValueError) as error:
            if errors is not None:
                errors.append(dict(path=str(path), reason_code='excluded_repo', error=str(error)))
    return answer


def scope_key(cfg):
    """A report authorizes this machine and these exact configured scopes only."""
    facts = dict(machine=socket.gethostname(), state=str(config.state_dir(cfg)),
                 trackers=cfg['trackers'], paths=cfg['paths'], cleanup=cfg['disk_cleanup'],
                 repos=_repos(cfg), machine_files={str(p): p.read_text() for p in sorted(config.repos_dir(cfg).glob('*.toml'))})
    return hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()


def _valid_result(value):
    if not isinstance(value, dict) or not isinstance(value.get('items'), list) or \
            not isinstance(value.get('errors'), list) or type(value.get('reclaimed_bytes')) is not int or \
            value['reclaimed_bytes'] < 0 or any(not isinstance(i, dict) for i in value['items'] + value['errors']):
        raise ValueError('invalid cleanup worker response')
    return value


def _reading(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def run(cfg: dict | None = None, *, mode='on-demand', apply=False, runtime=None) -> dict:
    cfg = config.get() if cfg is None else cfg
    if mode not in ('on-demand', 'triggered', 'daily'):
        raise ValueError('unknown cleanup mode')
    runtime = placement.cleanup_runtime(cfg) if runtime is None else runtime
    result = dict(mode=mode, applied=apply, drives=[], stages=[], items=[], errors=[], exhausted=False)
    state_dir = config.state_dir(cfg)
    with disk_state.CleanupLock(state_dir):
        key = scope_key(cfg)
        if apply and disk_state.read(state_dir).get('report_scope') != key:
            raise ValueError('complete a dry report for the same cleanup scope before apply')
        machines = _repos(cfg, result['errors'])
        rows = disk.read(cfg, reader=runtime.read)
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
                    request = dict(stage=stage, apply=apply and free is not None, now=runtime.now(),
                                   drive=drive, repos=machines, paths=cfg['disk_cleanup'],
                                   state_dir=str(state_dir), tasks={})
                    try:
                        answer = _valid_result(runtime.call(side, request))
                        result['items'].extend(dict(item, stage=stage, side=side) for item in answer['items'])
                        result['errors'].extend(dict(item, stage=stage, side=side) for item in answer['errors'])
                        if stage > 1 and apply and side == 'wsl' and answer['reclaimed_bytes'] >= 1024 ** 3:
                            trimmed = _valid_result(runtime.call(side, dict(request, stage=1)))
                            result['errors'].extend(trimmed['errors'])
                    except (OSError, ValueError, RuntimeError) as error:
                        result['errors'].append(dict(stage=stage, side=side, error=str(error)))
                free = _reading(runtime.read(row['path'])) if row['path'] is not None else None
                drive['post_free_gb'] = free
            result['exhausted'] |= bool(apply and mode != 'daily' and free is not None and
                                        free < tracker['warn_gb'] and stage == 6)
        if not apply and all(e.get('reason_code') == 'excluded_repo' for e in result['errors']):
            state = disk_state.read(state_dir)
            state['report_scope'] = key
            disk_state.write(state_dir, state)
    return result
