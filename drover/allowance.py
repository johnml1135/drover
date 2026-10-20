"""Subscription allowance: read Claude/Codex usage, learn usage rates."""
import json
import math
import os
import time
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
