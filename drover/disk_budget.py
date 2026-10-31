"""Budget purge: disposable paths go, least recently used first, until WSL used space reaches the target.

This module only orders the candidates and decides when to stop. What is disposable, what is idle and what may be
removed stay with disk_paths, whose revalidated removal is the only one. Used space inside WSL is the WSL side's own."""
import math
import os

from . import placement

SIDE = 'wsl'
GIB = 1024 ** 3


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


def _used():
    """GB used now on this distro's own root, or None. ttl=0: a cached reading would hide what a batch just freed."""
    value = placement.wsl_used_gb(os.environ.get('WSL_DISTRO_NAME', ''), ttl=0)
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def purge(request: dict) -> dict:
    """The output stage under a budget. Each batch is planned from what is left and every removal revalidates, so a
    worktree that came into use is kept. Used space is read again after each batch, since shared blocks can free less
    than they measure. Every batch uses up candidates, so this ends; short of the target it reports the shortfall.
    Unreadable used space stops it, because nothing then proves a removal is needed. A report-only request removes
    nothing and projects the same plan from the measured sizes."""
    from .disk_paths import disposable_rows, result
    out = result()
    if placement.cleanup_side() != SIDE:
        return out
    target = request['budget']['target_gb']
    found = list(disposable_rows(request, out))
    remove = {id(row): removal for row, removal in found}
    pending = [row for row, _ in found]
    used, short = _used(), None
    while used is not None:
        short = need_bytes(used, target)
        batch = plan(pending, short)
        if not batch:
            break
        chosen = {id(row) for row in batch}
        pending = [row for row in pending if id(row) not in chosen]
        if not request['apply']:
            freed = sum(row['bytes'] for row in batch)
            used, short = used - freed / GIB, max(0, short - freed)
            break
        for row in batch:
            remove[id(row)]()
        used = _used()
    status = 'unknown' if used is None else 'shortfall' if short else 'met'
    for row in pending:
        row.update(action='keep', reason_code='budget_used_unknown' if used is None else 'budget_not_needed')
    if used is None:
        short = None
    if status != 'met':
        out['items'].append(dict(path=str(request['drive']['path']), action='report', target_gb=target, used_gb=used,
                                 reason_code='budget_used_unknown' if used is None else 'budget_shortfall',
                                 shortfall_bytes=short, reclaimed_bytes=out['reclaimed_bytes']))
    out['budget'] = dict(status=status, used_gb=used, target_gb=target, shortfall_bytes=short)
    return out
