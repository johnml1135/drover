"""Cheap physical-drive observations and admission policy, independent of the hub."""
import math
import shutil

from . import config, placement


def _free(path):
    try:
        return shutil.disk_usage(path).free / (1024 ** 3)
    except OSError:
        return None


def read(cfg: dict | None = None, *, reader=None) -> list[dict]:
    """Read enabled physical disks once; unavailable or invalid facts stay unknown."""
    cfg = config.get() if cfg is None else cfg
    reader = _free if reader is None else reader
    rows = []
    for tracker in cfg['trackers']:
        if tracker['kind'] != 'disk' or not tracker.get('enabled', True):
            continue
        path = placement.physical_disk_path(tracker['path']) if tracker['path'] else None
        try:
            free = reader(path) if path is not None else None
        except OSError:
            free = None
        if type(free) not in (int, float) or not math.isfinite(free) or free < 0:
            free, level = None, 'unknown'
        else:
            level = next((name for name in ('critical', 'hold', 'queue', 'warn')
                          if free < tracker[name + '_gb']), 'ok')
        rows.append(dict(name=tracker['name'], path=path, free_gb=free, level=level))
    return rows


def admission(readings: list[dict]) -> dict:
    """Most severe known disk wins; critical additionally requests a sticky pause."""
    verdict, pause, reasons = 'go', False, []
    for row in readings:
        level = row['level']
        if level in ('hold', 'critical'):
            verdict = 'hold'
        elif level == 'queue' and verdict == 'go':
            verdict = 'queue'
        pause |= level == 'critical'
        if level in ('queue', 'hold', 'critical'):
            reasons.append(f"{row['name']}: disk {level}" +
                           (f" ({row['free_gb']:.1f} GB free)" if row.get('free_gb') is not None else ''))
    return dict(verdict=verdict, pause=pause, reasons=reasons)
