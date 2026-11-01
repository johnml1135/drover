"""Hub trackers that raise Inbox notices: the machine warden, landing velocity and context size.

Each check is a pure function returning [{'id', 'title', 'body'}]. tick() runs them on the [watch] interval_s clock,
publishes what is new through disk_notices (kind 'watch.notice') and retracts what has cleared. A check that fails
keeps its open notices and never stops the others or the dispatcher. Nothing here reads WSL: a reading that needs a
stopped WSL means no notice. Not ported from the herdr skill: pane, tmux and ORCHESTRATORS.md messaging, compaction
orders, process census by worktree, the landing-queue pressure score, base-moved and stale-hold flags, dedupe windows.
"""
import json
import logging
from datetime import datetime, timezone

from .ledger import disk_notices, ops

KIND, EPIC, ACTOR = 'watch.notice', 'Watch', 'watch'
STATE_KEY = 'watch_state'
CLEAR_INTERVALS = 3      # a notice closes after its condition has been absent this many intervals in a row
RECENT_BATCHES = 20      # the red rate looks at the gates of this many latest landing batches
PER_ITEM = ('context:', 'velocity:stalled:')
CHECKS = ('warden', 'velocity', 'context')      # the id prefix of each check's notices
log = logging.getLogger(__name__)


def warden(resources):
    """A hold verdict from the machine resource check. The wsl part never counts: it gates WSL work only."""
    if not resources or resources.get('verdict') != 'hold':
        return []
    reasons = [str(r) for r in resources.get('reasons') or []]
    return [{'id': 'warden:hold', 'title': 'Machine resources are on hold',
             'body': 'Nothing new starts until this clears.\n' + '\n'.join(f'- {r}' for r in reasons)}]


def _epoch(stamp):
    return datetime.strptime(stamp, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()


def velocity(conn, cfg, now):
    """Reviewed tasks that have waited stalled_hours to land, and a red gate rate over red_rate once min_gates ran."""
    cfg = cfg.get('watch', cfg)
    found = []
    rows = conn.execute(
        "SELECT t.id, t.title, COALESCE((SELECT MAX(e.at) FROM task_events e WHERE e.task_id=t.id AND e.kind='status' "
        "AND json_extract(e.data,'$.to')='reviewed'), t.updated) AS since FROM tasks t WHERE t.status='reviewed'").fetchall()
    for row in rows:
        hours = (now - _epoch(row['since'])) / 3600
        if hours >= cfg['stalled_hours']:
            found.append({'id': f"velocity:stalled:{row['id']}", 'title': f"Reviewed {int(hours)} h ago and not landed: {row['title']}",
                          'body': f"Task {row['id']} has been reviewed for {int(hours)} hours without landing. "
                                  'Land the ready subset now and stop adding to the batch.'})
    recent = conn.execute('SELECT COALESCE(SUM(gates), 0) AS gates, COALESCE(SUM(red), 0) AS red FROM '
                          '(SELECT gates, red FROM landing_batches ORDER BY id DESC LIMIT ?)', (RECENT_BATCHES,)).fetchone()
    v = {'gates': recent['gates'], 'red_gates': recent['red'], 'red_rate': recent['red'] / recent['gates'] if recent['gates'] else None}
    if v['gates'] >= cfg['min_gates'] and v['red_rate'] is not None and v['red_rate'] > cfg['red_rate']:
        found.append({'id': 'velocity:red-rate', 'title': 'Most landing gates are coming up red',
                      'body': f"{v['red_gates']} of the last {v['gates']} gates were red ({v['red_rate']:.0%}). "
                              'Require the full default suite green before READY, and split the batch.'})
    return found


def context(runs, cfg):
    """Live runs whose context passed context_tokens. runs: [{'run_id', 'task_id', 'context'}]; no reading, no notice."""
    cfg = cfg.get('watch', cfg)
    return [{'id': f"context:{r['run_id']}", 'title': f"Run {r['run_id']} context is over {cfg['context_tokens']} tokens",
             'body': f"Run {r['run_id']} (task {r.get('task_id')}) is at about {r['context']} tokens. "
                     'It should hand off and restart rather than keep growing.'}
            for r in runs or [] if isinstance(r.get('context'), (int, float)) and r['context'] > cfg['context_tokens']]


def _state(conn):
    try:
        state = json.loads(ops.get_setting(conn, STATE_KEY, '{}'))
    except ValueError:
        state = {}
    return {'at': state.get('at'), 'open': list(state.get('open') or []), 'episodes': dict(state.get('episodes') or {}),
            'absent': dict(state.get('absent') or {})}


def tick(conn, cfg, resources, now, runs=()):
    """Run the checks when interval_s has passed; returns the notice ids open afterwards."""
    w = cfg.get('watch') or {}
    state = _state(conn)
    if state['at'] is not None and now - state['at'] < w['interval_s']:
        return state['open']
    wanted, failed = {}, set()
    for name, check in (('warden', lambda: warden(resources)), ('velocity', lambda: velocity(conn, w, now)),
                        ('context', lambda: context(runs, w))):
        try:
            for notice in check():
                wanted[notice['id']] = notice
        except Exception:
            failed.add(name)
            log.exception('watch check %s failed', name)
    episodes = state['episodes']
    pid = lambda base: base if not episodes.get(base) else f'{base}#{episodes[base]}'
    absent, opened_before = state['absent'], set(state['open'])
    keep = [i for i in state['open'] if i.split(':', 1)[0] in failed]
    for ident in state['open']:
        base = ident.split('#', 1)[0]
        if ident in keep:
            continue
        if base in wanted and pid(base) == ident:
            absent.pop(ident, None)
            continue
        absent[ident] = absent.get(ident, 0) + 1
        if absent[ident] < CLEAR_INTERVALS:      # a condition that flaps keeps its one notice
            keep.append(ident)
            continue
        try:
            disk_notices.retract(conn, ident, kind=KIND, actor=ACTOR)
            absent.pop(ident, None)
            episodes[base] = episodes.get(base, 0) + 1      # if the condition returns it is a new notice
        except Exception:
            keep.append(ident)
            log.exception('watch could not retract %s', ident)
    opened = []
    for base, notice in wanted.items():
        ident = pid(base)
        try:
            if ident not in opened_before:
                disk_notices.publish(conn, {**notice, 'id': ident}, kind=KIND, epic_title=EPIC, actor=ACTOR)
            opened.append(ident)
        except Exception:
            log.exception('watch could not publish %s', ident)
    ids = list(dict.fromkeys(keep + opened))
    live = {i.split('#', 1)[0] for i in ids} | set(wanted)
    for base in [b for b in episodes if b not in live and b.startswith(PER_ITEM)]:
        del episodes[base]      # a run or task id does not come back as the same condition
    absent = {i: n for i, n in absent.items() if i in ids}
    ops.set_setting(conn, STATE_KEY, json.dumps({'at': now, 'open': ids, 'episodes': episodes, 'absent': absent}))
    return ids
