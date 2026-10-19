"""Subscription allowance: read Claude/Codex usage, compute headroom, admit work, learn rates."""
import argparse
import json
import math
import os
import time
import tomllib
from datetime import datetime
from pathlib import Path

STALE_S = 1800
TAIL_BYTES = 262144
CODEX_FILES_TRIED = 5
WINDOWS = {'five_hour': 5.0, 'seven_day': 168.0}  # hours
CODEX_WINDOW_NAMES = {300: 'five_hour', 10080: 'seven_day'}
PLANS = ('claude', 'codex')
DIRECTOR_ROLES = ['director', 'planner']
FIVE_HOUR_BASE_PCT = 5.0
FIVE_HOUR_PER_HOUR_PCT = 5.0


def __getattr__(name):   # DEFAULTS stays importable; it follows the active config
    if name == 'DEFAULTS':
        return defaults()
    raise AttributeError(name)


def defaults():
    """Per-plan reserves from the config's seats (a plan has one reading, so the best-priority seat's reserve and the
    director seat's extra reserve): 10% each, plus 25% kept back for the Claude director, unless the config says more."""
    from . import config, route
    out = {}
    seats = config.get()['seats']
    for backend, (reserve, director) in route.reserves(seats).items():
        best = min((s for s in seats if s['backend'] == backend), key=lambda s: s['priority'])
        out[backend] = dict(reserve_pct=reserve, director_reserve_pct=director, director_roles=list(DIRECTOR_ROLES),
                            five_hour_base_pct=best.get('five_hour_base_pct', FIVE_HOUR_BASE_PCT),
                            five_hour_per_hour_pct=best.get('five_hour_per_hour_pct', FIVE_HOUR_PER_HOUR_PCT))
    return out


def five_hour_floor(hours_to_reset, base=None, per_hour=None):
    """Five-hour floor: base% plus per_hour% for each hour until the window resets (unknown reset: a full window)."""
    base = FIVE_HOUR_BASE_PCT if base is None else base
    per_hour = FIVE_HOUR_PER_HOUR_PCT if per_hour is None else per_hour
    hours = WINDOWS['five_hour'] if hours_to_reset is None else min(max(0.0, hours_to_reset), WINDOWS['five_hour'])
    return base + per_hour * hours
# List-price ratios relative to the cheapest-listed baseline family of each plan.
PRIOR_RATIO = {'claude': {'opus': 2.0, 'sonnet': 1.0, 'haiku': 0.05},
               'codex': {'sol': 20.0, 'luna': 1.0}}
PRIOR_BASE = {'claude': 'sonnet', 'codex': 'luna'}
PRIOR_BASE_RATE = 1.0  # placeholder allowance-% per Mtok for the baseline family
MIN_SAMPLES = 10
MANUAL_MAX_AGE_S = 86400


def state_dir(state=None):
    return Path(state or os.environ.get('DROVER_STATE') or Path.home() / '.local/state/drover')


def home_dir(home=None):
    return Path(home or os.environ.get('DROVER_USER_HOME') or Path.home())


def _num(v, lo=None, hi=None):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v): return None
    if lo is not None and v < lo or hi is not None and v > hi: return None
    return float(v)


def _load_json(path):
    try: return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError): return None


def _result(plan, source, observed_at=None, windows=None, **extra):
    return dict(plan=plan, source=source, observed_at=observed_at, windows=windows or {}, **extra)


def _freshen(res, now):
    if res['source'] == 'live' and (res['observed_at'] is None or now - res['observed_at'] > STALE_S):
        res['source'] = 'stale'
    return res


def _live_windows(raw, now):
    """Keep only well-formed windows that have not passed their reset."""
    out = {}
    for name, hours in WINDOWS.items():
        w = raw.get(name) if isinstance(raw, dict) else None
        if not isinstance(w, dict): continue
        used, reset = _num(w.get('used_percentage'), 0, 100), _num(w.get('resets_at'), 0)
        if used is None or reset is None or reset <= now: continue
        out[name] = dict(used_pct=used, resets_at=reset, window_hours=hours)
    return out


def read_claude(state=None, now=None):
    now = time.time() if now is None else now
    best = None
    for path in sorted((state_dir(state) / 'allowance').glob('claude-*.json')):
        snap = _load_json(path)
        if not isinstance(snap, dict): continue
        ts = _num(snap.get('ts'))
        if ts is None:
            try: ts = path.stat().st_mtime
            except OSError: continue
        windows = _live_windows(snap.get('rate_limits'), now)
        if windows and (best is None or ts > best[0]): best = (ts, windows, snap)
    if best is None: return _result('claude', 'unknown')
    model = best[2].get('model')
    return _freshen(_result('claude', 'live', best[0], best[1], model=model if isinstance(model, dict) else None,
                            session_id=best[2].get('session_id')), now)


def _codex_files(home):
    root = home_dir(home) / '.codex' / 'sessions'
    found = []
    for y in sorted((d for d in root.glob('*') if d.is_dir()), reverse=True)[:2]:
        for m in sorted((d for d in y.glob('*') if d.is_dir()), reverse=True)[:2]:
            for d in sorted((d for d in m.glob('*') if d.is_dir()), reverse=True)[:3]:
                found += list(d.glob('rollout-*.jsonl'))
    def mtime(p):
        try: return p.stat().st_mtime
        except OSError: return 0
    return sorted(found, key=mtime, reverse=True)[:CODEX_FILES_TRIED]


def _tail_lines(path):
    with open(path, 'rb') as f:
        size = f.seek(0, os.SEEK_END)
        f.seek(max(0, size - TAIL_BYTES))
        data = f.read()
    lines = data.split(b'\n')
    return lines[1:] if size > TAIL_BYTES else lines  # first line may be cut


def _iso(s):
    try: return datetime.fromisoformat(str(s).replace('Z', '+00:00')).timestamp()
    except ValueError: return None


def _codex_windows(limits, now):
    out = {}
    for key in ('primary', 'secondary'):
        w = limits.get(key) if isinstance(limits, dict) else None
        if not isinstance(w, dict): continue
        used, mins, reset = _num(w.get('used_percent'), 0, 100), _num(w.get('window_minutes'), 1), _num(w.get('resets_at'), 0)
        if used is None or mins is None or reset is None or reset <= now: continue
        name = CODEX_WINDOW_NAMES.get(int(mins), 'window_%dm' % int(mins))
        out[name] = dict(used_pct=used, resets_at=reset, window_hours=mins / 60)
    return out


def read_codex(home=None, now=None):
    now = time.time() if now is None else now
    for path in _codex_files(home):
        try: lines = _tail_lines(path)
        except OSError: continue
        for raw in reversed(lines):
            if b'rate_limits' not in raw: continue
            try: ev = json.loads(raw)
            except ValueError: continue
            p = ev.get('payload') if isinstance(ev, dict) else None
            if not isinstance(p, dict) or p.get('type') != 'token_count' or not isinstance(p.get('rate_limits'), dict): continue
            windows = _codex_windows(p['rate_limits'], now)
            if not windows: continue
            ts = _iso(ev.get('timestamp'))
            if ts is None:
                try: ts = path.stat().st_mtime
                except OSError: break
            return _freshen(_result('codex', 'live', ts, windows, file=str(path)), now)
    return _result('codex', 'unknown')


def set_manual(plan, remaining_pct, state=None, now=None):
    rem = _num(remaining_pct, 0, 100)
    if plan not in PLANS or rem is None: raise ValueError('plan claude|codex and remaining_pct 0..100 required')
    path = state_dir(state) / 'allowance' / f'manual-{plan}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(dict(plan=plan, remaining_pct=rem, set_at=time.time() if now is None else now)), encoding='utf-8')
    os.replace(tmp, path)
    return path


def read_manual(plan, state=None, now=None):
    now = time.time() if now is None else now
    d = _load_json(state_dir(state) / 'allowance' / f'manual-{plan}.json')
    rem = _num(d.get('remaining_pct'), 0, 100) if isinstance(d, dict) else None
    if rem is None: return None
    at = _num(d.get('set_at'))
    res = _result(plan, 'manual', at, {'seven_day': dict(used_pct=100 - rem, resets_at=None, window_hours=168.0)})
    if at is None or now - at > MANUAL_MAX_AGE_S: res['source'] = 'stale'
    return res


def read_plan(plan, state=None, home=None, now=None):
    """Live reading when fresh, else the manual value, else the stale/unknown live result."""
    if plan not in PLANS: raise ValueError('unknown plan ' + plan)
    live = read_claude(state, now) if plan == 'claude' else read_codex(home, now)
    if live['source'] == 'live': return live
    return read_manual(plan, state, now) or live


def load_config(state=None):
    cfg = {k: dict(v) for k, v in defaults().items()}
    base = state_dir(state) / 'allowance'
    user = {}
    try: user = tomllib.loads((base / 'config.toml').read_text(encoding='utf-8'))
    except (OSError, tomllib.TOMLDecodeError):
        user = _load_json(base / 'config.json') or {}
    for plan, vals in (user.items() if isinstance(user, dict) else ()):
        if plan in cfg and isinstance(vals, dict):
            for k in ('reserve_pct', 'director_reserve_pct', 'five_hour_base_pct', 'five_hour_per_hour_pct'):
                v = _num(vals.get(k), 0, 100)
                if v is not None: cfg[plan][k] = v
            if isinstance(vals.get('director_roles'), list): cfg[plan]['director_roles'] = [str(r) for r in vals['director_roles']]
    return cfg


def headroom(plan, state=None, home=None, now=None, reading=None, config=None):
    """Remaining % per window, hours to reset and whether the burn rate fits the reserve."""
    now = time.time() if now is None else now
    reading = reading or read_plan(plan, state, home, now)
    reserve = (config or load_config(state))[plan]['reserve_pct']
    wins = {}
    for name, w in reading['windows'].items():
        remaining = 100 - w['used_pct']
        hours = None if w['resets_at'] is None else max(0.0, (w['resets_at'] - now) / 3600)
        ok = remaining > reserve
        if ok and hours is not None and w['window_hours']:
            elapsed = 1 - min(hours, w['window_hours']) / w['window_hours']
            if elapsed >= 0.05: ok = w['used_pct'] / elapsed <= 100 - reserve  # projected use at reset
        wins[name] = dict(remaining_pct=remaining, hours_to_reset=hours, burn_rate_ok=ok)
    return dict(plan=plan, source=reading['source'], observed_at=reading['observed_at'], windows=wins,
                burn_rate_ok=bool(wins) and all(w['burn_rate_ok'] for w in wins.values()))


def admit(plan, role, predicted_pct, state=None, home=None, now=None, reading=None, config=None):
    """Allow/deny a run predicted to use predicted_pct.

    A scalar is the seven_day cost only; five_hour is then checked against its floor with no
    prediction. Pass a dict {window: pct} to predict per window. Non-director roles are denied
    when a plan has a director reserve and the seven_day window is missing.
    """
    cfg = (config if config is not None else load_config(state)).get(plan)
    if cfg is None:
        return dict(allow=False, reason='no configured seat for backend', plan=plan, role=role, source='unknown')
    head = headroom(plan, state, home, now, reading, config)
    privileged = role in cfg['director_roles']
    decision = lambda allow, reason: dict(allow=allow, reason=reason, plan=plan, role=role, source=head['source'])
    if not head['windows'] or head['source'] in ('stale', 'unknown'):
        if privileged:
            week = head['windows'].get('seven_day')
            if week and week['remaining_pct'] < cfg['reserve_pct']:
                return decision(False, f'allowance {head["source"]}; last known weekly {week["remaining_pct"]:g}% is below the {cfg["reserve_pct"]:g}% reserve, no probe')
            return decision(True, f'allowance {head["source"]}; director/planner may probe')
        return decision(False, f'allowance {head["source"]}; workers wait for an observation')
    if not privileged and cfg['director_reserve_pct'] > 0 and 'seven_day' not in head['windows']:
        return decision(False, 'weekly window unknown; director reserve cannot be checked')
    for name, w in head['windows'].items():
        need = predicted_pct.get(name, 0) if isinstance(predicted_pct, dict) else (predicted_pct if name == 'seven_day' else 0)
        need = _num(need, 0)
        if need is None: return decision(False, 'predicted_pct must be a non-negative number')
        floor = cfg['reserve_pct']
        if name == 'five_hour':
            floor = five_hour_floor(w['hours_to_reset'], cfg.get('five_hour_base_pct'), cfg.get('five_hour_per_hour_pct'))
        elif name == 'seven_day' and not privileged: floor += cfg['director_reserve_pct']
        left = w['remaining_pct'] - need
        if left < floor:
            why = 'five-hour formula' if name == 'five_hour' else 'director reserve' if floor > cfg['reserve_pct'] else 'reserve'
            return decision(False, f'{name}: {left:g}% would remain, below the {floor:g}% {why} floor')
    return decision(True, 'within reserves')


def _family(model, plan):
    m = str(model).lower()
    return next((f for f in PRIOR_RATIO[plan] if f in m), None)


def fit_rate(samples, plan='claude', model='sonnet', base_rate=PRIOR_BASE_RATE):
    """Allowance-% per million list-price tokens: least squares through the origin, else a ratio prior."""
    good = [(float(x), float(y)) for x, y in samples
            if _num(x, 0) is not None and _num(y) is not None and x > 0]
    sxx = sum(x * x for x, _ in good)
    if len(good) >= MIN_SAMPLES and sxx > 0:
        rate = max(0.0, sum(x * y for x, y in good) / sxx)
        return dict(rate=rate, n=len(good), source='fit')
    fam = _family(model, plan)
    if fam is None: raise ValueError(f'no price prior for model {model!r} on {plan}')
    return dict(rate=base_rate * PRIOR_RATIO[plan][fam] / PRIOR_RATIO[plan][PRIOR_BASE[plan]], n=len(good), source='prior')


def status(state=None, home=None, now=None):
    now = time.time() if now is None else now
    cfg = load_config(state)
    return {p: headroom(p, state, home, now, config=cfg) for p in cfg}


def _fmt_status(st):
    lines = []
    for plan, h in st.items():
        lines.append(f'{plan}: source={h["source"]} burn_rate_ok={h["burn_rate_ok"]}')
        for name, w in h['windows'].items():
            hrs = '?' if w['hours_to_reset'] is None else f'{w["hours_to_reset"]:.1f}h'
            lines.append(f'  {name}: {w["remaining_pct"]:.1f}% remaining, resets in {hrs}, burn_rate_ok={w["burn_rate_ok"]}')
    return '\n'.join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog='drover.allowance')
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--json', action='store_true', default=argparse.SUPPRESS)
    ap.add_argument('--json', action='store_true')
    sub = ap.add_subparsers(dest='cmd')
    sub.add_parser('status', parents=[common])
    s = sub.add_parser('set', parents=[common]); s.add_argument('plan', choices=sorted(PLANS)); s.add_argument('--remaining-pct', type=float, required=True)
    a = sub.add_parser('admit', parents=[common]); a.add_argument('plan', choices=sorted(PLANS))
    a.add_argument('--role', default='worker'); a.add_argument('--predicted-pct', type=float, required=True)
    args = ap.parse_args(argv)
    if args.cmd == 'set':
        try: out = dict(path=str(set_manual(args.plan, args.remaining_pct)))
        except ValueError as e: ap.error(str(e))
        print(json.dumps(out) if args.json else f'wrote {out["path"]}')
        return 0
    if args.cmd == 'admit':
        d = admit(args.plan, args.role, args.predicted_pct)
        print(json.dumps(d) if args.json else ('ALLOW' if d['allow'] else 'DENY') + ': ' + d['reason'])
        return 0 if d['allow'] else 1
    st = status()
    print(json.dumps(st) if args.json else _fmt_status(st))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
