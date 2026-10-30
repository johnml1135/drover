"""Cheap physical-drive observations and admission policy, independent of the hub."""
import json
import math
import shutil
import subprocess

from . import config, placement


def _free(path):
    try:
        return shutil.disk_usage(path).free / (1024 ** 3)
    except OSError:
        return None


def tracker_distro(cfg):
    """The distro of the wsl tracker (the same one gather measures), else the default."""
    t = next((t for t in cfg['trackers'] if t['kind'] == 'wsl' and t.get('enabled', True)), None)
    return placement.distro(t['distro'] or None if t else None)


def held(side, cfg=None):
    """Disk hold for work placed on this side, from the shared WSL measurement; it never spawns a process."""
    cfg = config.get() if cfg is None else cfg
    wsl_free = placement.wsl_free_known(tracker_distro(cfg)) if side == 'wsl' else None
    return admission(read(cfg, side=side, wsl_free=wsl_free))['verdict'] == 'hold'


def read(cfg: dict | None = None, *, reader=None, side=None, wsl_free=None) -> list[dict]:
    """Read enabled physical disks once; unavailable or invalid facts stay unknown.

    side='wsl' judges a wsl_combined tracker on host free + wsl_free (GB inside the distro; None counts as 0, i.e.
    host alone), because free space inside the VHDX is reused without growing it. That work is held whenever the
    host alone is below host_floor_gb or unreadable, measured WSL space or not. Every other side/tracker is judged
    on the host alone. A combined tracker's row carries floor_gb whatever the side."""
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
        row = dict(name=tracker['name'], path=path, free_gb=free, level=level)
        if tracker.get('wsl_combined'):
            row['floor_gb'] = tracker['host_floor_gb']
            if side == 'wsl':
                _combine(row, tracker, free, wsl_free)
        rows.append(row)
    return rows


def _combine(row, tracker, host, wsl_free):
    """Judge WSL-side work on host + WSL free; the host floor holds it whatever the distro has."""
    floor = tracker['host_floor_gb']
    if host is None:
        row['level'] = 'hold'   # an unreadable host is no escape from the floor
        return
    wsl = wsl_free if type(wsl_free) in (int, float) and math.isfinite(wsl_free) and wsl_free > 0 else 0.0
    total = host + wsl
    level = row['level'] if not wsl else next(
        (name for name in ('critical', 'hold', 'queue', 'warn') if total < tracker[name + '_gb']), 'ok')
    if host < floor or level == 'critical':
        level = 'hold'   # the sticky pause for WSL work is the floor, decided in tick
    row['level'] = level
    if wsl:
        row.update(free_gb=total, host_gb=host, wsl_gb=wsl,
                   detail=f"{tracker['name'].upper()}: {host:.0f} GB + WSL {wsl:.0f} GB = {total:.0f} GB for WSL work; "
                          f"floor {floor:g} GB")


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
                           (f" ({row['detail']})" if row.get('detail') else
                            f" ({row['free_gb']:.1f} GB free)" if row.get('free_gb') is not None else ''))
    return dict(verdict=verdict, pause=pause, reasons=reasons)


LOCK_BACKOFF_S = 30 * 60
RETRIGGER_DROP_GB = 10.0       # a finished cleanup re-triggers only after free space falls this far below its result
DEFAULT_COOLDOWN_S = 6 * 3600  # minimum gap after any cleanup run; disk_cleanup.cooldown_s overrides
UNKNOWN_CLAIM_MAX_S = 24 * 3600
ABSENT_GRACE_S = 120


def _lock_wait(state, blocker, directory, now):
    """Track how long one lock has blocked cleanup; past the back-off raise one owner-visible notice per lock."""
    token = None if blocker is None else blocker.get('token') or 'unreadable'
    wait = state.get('lock_wait')
    if wait and wait.get('token') != token:
        # The lock this notice named is gone or changed: withdraw it (an undelivered one simply disappears).
        outbox = state.setdefault('outbox', {})
        entry = outbox.get('lock:' + wait['token'])
        if entry is not None and not entry['acknowledged']:
            del outbox['lock:' + wait['token']]
        elif entry is not None:
            entry['retracted'] = True
        state.pop('lock_wait', None)
    if blocker is None:
        return
    wait = state.get('lock_wait')
    if not wait:
        wait = state['lock_wait'] = dict(token=token, since=now)
    if now - wait['since'] < LOCK_BACKOFF_S:
        return
    ident = 'lock:' + token
    outbox = state.setdefault('outbox', {})
    if ident not in outbox:
        notice = dict(id=ident, kind='cleanup_lock', title='Disk cleanup is blocked by a stale lock',
                      body=f"{directory / 'disk-cleanup.lock'} has been held for over {LOCK_BACKOFF_S // 60} minutes "
                           f"and its holder cannot be proven dead. Owner: {json.dumps(blocker, sort_keys=True)}. "
                           "If that process is gone, remove the lock directory.")
        outbox[ident] = dict(notice=notice, toasted=False, acknowledged=False)


def _claim_age(directory, claim, now):
    """Seconds since the claim was made. A legacy claim without `at` ages from its claim file, else counts as old."""
    if 'at' in claim:
        return now - claim['at']
    try:
        return now - (directory / 'disk' / ('claim-' + claim['id'] + '.json')).stat().st_mtime
    except OSError:
        return math.inf


DEFERRAL_NOTICE_S = 30 * 60


def _deferral_notice(state, now, rows):
    """One Inbox notice once deferrals last 30 min while a drive is at queue or worse; retracted when cleanup runs."""
    ident = 'cleanup_deferred'
    outbox = state.setdefault('outbox', {})
    deferral = state.get('cleanup_deferred')
    low = any(row['level'] in ('queue', 'hold', 'critical') for row in rows)
    if deferral and low and now - deferral['since'] >= DEFERRAL_NOTICE_S:
        if ident not in outbox:
            why = '; '.join(deferral['reasons']) or deferral['verdict']
            outbox[ident] = dict(toasted=False, acknowledged=False, notice=dict(
                id=ident, kind='cleanup_deferred', title='Disk cleanup is deferred while disk space is low',
                body=f"Cleanup has been deferred for over {DEFERRAL_NOTICE_S // 60} minutes ({deferral['verdict']}: {why})."))
    elif ident in outbox:
        entry = outbox[ident]
        if entry['acknowledged']:
            entry['retracted'] = True
        else:
            del outbox[ident]


def tick(cfg: dict | None = None, *, now=None, reader=None, enqueue=None, gate=None) -> dict:
    """Observe even while paused; short claims precede all background work.

    gate() returns a load/RAM verdict ({'verdict', 'reasons'}); a cleanup launches only on 'go', otherwise it is
    deferred and state['cleanup_deferred'] records when and why."""
    import json
    import time
    import uuid
    from . import disk_state
    cfg = config.get() if cfg is None else cfg
    now = time.time() if now is None else now
    rows = read(cfg, reader=reader)
    directory = config.state_dir(cfg)
    # The sticky pause stops the whole dispatcher, so it needs every seat side to be paused: Windows work at H: below
    # critical_gb, WSL work at H: below the floor of a wsl_combined drive (critical_gb for any other drive).
    sides = {s.get('placement') for s in cfg.get('seats', [])} & {'windows', 'wsl'}
    critical = [row for row in rows if row['level'] == 'critical']
    paused = {'windows': bool(critical),
              'wsl': any(row['free_gb'] < row['floor_gb'] if 'floor_gb' in row else row in critical for row in rows
                         if row['free_gb'] is not None)}
    pause = bool(critical) and all(paused[side] for side in sides)
    for row in critical if pause else ():
        name = uuid.uuid5(uuid.NAMESPACE_URL, row['name']).hex
        disk_state.save_file(directory, 'critical-' + name + '.json', dict(drive=row['name'], at=now))
    result = dict(admission(rows), drives=rows, pause=pause)
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
            unresolved = not identity.get('birth')
            try:
                if unresolved and identity.get('side') == placement.cleanup_side() and \
                        type(identity.get('pid')) is int and identity['pid'] > 0:
                    # Launched but its start time was unreadable: resolve it now, so liveness can be proven.
                    seen = placement.cleanup_identity(identity['pid'])
                    age = _claim_age(directory, claim, now)
                    if seen.get('birth') and seen['birth'] != 'absent':
                        identity = seen
                        disk_state.save_file(directory, 'claim-' + claim['id'] + '.json', seen)
                    elif (seen.get('birth') == 'absent' and age > ABSENT_GRACE_S) or age > UNKNOWN_CLAIM_MAX_S:
                        dead.add(claim['id'])
                        continue
                if placement.cleanup_claim_alive(identity) is False:
                    dead.add(claim['id'])
            except (subprocess.SubprocessError, OSError):
                if unresolved and _claim_age(directory, claim, now) > UNKNOWN_CLAIM_MAX_S:
                    dead.add(claim['id'])   # a claim whose process cannot be read for a day is released
                    continue
                return result  # Process state unknown (slow query): skip this tick's disk step.
    identity = None
    # Healthy ticks must never inventory processes.
    if any(row['level'] not in ('ok', 'unknown') for row in rows):
        try:
            identity = placement.cleanup_identity()
        except (subprocess.SubprocessError, OSError):
            return result
    # A held cleanup lock would make a triggered run fail at once; do not spawn one per tick.
    blocker = disk_state.lock_blocker(directory) if identity else None
    pressure = None
    if gate is not None and identity and blocker is None and \
            any(row['level'] not in ('ok', 'unknown') and row['free_gb'] is not None for row in rows):
        try:
            pressure = gate()
        except (OSError, ValueError, subprocess.SubprocessError):
            pressure = None   # an unreadable machine state defers nothing
    def blocked(row):
        # Critical space relaxes the gate: only hold stops it. Everything else needs go.
        if pressure is None:
            return False
        return pressure.get('verdict') == 'hold' if row['level'] == 'critical' else pressure.get('verdict') != 'go'
    def change(state):
        episodes = state.setdefault('episodes', {})
        applied = bool(state.get('automation', {}).get('apply', False))
        launches = []
        cooldown = cfg.get('disk_cleanup', {}).get('cooldown_s', DEFAULT_COOLDOWN_S)
        state['readings'] = rows
        deferrals = []
        _lock_wait(state, blocker, directory, now)
        for row in rows:
            episode = episodes.setdefault(row['name'], {})
            claim = episode.get('claim')
            if claim and claim['id'] in receipts:
                completed = receipts[claim['id']]
                episode['last_cleanup'] = now
                episode.update(claim=None)
                episode['post' if completed['applied'] else 'report_post'] = completed['free']
                claim = None
            elif claim and claim['id'] in dead:
                episode['last_cleanup'] = now
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
            if blocker is not None:
                continue
            if claim or (baseline is not None and free > baseline - RETRIGGER_DROP_GB):
                continue
            last = episode.get('last_cleanup', -math.inf)
            if row['level'] != 'critical' and now - last < cooldown:
                deferrals.append(('cooldown', ['%s: last cleanup ended %d min ago; waiting %d min' % (
                    row['name'], (now - last) // 60, cooldown // 60)]))
                continue
            if blocked(row):
                deferrals.append((pressure['verdict'], list(pressure.get('reasons', []))))
                continue
            claim = dict(id=uuid.uuid4().hex, identity=identity, applied=applied, at=now,
                         prior_last=episode.get('last_cleanup'))
            episode['last_cleanup'] = now
            episode['claim'] = claim
            launches.append((row['name'], claim))
        if deferrals:
            previous = state.get('cleanup_deferred') or {}
            state['cleanup_deferred'] = dict(at=now, since=previous.get('since', now), verdict=deferrals[0][0],
                                             reasons=[r for _, rs in deferrals for r in rs])
        else:
            state.pop('cleanup_deferred', None)
        _deferral_notice(state, now, rows)
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
                    if claim.get('prior_last') is None:
                        episode.pop('last_cleanup', None)
                    else:
                        episode['last_cleanup'] = claim['prior_last']
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


def main(argv=None):
    import argparse
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('read', 'tick', 'install'))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'install':
            from .disk_schedule import install
            answer = install(apply=args.apply, dry_run=args.dry_run)
        elif args.command == 'read':
            answer = read()
        else:
            cfg = config.get()
            enqueue = None
            if args.apply or args.dry_run:
                def enqueue():
                    if args.apply:
                        from .disk_cleanup import scope_key
                        from . import disk_state
                        if disk_state.read(config.state_dir(cfg)).get('report_scope') != scope_key(cfg):
                            raise ValueError('complete a dry report for the same cleanup scope before apply')
                    return placement.cleanup_enqueue(cfg, {'applied': args.apply, 'id': 'scheduled'})
            answer = tick(cfg, enqueue=enqueue)
        print(json.dumps(answer, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(str(error), file=__import__('sys').stderr)
        return 1
