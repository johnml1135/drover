"""Budget purge: disposable paths go, least recently used first, until WSL used space reaches the target.

This module only orders the candidates and decides when to stop. What is disposable, what is idle and what may be
removed stay with disk_paths, whose revalidated removal is the only one. Used space inside WSL is the WSL side's own."""
import math
import os
import time

from . import placement

SIDE = 'wsl'
GIB = 1024 ** 3
DEADLINE_SHARE = 0.8     # of worker_timeout_s: a purge ends itself in time to report what it did
_clock = time.monotonic


def deadline(request: dict) -> float:
    """The `_clock` time by which a stage ends itself, DEADLINE_SHARE of the worker's time limit from now."""
    return _clock() + request['paths'].get('worker_timeout_s', 1800) * DEADLINE_SHARE


def need_bytes(used_gb, target_gb) -> int:
    """Bytes to clear to bring used space down to the target; 0 when it is already there."""
    return max(0, math.ceil((used_gb - target_gb) * GIB))


def plan(entries: list[dict], need_bytes: int) -> list[dict]:
    """The candidates to clear `need_bytes`, longest idle first and newest idle last, stopping once that is covered.
    A file's age says when it was built, not when it was last wanted, so its worktree's idle time (idle_s) leads."""
    order = sorted((e for e in entries if e.get('action', 'candidate') == 'candidate'),
                   key=lambda e: (-e.get('idle_s', e['age_s']), -e['age_s'], e['path']))
    chosen, total = [], 0
    for entry in order:
        if total >= need_bytes:
            break
        chosen.append(entry)
        total += entry['bytes']
    return chosen


def processes_unknown(path, target_gb) -> dict:
    """The report row for a purge refused because no Windows snapshot proves which worktrees are free."""
    return dict(path=str(path), action='report', reason_code='budget_processes_unknown', target_gb=target_gb)


def _used():
    """GB used now on this distro's own root, or None. ttl=0: a cached reading would hide what a removal just freed."""
    value = placement.wsl_used_gb(os.environ.get('WSL_DISTRO_NAME', ''), ttl=0)
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def purge(request: dict) -> dict:
    """The output stage under a budget, one declared folder at a time: the oldest left is revalidated and removed,
    then used space is read again, since shared blocks can free less than they measure. Every step uses up a
    candidate, so this ends; short of the target it reports the shortfall. Unreadable used space stops it, because
    nothing then proves a removal is needed. Past its deadline it stops between folders and returns what it did, so a
    worker timeout cannot lose the outcome. A report-only request removes nothing and projects the plan from sizes."""
    from .disk_paths import disposable_rows, result
    out = result()
    if placement.cleanup_side() != SIDE:
        return out
    target = request['budget']['target_gb']
    if request['budget'].get('windows_paths') is None:
        out['items'].append(processes_unknown(request['drive']['path'], target))
        out['budget'] = dict(status='processes_unknown', used_gb=None, target_gb=target, shortfall_bytes=None)
        return out
    limit = request['paths'].get('worker_timeout_s', 1800) * DEADLINE_SHARE
    until = deadline(request)
    remove, pending, late = {}, [], False
    for row, removal in disposable_rows(request, out, deadline=until):
        remove[id(row)] = removal
        pending.append(row)
        if _clock() >= until:
            late = True
            break
    late = late or any(i.get('reason_code') == 'output_deadline' for i in out['items'])    # gathering ran out of time
    used, short = _used(), None
    while used is not None:
        short = need_bytes(used, target)
        batch = plan(pending, short)
        if not batch:
            break
        if not request['apply']:
            chosen = {id(row) for row in batch}
            pending = [row for row in pending if id(row) not in chosen]
            freed = sum(row['bytes'] for row in batch)
            used, short = used - freed / GIB, max(0, short - freed)
            break
        if _clock() >= until:
            late = True
            break
        pending = [row for row in pending if row is not batch[0]]
        remove[id(batch[0])]()
        used = _used()
    if used is None:
        status, short, reason = 'unknown', None, 'budget_used_unknown'
    elif not short:
        status, reason = 'met', 'budget_not_needed'
    elif late:
        status, reason = 'deadline', 'budget_deadline'
    else:
        status, reason = 'shortfall', 'budget_shortfall'
    for row in pending:
        row.update(action='keep', reason_code=reason)
    if status != 'met':
        out['items'].append(dict(path=str(request['drive']['path']), action='report', target_gb=target, used_gb=used,
                                 reason_code=reason, shortfall_bytes=short, reclaimed_bytes=out['reclaimed_bytes'],
                                 deadline_s=limit, candidates_left=len(pending)))
    out['budget'] = dict(status=status, used_gb=used, target_gb=target, shortfall_bytes=short)
    return out
