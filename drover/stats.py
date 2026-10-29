"""Permanent run statistics and bounded hourly/daily history. The CLI is read-only."""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import config
from .ledger import ops, tx

COUNTERS = ('runs', 'sessions', 'handoffs', 'compactions', 'retries', 'escalations', 'rework')
PEAKS = ('peak_cpu_pct', 'peak_ram_mb', 'peak_disk_bytes')


def stamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def hour(epoch):
    return stamp(epoch - epoch % 3600)


def initial_run(conn, rid):
    r = dict(conn.execute('SELECT * FROM runs WHERE id=?', (rid,)).fetchone())
    prior = [dict(x) for x in conn.execute("SELECT * FROM runs WHERE task_id=? AND role=? AND id<? ORDER BY id",
                                          (r['task_id'], r['role'], rid))]
    last = prior[-1] if prior else None
    data = {k: 0 for k in COUNTERS}
    data.update({k: None for k in PEAKS})
    data.update(runs=1, sessions=0 if r['backend'] == 'local' else 1, seat=None, placement=None,
                retries=int(bool(last)), escalations=int(bool(last and last['model'] != r['model'])),
                rework=int(bool(last and (last['outcome'] in ('hold', 'gate-fail', 'failed') or
                    conn.execute("SELECT 1 FROM runs WHERE task_id=? AND role='review' AND outcome='hold' AND id>?",
                                 (r['task_id'], last['id'])).fetchone()))),
                started=r['started'], ended=None, duration_s=None, tokens_by_model={}, gate_result=None,
                policy_version=r['policy_version'])
    conn.execute('INSERT INTO run_stats(run_id,data) VALUES(?,?)', (rid, json.dumps(data)))


def update_run(conn, rid, **fields):
    row = conn.execute('SELECT data FROM run_stats WHERE run_id=?', (rid,)).fetchone()
    if row is None:
        if not conn.execute('SELECT 1 FROM runs WHERE id=?', (rid,)).fetchone():
            raise ops.NotFound(f'run {rid}')
        initial_run(conn, rid)
        row = conn.execute('SELECT data FROM run_stats WHERE run_id=?', (rid,)).fetchone()
    data = json.loads(row[0])
    allowed = {*COUNTERS, *PEAKS, 'seat', 'placement', 'started', 'ended', 'duration_s', 'tokens_by_model',
               'gate_result', 'policy_version'}
    if set(fields) - allowed:
        raise ValueError('unknown run statistic')
    for k, v in fields.items():
        if k in COUNTERS and (not isinstance(v, int) or isinstance(v, bool) or v < 0):
            raise ValueError('stat counters must be nonnegative integers')
        if k in PEAKS and v is not None:
            import math
            if not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
                raise ValueError('peaks must be nonnegative finite numbers')
            v = max(v, data.get(k) or 0)
        data[k] = v
    with tx(conn):
        conn.execute('UPDATE run_stats SET data=? WHERE run_id=?', (json.dumps(data), rid))
    return data


def _bucket(conn, table, ts):
    row = conn.execute(f'SELECT data FROM {table} WHERE ts=?', (ts,)).fetchone()
    return json.loads(row[0]) if row else {'tokens_by_model': {}, 'landings': 0, 'completed_runs': 0}


def _save(conn, table, ts, data):
    conn.execute(f'INSERT INTO {table}(ts,data) VALUES(?,?) ON CONFLICT(ts) DO UPDATE SET data=excluded.data',
                 (ts, json.dumps(data)))


def _add_tokens(target, source):
    for model, tokens in source.items():
        dest = target.setdefault(model, {})
        for k, n in tokens.items():
            dest[k] = dest.get(k, 0) + n


def finish_run(conn, rid):
    r = dict(conn.execute('SELECT * FROM runs WHERE id=?', (rid,)).fetchone())
    old = conn.execute('SELECT data FROM run_stats WHERE run_id=?', (rid,)).fetchone()
    old = json.loads(old[0]) if old else {}
    tokens = old.get('tokens_by_model') or {}
    if not tokens and r['backend'] != 'local' and r['model'] and any(r[k] is not None for k in ('tokens_in', 'tokens_out', 'tokens_cached')):
        tokens[r['model']] = {k: r[k] or 0 for k in ('tokens_in', 'tokens_out', 'tokens_cached')}
    for counts in tokens.values():
        counts['tokens_total'] = counts.get('tokens_in', 0) + counts.get('tokens_out', 0) + (
            counts.get('tokens_cached', 0) + counts.get('tokens_cache_write', 0) if r['backend'] == 'claude' else 0)
    update_run(conn, rid, ended=r['ended'], duration_s=r['wall_s'], tokens_by_model=tokens,
               handoffs=max(old.get('handoffs', 0), int(r['outcome'] == 'rotated')),
               gate_result=r['gate_receipt'])
    ts = hour(ops._parse(r['ended']).timestamp())
    data = _bucket(conn, 'stats_hourly', ts)
    _add_tokens(data['tokens_by_model'], tokens)
    data['completed_runs'] += 1
    _save(conn, 'stats_hourly', ts, data)


def landing(conn, epoch=None):
    ts = hour(time.time() if epoch is None else epoch)
    data = _bucket(conn, 'stats_hourly', ts)
    data['landings'] += 1
    _save(conn, 'stats_hourly', ts, data)


def sample(conn, resources=None, now=None):
    """One current gauge snapshot per UTC hour; flow counters are written at their source."""
    from . import usage
    now = time.time() if now is None else now
    ts = hour(now)
    facts = (resources or {}).get('facts', resources or {})
    allow = {}
    for s in config.get()['seats']:
        allow[s['name']] = {w: {'used_pct': r['used_pct'], 'resets_at': r['resets_at'],
                               'burn_per_hour': usage.burn_rate(conn, s['name'], w, now)}
                            for w, r in usage.latest(conn, s['name'], now).items()}
    queue = {r['status']: r['n'] for r in conn.execute('SELECT status,COUNT(*) n FROM tasks WHERE archived=0 GROUP BY status')}
    workers = conn.execute("SELECT COUNT(*) FROM runs WHERE ended IS NULL AND backend<>'local'").fetchone()[0]
    with tx(conn):
        data = _bucket(conn, 'stats_hourly', ts)
        # Refresh gauges within the current hour, preserving additive counters.
        data.update(allowance=allow, resources=facts, queue=queue, active_workers=workers, sampled_at=stamp(now))
        _save(conn, 'stats_hourly', ts, data)
    return data


def _rollup(rows):
    """Daily flows sum; gauges retain the latest snapshot and resource ranges."""
    out = {'tokens_by_model': {}, 'landings': 0, 'completed_runs': 0, 'hours': 0}
    for _, data in rows:
        _add_tokens(out['tokens_by_model'], data.get('tokens_by_model', {}))
        for k in ('landings', 'completed_runs'):
            out[k] += data.get(k, 0)
        out['hours'] += data.get('hours', 1)
        for k in ('allowance', 'resources', 'queue', 'active_workers', 'sampled_at'):
            if k in data:
                out[k] = data[k]
        ranges = out.setdefault('resource_ranges', {})
        for k, v in data.get('resources', {}).items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                span = ranges.setdefault(k, {'min': v, 'max': v})
                span['min'], span['max'] = min(span['min'], v), max(span['max'], v)
        for k, span in data.get('resource_ranges', {}).items():
            dest = ranges.setdefault(k, dict(span))
            dest['min'], dest['max'] = min(dest['min'], span['min']), max(dest['max'], span['max'])
    return out


def prune(conn, now=None, runs_dir=None):
    """Once per UTC day: roll old hours into days, retain a year, prune only finished run logs."""
    now = time.time() if now is None else now
    today = stamp(now)[:10]
    if ops.get_setting(conn, '_stats_pruned_day') == today:
        return False
    cutoff = hour(now - 35 * 86400)
    with tx(conn):
        groups = {}
        for r in conn.execute('SELECT * FROM stats_hourly WHERE ts<? ORDER BY ts', (cutoff,)):
            groups.setdefault(r['ts'][:10] + 'T00:00:00Z', []).append((r['ts'], json.loads(r['data'])))
        for day, rows in groups.items():
            old = conn.execute('SELECT data FROM stats_daily WHERE ts=?', (day,)).fetchone()
            if old:
                rows.insert(0, (day, json.loads(old[0])))
            _save(conn, 'stats_daily', day, _rollup(rows))
        conn.execute('DELETE FROM stats_hourly WHERE ts<?', (cutoff,))
        conn.execute('DELETE FROM stats_daily WHERE ts<?', (stamp(now - 365 * 86400)[:10] + 'T00:00:00Z',))
        ops.set_setting(conn, '_stats_pruned_day', today)
    if runs_dir:
        prune_logs(conn, Path(runs_dir), now)
    return True


def prune_logs(conn, root, now):
    ids = {str(r['id']) for r in conn.execute('SELECT id FROM runs WHERE ended IS NOT NULL AND ended<?',
                                             (stamp(now - 30 * 86400),))}
    # Deleted tasks no longer have run rows. Their final runner status still dates their log artifacts.
    if root.is_dir():
        for path in root.iterdir():
            if not path.name.isdigit() or not path.is_dir() or path.is_symlink():
                continue
            try:
                ended = json.loads((path / 'status.json').read_text(encoding='utf-8')).get('ended')
                if ended and datetime.fromisoformat(ended.replace('Z', '+00:00')).timestamp() < now - 30 * 86400:
                    ids.add(path.name)
            except (OSError, ValueError, AttributeError):
                continue
    for rid in ids:
        path = root / rid
        if path.is_dir() and not path.is_symlink():
            for name in ('out.log', 'runner.log', 'tail.log'):
                try:
                    (path / name).unlink(missing_ok=True)
                except OSError:
                    pass


def run_rows(conn, task=None, since=None):
    sql = 'SELECT r.*,s.data stats FROM runs r LEFT JOIN run_stats s ON s.run_id=r.id WHERE 1=1'
    args = []
    for col, value, op in (('r.task_id', task, '='), ('r.started', since, '>=')):
        if value is not None:
            sql += f' AND {col}{op}?'
            args.append(value)
    rows = []
    for r in conn.execute(sql + ' ORDER BY r.id', args):
        row = dict(r)
        row['stats'] = json.loads(row['stats'] or '{}')
        rows.append(row)
    return rows


def task_totals(rows):
    totals = {}
    for r in rows:
        t = totals.setdefault(r['task_id'], {**{k: 0 for k in COUNTERS}, **{k: None for k in PEAKS},
                                           'tokens_by_model': {}, 'duration_s': 0, 'seats': [], 'placements': [],
                                           'started': r['started'], 'ended': None, 'gate_result': None,
                                           'policy_versions': []})
        s = r['stats']
        for k in COUNTERS:
            t[k] += s.get(k, int(k == 'runs'))
        for k in PEAKS:
            if s.get(k) is not None:
                t[k] = max(t[k] or 0, s[k])
        _add_tokens(t['tokens_by_model'], s.get('tokens_by_model', {}))
        t['duration_s'] += r['wall_s'] or 0
        t['ended'] = r['ended']
        t['gate_result'] = r['gate_receipt'] or t['gate_result']
        for src, dest in (('seat', 'seats'), ('placement', 'placements'), ('policy_version', 'policy_versions')):
            if s.get(src) and s[src] not in t[dest]:
                t[dest].append(s[src])
    return totals


def query(conn, since=None, task=None, series_only=False):
    if since:
        dt = datetime.fromisoformat(since.replace('Z', '+00:00'))
        since = stamp(dt.replace(tzinfo=dt.tzinfo or timezone.utc).timestamp())
    def series(table):
        sql = f'SELECT * FROM {table}' + (' WHERE ts>=?' if since else '') + ' ORDER BY ts'
        return [{'ts': r['ts'], **json.loads(r['data'])} for r in conn.execute(sql, (since,) if since else ())]
    if series_only:  # the charts need only the time series, not every run
        return {'hourly': series('stats_hourly'), 'daily': series('stats_daily')}
    rows = run_rows(conn, task, since)
    return {'hourly': series('stats_hourly'), 'daily': series('stats_daily'), 'runs': rows, 'tasks': task_totals(rows)}


def hub_base_url():
    """Explicit hub URL, the Windows gateway under WSL NAT, or localhost."""
    override = os.environ.get('DROVER_HUB_URL')
    if override:
        return override.rstrip('/')
    host = '127.0.0.1'
    if os.name != 'nt':
        from .decide import _gateway_from_route, _is_wsl
        if _is_wsl():
            try:
                proc = subprocess.run(['ip', 'route'], capture_output=True, text=True, timeout=2)
                host = _gateway_from_route(proc.stdout) or host
            except (OSError, subprocess.SubprocessError):
                pass
    return f"http://{host}:{config.get()['hub']['api_port']}"


def hub_request(path, body=None):
    """Use the hub API from WSL, without opening a Windows SQLite file."""
    token = os.environ.get('DROVER_HUB_TOKEN')
    if not token:
        token = config.state_path('hub-token').read_text(encoding='utf-8').strip()
    base = hub_base_url()
    req = Request(base.rstrip('/') + '/api/v1' + path, data=json.dumps(body).encode() if body is not None else None,
                  headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    with urlopen(req, timeout=30) as response:
        return json.load(response)


def main(argv=None):
    ap = argparse.ArgumentParser(prog='drover.stats')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--since')
    ap.add_argument('--task')
    ap.add_argument('--db')
    a = ap.parse_args(argv)
    try:
        if not a.db and os.name != 'nt' and not os.environ.get('DROVER_LEDGER'):
            result = hub_request('/stats?' + urlencode({k: v for k, v in {'since': a.since, 'task': a.task}.items() if v}))
        else:
            from .ledger import default_path
            path = Path(a.db or default_path()).resolve()
            # No directory creation, migrations, WAL setup or settings writes in this reader.
            conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
            conn.row_factory = sqlite3.Row
            try:
                result = query(conn, a.since, a.task)
            finally:
                conn.close()
        print(json.dumps(result) if a.json else json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as e:
        print(f'drover.stats: {e}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
