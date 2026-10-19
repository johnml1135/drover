"""Per-seat allowance observations, polling and use-it-or-lose-it ordering."""
import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import config
from .ledger import ops, tx

WINDOWS = {300: 'five_hour', 10080: 'seven_day'}
HOURS = {'five_hour': 5, 'seven_day': 168}
CACHE_S, POLL_S = 60, 300


def next_reset(text, zone, now=None):
    """The next future calendar instance of a yearless reset in its stated zone."""
    now = time.time() if now is None else now
    try:
        tz = ZoneInfo(zone)
    except ZoneInfoNotFoundError:
        # Windows does not ship IANA tzdata with Python. Reuse the configured WSL distro's database.
        if os.name != 'nt' or any(p in ('', '.', '..') for p in zone.split('/')) or '\\' in zone:
            raise ValueError('timezone data unavailable') from None
        source = Path('//wsl.localhost') / config.get()['paths']['wsl_distro'] / 'usr/share/zoneinfo' / zone
        try:
            with source.open('rb') as f:
                tz = ZoneInfo.from_file(f, key=zone)
        except OSError:
            raise ValueError('timezone data unavailable; use manual UTC entry') from None
    local = datetime.fromtimestamp(now, tz)
    for year in range(local.year, local.year + 5):
        for fmt in ('%Y %b %d, %I:%M%p', '%Y %b %d, %I%p'):
            try:
                value = datetime.strptime(f'{year} {text.strip()}', fmt)
            except ValueError:
                continue
            candidates = []
            for fold in (0, 1):
                epoch = value.replace(tzinfo=tz, fold=fold).timestamp()
                # Ignore nonexistent spring-forward wall times; consider both fall-back instances.
                if epoch > now and datetime.fromtimestamp(epoch, tz).replace(tzinfo=None) == value:
                    candidates.append(epoch)
            if candidates:
                return min(candidates)
    raise ValueError('unrecognised reset time')


def parse_claude(raw, now=None):
    rows = []
    for label, window in (('Current session', 'five_hour'), ('Current week (all models)', 'seven_day')):
        m = re.search(r'^' + re.escape(label) + r': (\d+)% used(?: · resets (.+?) \(([^)]+)\))?\s*$', raw, re.M)
        if not m or int(m[1]) > 100 or (not m[2] and (window == 'seven_day' or int(m[1]) != 0)):
            raise ValueError('unrecognised /usage output')
        rows.append({'window': window, 'used_pct': float(m[1]),
                     'resets_at': next_reset(m[2], m[3], now) if m[2] else None})
    return rows


def parse_codex(raw):
    limits = json.loads(raw)
    if limits is None:
        return []
    if not isinstance(limits, dict):
        raise ValueError('invalid rate limits')
    rows = []
    for name in ('primary', 'secondary'):
        w = limits.get(name)
        if w is None:
            continue
        if not isinstance(w, dict) or w.get('window_minutes') not in WINDOWS:
            raise ValueError('unknown allowance window')
        pct, reset = float(w['used_percent']), float(w['resets_at'])
        if not math.isfinite(pct) or not 0 <= pct <= 100 or not math.isfinite(reset):
            raise ValueError('invalid allowance value')
        rows.append({'window': WINDOWS[w['window_minutes']], 'used_pct': pct, 'resets_at': reset})
    return rows


def read_codex_raw(home):
    """Newest rollout, last token_count only; null windows never reuse an earlier observation."""
    paths = list((Path(home) / 'sessions').rglob('rollout-*.jsonl'))
    if not paths:
        raise ValueError('no rollout reading')
    path = max(paths, key=lambda p: (p.stat().st_mtime_ns, p.name))
    raw = None
    with path.open(encoding='utf-8', errors='replace') as f:
        for line in f:
            try:
                ev = json.loads(line)
            except ValueError:
                continue  # a trailing partial line is normal while a rollout is being written
            if not isinstance(ev, dict):
                continue
            payload = ev.get('payload') or {}
            if isinstance(payload, dict) and payload.get('type') == 'token_count':
                raw = json.dumps(payload.get('rate_limits'))
    if raw is None:
        raise ValueError('no token_count reading')
    return raw


def read_codex(home):
    raw = read_codex_raw(home)
    return parse_codex(raw), raw


def readings(conn, seat=None, since=None):
    sql, args = 'SELECT * FROM allowance_readings WHERE 1=1', []
    for col, value, op in (('seat', seat, '='), ('ts', since, '>=')):
        if value is not None:
            sql += f' AND {col}{op}?'
            args.append(value)
    return [dict(r) for r in conn.execute(sql + ' ORDER BY ts,id', args)]


def store(conn, seat, rows, raw, now=None, ok=True):
    now = time.time() if now is None else now
    with tx(conn):
        for r in rows or [{'window': 'unknown', 'used_pct': None, 'resets_at': None}]:
            conn.execute('INSERT INTO allowance_readings(seat,ts,window,used_pct,resets_at,raw_text,ok) '
                         'VALUES(?,?,?,?,?,?,?)',
                         (seat, now, r['window'], r['used_pct'], r['resets_at'], raw, int(ok)))
        if not ok:
            ops.event(conn, 'usage.alert', 'usage', {'seat': seat, 'message': 'Allowance reading failed; enter a manual reading.'})


def manual(conn, seat, window, pct, resets, now=None):
    now = time.time() if now is None else now
    if window not in HOURS:
        raise ValueError('window must be five_hour or seven_day')
    pct = float(pct)
    try:
        reset = float(resets)
    except (ValueError, TypeError):
        reset = datetime.fromisoformat(resets.replace('Z', '+00:00'))
    if isinstance(reset, datetime):
        if reset.tzinfo is None:
            raise ValueError('reset must include a timezone')
        reset = reset.timestamp()
    if not math.isfinite(pct) or not 0 <= pct <= 100 or not math.isfinite(reset) or reset <= now:
        raise ValueError('percentage must be 0..100 and reset must be in the future')
    row = {'window': window, 'used_pct': pct, 'resets_at': reset}
    store(conn, seat, [row], json.dumps({'source': 'manual', **row}), now)
    return row


def latest(conn, seat, now=None):
    """Latest observation per window, invalidated by a more recent failed probe."""
    now = time.time() if now is None else now
    invalid = conn.execute("SELECT ts,id FROM allowance_readings WHERE seat=? AND ts<=? "
                           "AND (ok=0 OR window='unknown') ORDER BY ts DESC,id DESC LIMIT 1", (seat, now)).fetchone()
    out = {}
    for window in HOURS:
        row = conn.execute('SELECT * FROM allowance_readings WHERE seat=? AND window=? AND ts<=? '
                           'ORDER BY ts DESC,id DESC LIMIT 1', (seat, window, now)).fetchone()
        if row is None or not row['ok'] or row['used_pct'] is None:
            continue
        if invalid and (row['ts'], row['id']) < (invalid['ts'], invalid['id']) and '"source": "manual"' not in row['raw_text']:
            continue
        if row['resets_at'] is None or row['resets_at'] > now:
            out[window] = dict(row)
    return out


def active_load(conn):
    load = {}
    for row in conn.execute("SELECT r.backend,s.data FROM runs r LEFT JOIN run_stats s ON s.run_id=r.id "
                            "WHERE r.ended IS NULL AND r.backend<>'local'"):
        seat = json.loads(row['data'] or '{}').get('seat')
        if not seat:
            fallback = config.seats_for(row['backend'])
            seat = fallback[0]['name'] if fallback else None
        if seat:
            load[seat] = load.get(seat, 0) + 1
    return load


def headroom(conn, seat, now=None):
    now = time.time() if now is None else now
    windows = latest(conn, seat, now)
    if not windows:
        return None
    return {'remaining_pct': min(100 - r['used_pct'] for r in windows.values()),
            'windows': {w: {'remaining_pct': 100 - r['used_pct'], 'used_pct': r['used_pct'],
                            'hours_to_reset': (r['resets_at'] - now) / 3600 if r['resets_at'] else None}
                        for w, r in windows.items()}}


def burn_rate(conn, seat, window, now=None):
    """Percentage points per hour in the last 24 hours of the current reset window."""
    now = time.time() if now is None else now
    current = latest(conn, seat, now).get(window)
    if not current:
        return None
    rows = [r for r in readings(conn, seat, now - 86400) if r['ok'] and r['window'] == window
            and r['resets_at'] == current['resets_at'] and r['ts'] <= current['ts']]
    # A decreased counter is a reset even if the provider reused a reset timestamp.
    start = 0
    for i in range(1, len(rows)):
        if rows[i]['used_pct'] < rows[i - 1]['used_pct']:
            start = i
    rows = rows[start:]
    if len(rows) < 2 or rows[-1]['ts'] <= rows[0]['ts']:
        return None
    return max(0, (rows[-1]['used_pct'] - rows[0]['used_pct']) * 3600 / (rows[-1]['ts'] - rows[0]['ts']))


def order(conn, seats=None, now=None, director=False, seat_load=None, threshold=None):
    now = time.time() if now is None else now
    seats = config.get()['seats'] if seats is None else seats
    threshold = config.get()['seats_policy']['waste_threshold_pct'] if threshold is None else threshold
    result = []
    for seat in seats:
        windows = latest(conn, seat['name'], now)
        h = headroom(conn, seat['name'], now)
        weekly = windows.get('seven_day')
        burn = burn_rate(conn, seat['name'], 'seven_day', now)
        waste = None
        if weekly and weekly['resets_at'] and burn is not None:
            waste = max(0, 100 - weekly['used_pct'] - burn * (weekly['resets_at'] - now) / 3600)
        urgent = waste is not None and waste >= threshold
        floor = seat['reserve_pct']
        eligible = bool(h) and h['remaining_pct'] > 0 and (director and seat['director'] or h['remaining_pct'] > floor)
        reason = f'projected waste {waste:.1f}% at reset' if urgent else f"priority {seat['priority']}"
        if not h:
            reason, eligible = 'no current allowance reading', False
        elif not eligible:
            reason = f'below reserve {floor:g}%'
        if seat['max_workers'] and (seat_load or {}).get(seat['name'], 0) >= seat['max_workers']:
            eligible, reason = False, 'max_workers reached'
        result.append({'seat': seat['name'], 'backend': seat['backend'], 'priority': seat['priority'],
                       'eligible': eligible, 'urgent': urgent, 'waste_pct': waste, 'burn_per_hour': burn,
                       'reason': reason, 'readings': [{k: v for k, v in r.items() if k != 'raw_text'} for r in windows.values()]})
    return sorted(result, key=lambda r: (not r['eligible'], not r['urgent'], r['priority'], r['seat']))


class Poller:
    def __init__(self, conn, run=subprocess.run, now=time.time, windows=None, launcher=None):
        from .launch import RunnerLauncher
        self.conn, self.run, self.now = conn, run, now
        self.windows = os.name == 'nt' if windows is None else windows
        launcher = launcher or RunnerLauncher(windows=self.windows)
        self.wsl_exe = getattr(launcher, 'wsl_exe', lambda argv: argv)
        self.distro = getattr(launcher, 'distro', config.get()['paths']['wsl_distro'])
        self.checked = {}

    def refresh(self, seat, cache_s=CACHE_S):
        now, name = self.now(), seat['name']
        if name in self.checked and 0 <= now - self.checked[name] < cache_s:
            return headroom(self.conn, name, now)
        # Manual entries survive probe errors for the normal five-minute polling interval.
        recent = self.conn.execute('SELECT ts,raw_text FROM allowance_readings WHERE seat=? ORDER BY ts DESC,id DESC LIMIT 1',
                                   (name,)).fetchone()
        if recent and 0 <= now - recent['ts'] < POLL_S and '"source": "manual"' in recent['raw_text']:
            return headroom(self.conn, name, now)
        self.checked[name] = now
        raw = ''
        try:
            if seat['backend'] == 'claude':
                argv = [seat['binary'], '-p', '/usage', '--output-format', 'json']
                env = {**os.environ, **{k: config.native(v) for k, v in config.seat_env(seat).items()}}
                if self.windows and seat['placement'] == 'wsl':
                    from .runner import to_wsl_path
                    argv = self.wsl_exe(argv)
                    account = {k: to_wsl_path(v) for k, v in config.seat_env(seat).items()}
                    code = ("import json,os,subprocess,sys; "
                            "os.environ.update({k:os.path.expanduser(v) for k,v in json.loads(sys.argv[1]).items()}); "
                            "sys.exit(subprocess.call(json.loads(sys.argv[2])))")
                    argv = ['wsl.exe', '-d', self.distro, '--exec', 'python3', '-c', code,
                            json.dumps(account), json.dumps(argv)]
                proc = self.run(argv, env=env, capture_output=True, text=True, encoding='utf-8', timeout=30)
                raw = proc.stdout or ''
                if proc.returncode:
                    raise ValueError('usage command failed')
                payload = json.loads(raw)
                if not isinstance(payload, dict) or not isinstance(payload.get('result'), str):
                    raise ValueError('missing result')
                raw = payload['result']
                rows = parse_claude(raw, now)
            else:
                home = seat.get('codex_home') or seat.get('config_dir') or '~/.codex'
                if self.windows and seat['placement'] == 'wsl':
                    from .runner import to_wsl_path
                    code = ("import json,os,sys; sys.path.insert(0,sys.argv[1]); "
                            "from drover.usage import read_codex_raw; "
                            "print(json.dumps(read_codex_raw(os.path.expanduser(sys.argv[2]))))")
                    argv = ['wsl.exe', '-d', self.distro, '--exec', 'python3', '-c', code,
                            to_wsl_path(str(Path(__file__).resolve().parents[1])), to_wsl_path(home)]
                    proc = self.run(argv, capture_output=True, text=True, encoding='utf-8', timeout=30)
                    if proc.returncode:
                        raise ValueError('rollout reader failed')
                    raw = json.loads(proc.stdout)
                else:
                    raw = read_codex_raw(config.native(home))
                rows = parse_codex(raw)
                # Null windows clear provider observations, but unexpired manual fallbacks survive.
                present = {r['window'] for r in rows}
                current = latest(self.conn, name, now)
                manual_windows = {w for w, r in current.items() if '"source": "manual"' in r['raw_text']}
                rows += [{'window': w, 'used_pct': None, 'resets_at': None}
                         for w in HOURS if w not in present and w not in manual_windows]
            store(self.conn, name, rows, raw, now, ok=True)
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            store(self.conn, name, [], raw, now, ok=False)
        return headroom(self.conn, name, now)

    def poll(self, seats, active):
        for seat in seats:
            if active.get(seat['name'], 0):
                self.refresh(seat, POLL_S)


def main(argv=None):
    from .ledger import connect, default_path
    ap = argparse.ArgumentParser(prog='drover.usage')
    ap.add_argument('--db')
    sub = ap.add_subparsers(dest='command', required=True)
    p = sub.add_parser('order')
    p.add_argument('--json', action='store_true')
    p = sub.add_parser('set')
    for field in ('seat', 'window', 'pct', 'resets'):
        p.add_argument(field)
    p.add_argument('--json', action='store_true')
    a = ap.parse_args(argv)
    try:
        if a.command == 'set' and not config.seat_by_name(a.seat):
            raise ValueError('unknown seat')
        # WSL clients use the authenticated hub; the ledger is never opened over DrvFs.
        if not a.db and os.name != 'nt' and not os.environ.get('DROVER_LEDGER'):
            from . import stats
            result = stats.hub_request('/usage/order' if a.command == 'order' else '/usage',
                                       None if a.command == 'order' else
                                       {'seat': a.seat, 'window': a.window, 'pct': float(a.pct), 'resets': a.resets})
        else:
            conn = connect(a.db or default_path())
            try:
                result = order(conn) if a.command == 'order' else manual(conn, a.seat, a.window, a.pct, a.resets)
            finally:
                conn.close()
        print(json.dumps(result) if a.json else json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError) as e:
        print(f'drover.usage: {e}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
