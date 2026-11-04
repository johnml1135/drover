"""Cheap physical-drive observations and admission policy, independent of the hub."""
import json
import math
import shutil
import uuid

from . import compaction_hold, config, disk_runtime, placement


def _free(path):
    try:
        return shutil.disk_usage(path).free / (1024 ** 3)
    except OSError:
        return None


def tracker_distro(cfg):
    """The distro of the wsl tracker (the same one gather measures), else the default."""
    t = next((t for t in cfg['trackers'] if t['kind'] == 'wsl' and t.get('enabled', True)), None)
    return placement.distro(t['distro'] or None if t else None)


def wsl_budget(cfg, *, used=None) -> dict:
    """Judge GB used inside WSL against [disk_cleanup] wsl_purge_gb / wsl_stop_gb. level is 'ok', 'purge' (at or
    above wsl_purge_gb), 'stop' (at or above wsl_stop_gb) or 'unknown' (no usable reading, which triggers nothing).
    target_gb is where a purge aims."""
    from .config_defaults import DEFAULTS
    policy = {**DEFAULTS['disk_cleanup'], **cfg.get('disk_cleanup', {})}
    if type(used) not in (int, float) or not math.isfinite(used) or used < 0:
        level = 'unknown'
    elif used >= policy['wsl_stop_gb']:
        level = 'stop'
    elif used >= policy['wsl_purge_gb']:
        level = 'purge'
    else:
        level = 'ok'
    return dict(used_gb=used if level != 'unknown' else None, level=level, target_gb=policy['wsl_target_gb'])


def _wsl_used(cfg):
    """GB used inside WSL, or None. Only a wsl_combined drive means WSL lives on this machine's tracked drive; the
    distro must already be running (`wsl -l --running` starts nothing), so a reading never boots it."""
    if not any(t['kind'] == 'disk' and t.get('enabled', True) and t.get('wsl_combined') for t in cfg['trackers']):
        return None
    distro = tracker_distro(cfg)
    try:
        up = placement.wsl_running(distro)
    except OSError:
        up = False
    return placement.wsl_used_gb(distro, up)


def wsl_stop_row(cfg, used):
    """A hold row for new WSL work once GB used inside WSL reaches wsl_stop_gb, else None (an unknown reading holds
    nothing). Running work is never touched: only admission reads this."""
    budget = wsl_budget(cfg, used=used)
    if budget['level'] != 'stop':
        return None
    from .config_defaults import DEFAULTS
    limit = {**DEFAULTS['disk_cleanup'], **cfg.get('disk_cleanup', {})}['wsl_stop_gb']
    return dict(name='wsl', level='hold', free_gb=None,
                detail=f"WSL: {budget['used_gb']:.0f} GB used, at or above the {limit:g} GB limit")


def compaction_row(cfg):
    """A hold row for new WSL work while the overnight compaction holds WSL, else None."""
    if not compaction_hold.active(config.state_dir(cfg) if cfg.get('paths') else None):
        return None
    return dict(name='wsl', level='hold', free_gb=None, detail='WSL: held for the overnight compaction')


def wsl_hold_rows(cfg, used):
    """The rows that hold WSL work whatever the drives say."""
    return [row for row in (wsl_stop_row(cfg, used), compaction_row(cfg)) if row]


def held(side, cfg=None):
    """Disk hold for work placed on this side, from the shared WSL measurement; it never spawns a process.
    WSL work is also held while WSL used space is at wsl_stop_gb or a compaction holds WSL; Windows work is not."""
    cfg = config.get() if cfg is None else cfg
    wsl_free = placement.wsl_free_known(tracker_distro(cfg)) if side == 'wsl' else None
    rows = read(cfg, side=side, wsl_free=wsl_free)
    if side == 'wsl':
        rows.extend(wsl_hold_rows(cfg, placement.wsl_used_known(tracker_distro(cfg))))
    return admission(rows)['verdict'] == 'hold'


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
PARTIAL_RETRY_S = 30 * 60      # minimum gap after a budget purge that freed bytes, while WSL stays over budget
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


BUDGET_OUTCOMES = {
    'no_host': 'no disk tracker has wsl_combined, so no WSL worktree can be proven to be on a tracked drive',
    'processes_unknown': 'a process could not be read, so nothing proves which WSL worktrees are free',
    'shortfall': 'every disposable path that could go has gone and WSL is still above the target',
    'deadline': 'the purge ran out of time before WSL reached the target',
    'unknown': 'space used inside WSL could not be read',
    'failed': 'the cleanup worker failed',
    'died': 'the cleanup process died before it reported',
}


def _retry_wait(row, episode, cooldown):
    """Seconds this episode waits after its last cleanup ended. At stop the first claim of an episode does not wait and
    a purge that freed bytes comes back at PARTIAL_RETRY_S; every other ending, a dead claim included, has freed nothing
    and waits out the cooldown, never less than PARTIAL_RETRY_S. A run that could not help must not relaunch each tick."""
    if row.get('wsl') and row['level'] == 'stop':
        if not episode.get('attempted'):
            return 0
        return PARTIAL_RETRY_S if episode.get('reclaimed') else max(cooldown, PARTIAL_RETRY_S)
    return min(cooldown, PARTIAL_RETRY_S) if episode.get('partial') else cooldown


def budget_notice(state, outcome, *, applied=True, processes=()):
    """One Inbox notice per outcome in a WSL budget episode; `met` ends the episode, as does a healthy reading in tick.
    Each notice has a new id: the ledger never publishes an id twice, so a fixed one would fire once a lifetime."""
    episodes = state.setdefault('notice_episodes', {})
    if outcome == 'met':
        episodes.pop('wsl', None)
        return
    episode = episodes.setdefault('wsl', {})
    if outcome in episode:
        return
    ident = episode[outcome] = 'wsl_budget:' + uuid.uuid4().hex
    named = f" Unreadable: {', '.join(processes)}. Close them or add them to [disk_cleanup] process_ignore." if processes else ''
    state.setdefault('outbox', {})[ident] = dict(toasted=False, acknowledged=False, notice=dict(
        id=ident, kind='wsl_budget', title=f'WSL disk budget not met: {outcome}',
        body=f"{'Budget purge' if applied else 'Report-only budget purge'} ended {outcome}: "
             f"{BUDGET_OUTCOMES.get(outcome, 'no reason recorded')}.{named}"))


def _build_roots_tick(cfg, directory, now, compacting, launch):
    """Launch a build-roots run when a Windows build root is declared and `build_root_interval_s` has passed since the
    last one started, whatever any drive's level and outside the cleanup cooldown. Its start is kept in state, so a
    restarted hub waits out the interval; a launch that fails puts the previous start back. The run itself takes the
    cleanup lock, and skips when it is held, so no claim is kept. None during a WSL compaction, whose window holds
    that lock. True when it launched: the run is about to take the lock, so this tick claims no triggered cleanup,
    which would meet that lock and fail."""
    from . import disk_state
    from .config_defaults import BUILD_ROOT_INTERVAL_S
    policy = cfg.get('disk_cleanup', {})
    if compacting or not any(entry.get('placement') == 'windows' for entry in policy.get('build_roots', [])):
        return False
    interval = policy.get('build_root_interval_s', BUILD_ROOT_INTERVAL_S)
    def due(state):
        previous = (state.get('build_roots') or {}).get('last_start')
        if previous is not None and now - previous < interval:
            return False, previous
        state['build_roots'] = dict(state.get('build_roots') or {}, last_start=now)
        return True, previous
    try:
        start, previous = disk_state.update(directory, due)
    except BlockingIOError:
        return False
    if not start:
        return False
    try:
        launch() if launch is not None else disk_runtime.cleanup_enqueue(
            cfg, dict(id='build-roots', applied=True), mode='build-roots')
    except BaseException:
        def restore(state):
            if (state.get('build_roots') or {}).get('last_start') == now:
                if previous is None:
                    state['build_roots'].pop('last_start', None)
                else:
                    state['build_roots']['last_start'] = previous
        try:
            disk_state.update(directory, restore)
        except BlockingIOError:
            pass
        raise
    return True


def tick(cfg: dict | None = None, *, now=None, reader=None, enqueue=None, gate=None, wsl_used=None,
         build_enqueue=None) -> dict:
    """Observe even while paused; short claims precede all background work.

    Machine load never defers a cleanup, which runs on a CPU cap at low priority; `gate` is accepted and ignored.
    Only the lock and the cooldown defer, and state['cleanup_deferred'] records when and why.
    Free space on a drive and GB used inside WSL trigger independently: WSL at purge or stop claims its own
    episode named 'wsl'. `wsl_used` is a callable returning that reading (default: read it without starting WSL).
    A build-roots run has its own cadence (_build_roots_tick); `build_enqueue` launches it (default: detached)."""
    import json
    import time
    import uuid
    from . import disk_state
    cfg = config.get() if cfg is None else cfg
    now = time.time() if now is None else now
    rows = read(cfg, reader=reader)
    budget = wsl_budget(cfg, used=(wsl_used or (lambda: _wsl_used(cfg)))())
    # The WSL episode is judged on used space, not free: its row is never a drive reading, never pauses anything.
    episode_rows = rows + [dict(name='wsl', free_gb=budget['used_gb'], level=budget['level'], wsl=True)]
    directory = config.state_dir(cfg)
    compacting = compaction_hold.active(directory)
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
    building = _build_roots_tick(cfg, directory, now, compacting, build_enqueue)
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
            except OSError:
                if unresolved and _claim_age(directory, claim, now) > UNKNOWN_CLAIM_MAX_S:
                    dead.add(claim['id'])   # a claim whose process cannot be read for a day is released
                    continue
                return result  # Process state unknown (slow query): skip this tick's disk step.
    identity = None
    # Healthy ticks must never inventory processes.
    if any(row['level'] not in ('ok', 'unknown') for row in episode_rows):
        try:
            identity = placement.cleanup_identity()
        except OSError:
            return result
    # A held cleanup lock would make a triggered run fail at once; do not spawn one per tick. The compaction holds that
    # lock for its whole window, which is no stale lock, and the hold already defers every cleanup.
    blocker = disk_state.lock_blocker(directory) if identity and not compacting else None
    def change(state):
        episodes = state.setdefault('episodes', {})
        applied = bool(state.get('automation', {}).get('apply', False))
        launches = []
        cooldown = cfg.get('disk_cleanup', {}).get('cooldown_s', DEFAULT_COOLDOWN_S)
        state['readings'] = rows
        deferrals = []
        _lock_wait(state, blocker, directory, now)
        for row in episode_rows:
            if row.get('wsl') and row['name'] not in episodes and row['level'] not in ('purge', 'stop'):
                continue   # a healthy distro leaves no episode behind
            episode = episodes.setdefault(row['name'], {})
            claim = episode.get('claim')
            if claim and claim['id'] in receipts:
                completed = receipts[claim['id']]
                episode['last_cleanup'] = now
                episode.update(claim=None)
                if row.get('wsl'):       # the receipt's free space is the drive's; WSL re-arms on its cooldown alone
                    episode.update(partial=bool(completed.get('partial')), reclaimed=bool(completed.get('reclaimed_bytes')))
                else:
                    episode['post' if completed['applied'] else 'report_post'] = completed['free']
                claim = None
            elif claim and claim['id'] in dead:
                episode['last_cleanup'] = now
                episode['claim'] = claim = None
                episode.pop('partial', None)
                episode.pop('reclaimed', None)
                if row.get('wsl'):
                    budget_notice(state, 'died', applied=applied)
            free = row['free_gb']
            if free is None:
                continue
            if row['level'] == 'ok':
                episode.pop('post', None)
                episode.pop('report_post', None)
                for key in ('partial', 'reclaimed', 'attempted'):
                    episode.pop(key, None)
                state.setdefault('notice_episodes', {}).pop(row['name'], None)
                continue
            baseline = episode.get('post' if applied else 'report_post')
            if blocker is not None or building:     # a build-roots run launched this tick is taking the lock
                continue
            if claim or (baseline is not None and free > baseline - RETRIGGER_DROP_GB):
                continue
            if compacting:     # every cleanup has a WSL stage, so one that falls due in the window waits for its end
                deferrals.append(('compaction', ['%s: waiting for the WSL compaction to end' % row['name']]))
                continue
            last = episode.get('last_cleanup', -math.inf)
            wait = _retry_wait(row, episode, cooldown)
            if row['level'] != 'critical' and now - last < wait:
                deferrals.append(('cooldown', ['%s: last cleanup ended %d min ago; waiting %d min' % (
                    row['name'], (now - last) // 60, wait // 60)]))
                continue
            claim = dict(id=uuid.uuid4().hex, identity=identity, applied=applied, at=now,
                         prior_last=episode.get('last_cleanup'))
            episode['last_cleanup'] = now
            episode['claim'] = claim
            if row.get('wsl'):
                episode['attempted'] = True
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
        answer = enqueue() if enqueue is not None else disk_runtime.cleanup_enqueue(cfg, claim)
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
                    return disk_runtime.cleanup_enqueue(cfg, {'applied': args.apply, 'id': 'scheduled'})
            answer = tick(cfg, enqueue=enqueue)
        print(json.dumps(answer, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(str(error), file=__import__('sys').stderr)
        return 1
