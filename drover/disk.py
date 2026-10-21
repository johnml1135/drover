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


def tick(cfg: dict | None = None, *, now=None, reader=None, enqueue=None) -> dict:
    """Observe even while paused; short claims precede all background work."""
    import json
    import time
    import uuid
    from . import disk_state
    cfg = config.get() if cfg is None else cfg
    now = time.time() if now is None else now
    rows = read(cfg, reader=reader)
    directory = config.state_dir(cfg)
    for row in rows:
        if row['level'] == 'critical':
            name = uuid.uuid5(uuid.NAMESPACE_URL, row['name']).hex
            disk_state.save_file(directory, 'critical-' + name + '.json', dict(drive=row['name'], at=now))
    result = dict(admission(rows), drives=rows)
    result['critical_tokens'] = [p.name for p in (directory / 'disk').glob('critical-*.json')]
    result['pause'] |= bool(result['critical_tokens'])
    from .disk_cleanup import retry_notices
    retry_notices(cfg)
    # Process checks and receipt IO happen outside the short state transaction.
    try:
        observed = disk_state.read(directory)
    except BlockingIOError:
        return result
    dead, receipts = set(), {}
    for episode in observed.get('episodes', {}).values():
        claim = episode.get('claim')
        if not claim:
            continue
        receipt = directory / 'disk' / ('result-' + claim['id'] + '.json')
        if receipt.exists():
            receipts[claim['id']] = json.loads(receipt.read_text(encoding='utf-8'))
        else:
            attached = directory / 'disk' / ('claim-' + claim['id'] + '.json')
            identity = json.loads(attached.read_text(encoding='utf-8')) if attached.exists() else claim['identity']
            if placement.cleanup_claim_alive(identity) is False:
                dead.add(claim['id'])
    identity = None
    # Healthy ticks must never inventory processes.
    if any(row['level'] not in ('ok', 'unknown') for row in rows):
        identity = placement.cleanup_identity()
    def change(state):
        episodes = state.setdefault('episodes', {})
        applied = bool(state.get('automation', {}).get('apply', False))
        launches = []
        state['readings'] = rows
        for row in rows:
            episode = episodes.setdefault(row['name'], {})
            claim = episode.get('claim')
            if claim and claim['id'] in receipts:
                completed = receipts[claim['id']]
                episode.update(claim=None)
                episode['post' if completed['applied'] else 'report_post'] = completed['free']
                claim = None
            elif claim and claim['id'] in dead:
                episode['claim'] = claim = None
            free = row['free_gb']
            if free is None:
                continue
            if row['level'] == 'ok':
                episode.pop('post', None)
                episode.pop('report_post', None)
                state.setdefault('notice_episodes', {}).pop(row['name'], None)
                continue
            baseline = episode.get('post' if applied else 'report_post')
            if claim or (baseline is not None and free > baseline - 1):
                continue
            claim = dict(id=uuid.uuid4().hex, identity=identity, applied=applied)
            episode['claim'] = claim
            launches.append((row['name'], claim))
        return launches
    try:
        launches = disk_state.update(directory, change)
    except BlockingIOError:
        return result
    if not launches:
        return result
    claim = launches[0][1]
    try:
        answer = enqueue() if enqueue is not None else placement.cleanup_enqueue(cfg, claim)
    except BaseException:
        def release(state):
            for name, claim in launches:
                episode = state['episodes'][name]
                if (episode.get('claim') or {}).get('id') == claim['id']:
                    episode['claim'] = None
        try:
            disk_state.update(directory, release)
        except BlockingIOError:
            for _, claim in launches:
                disk_state.save_file(directory, 'result-' + claim['id'] + '.json',
                                     dict(applied=claim['applied'], free=None))
        raise
    if isinstance(answer, dict) and answer.get('identity'):
        for _, claim in launches:
            disk_state.save_file(directory, 'claim-' + claim['id'] + '.json', answer['identity'])
        def attach(state):
            for name, claim in launches:
                current = state['episodes'][name].get('claim')
                if current and current['id'] == claim['id']:
                    current['identity'] = answer['identity']
        try:
            disk_state.update(directory, attach)
        except BlockingIOError:
            pass
    return result
