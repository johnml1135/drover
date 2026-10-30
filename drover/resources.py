"""Resource checks for admission, driven by the config's [[trackers]]: load, RAM, disks, GPU VRAM and WSL.

check() returns {verdict: go|queue|hold, reasons, facts, slots, wsl: {verdict, reasons}}. Unknown facts never block.
The wsl tracker never feeds the global verdict (a Windows task must not wait for WSL): the dispatcher consults
result['wsl'] only for work that needs WSL. Runs on WSL (full) and on Windows (no /proc: load is unknown, RAM via
GlobalMemoryStatusEx). With seats placed in WSL the Windows hub also reads WSL's /proc/meminfo
(wsl_ram_available_gb): RAM queues or holds only when every side that has a seat is short.
Tests inject facts through the DROVER_RESOURCES_FAKE env var or the `fake` argument, a JSON object:
  platform "wsl"|"windows", load5, cores, ram_available_gb, ollama_ram_gb,
  disk_free_gb {tracker name: GB} (null or missing = unknown), vram [used_mb, total_mb] or null, read_only bool,
  wsl_up bool, wsl_mem {used_gb, cap_gb}.
CLI exit codes: 0 go, 4 queue, 5 hold.
"""
from drover import placement
import argparse
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path

from . import config as config_mod
from .placement_host import wsl_running
from . import disk

GB = 1024 ** 3
WSL_READ_TIMEOUT_S = 8
EXIT = {'go': 0, 'queue': 4, 'hold': 5}


def trackers_of(config=None):
    """Enabled trackers from the process config or an explicit tracker configuration."""
    cfg = config_mod.get() if config is None else config
    if not isinstance(cfg, dict) or 'trackers' not in cfg:
        raise config_mod.ConfigError("resource configuration must contain trackers; legacy dictionaries are unsupported")
    return [t for t in cfg['trackers'] if t.get('enabled', True)]


def is_windows():
    return os.name == 'nt'


# ---- readers (each returns None when unknown) ---------------------------------------------------------------

# Windows has no load average. CPU busy % (GetSystemTimes between two samples) is smoothed with a 60 s exponential
# average and mapped onto the per-core load scale as busy_fraction * WINDOWS_LOAD_AT_FULL_PER_CORE, so the default
# load thresholds (queue 2.5, hold 3.5 per core) mean about 68% and 95% busy. Documented in drover.example.toml.
WINDOWS_LOAD_AT_FULL_PER_CORE = 3.5 / 0.95
WINDOWS_CPU_SMOOTHING_S = 60.0
_CPU = {'last': None, 'busy': None}


def _windows_system_times():
    """(idle, kernel, user) 100 ns ticks since boot; kernel includes idle. None when unavailable."""
    import ctypes
    from ctypes import wintypes
    idle, kernel, user = (wintypes.FILETIME() for _ in range(3))
    if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        return None
    return tuple((f.dwHighDateTime << 32) | f.dwLowDateTime for f in (idle, kernel, user))


def _windows_cpu_busy(times=None, clock=None, sleep=None):
    """Smoothed CPU busy fraction 0..1, or None. The first call samples over a short window itself."""
    import time
    times = times or _windows_system_times
    clock = clock or time.monotonic
    now, sample = clock(), times()
    if sample is None:
        return None
    last = _CPU['last']
    if last is None:
        (sleep or time.sleep)(0.25)
        last, now, sample = (now, sample), clock(), times()
        if sample is None:
            return None
    (t0, s0), (t1, s1) = last, (now, sample)
    total = (s1[1] - s0[1]) + (s1[2] - s0[2])
    if total > 0:
        instant = min(1.0, max(0.0, 1.0 - (s1[0] - s0[0]) / total))
        previous = _CPU['busy']
        weight = 1.0 if previous is None else 1.0 - math.exp(-max(t1 - t0, 0.0) / WINDOWS_CPU_SMOOTHING_S)
        _CPU['busy'] = instant if previous is None else previous + weight * (instant - previous)
        _CPU['last'] = (t1, s1)
    return _CPU['busy']


def read_load():
    """(5-minute load, cores) or None. On Windows: smoothed CPU busy mapped to the per-core load scale."""
    try:
        load5 = float(Path('/proc/loadavg').read_text().split()[1])
    except (OSError, ValueError, IndexError):
        try:
            load5 = os.getloadavg()[1]
        except (OSError, AttributeError):
            if not is_windows():
                return None
            try:
                busy = _windows_cpu_busy()
            except (OSError, AttributeError):
                return None
            cores = os.cpu_count() or 1
            return None if busy is None else (busy * WINDOWS_LOAD_AT_FULL_PER_CORE * cores, cores)
    return load5, os.cpu_count() or 1


def _windows_ram_gb():
    import ctypes

    class Status(ctypes.Structure):
        _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong), ('total', ctypes.c_ulonglong),
                    ('avail', ctypes.c_ulonglong), ('tpf', ctypes.c_ulonglong), ('apf', ctypes.c_ulonglong),
                    ('tv', ctypes.c_ulonglong), ('av', ctypes.c_ulonglong), ('ave', ctypes.c_ulonglong)]
    status = Status()
    status.length = ctypes.sizeof(Status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return status.avail / GB


def read_ram_gb():
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemAvailable:'):
                return int(line.split()[1]) * 1024 / GB
    except (OSError, ValueError, IndexError):
        pass
    if is_windows():
        try:
            return _windows_ram_gb()
        except (OSError, AttributeError):
            return None
    return None


def read_vram():
    """[used_mb, total_mb] or None."""
    cmd = ['--query-gpu=memory.used,memory.total', '--format=csv,noheader,nounits']
    for exe in (['nvidia-smi.exe'] if is_windows() else ['nvidia-smi', 'nvidia-smi.exe']):
        try:
            out = subprocess.run([exe, *cmd], text=True, capture_output=True, timeout=10, **placement.NO_WINDOW)
            used, total = (float(x) for x in out.stdout.splitlines()[0].split(','))
            return [used, total] if total > 0 else None
        except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
            continue
    return None


def read_only_mount(path=None):
    """True when the filesystem holding path is mounted ro; None when unknown (always None off Linux)."""
    try:
        real = os.path.realpath(path or Path.home())
        best, opts = '', None
        for line in Path('/proc/self/mountinfo').read_text().splitlines():
            left, _, right = line.partition(' - ')
            fields = left.split()
            point = fields[4].replace('\\040', ' ')
            if (real == point or real.startswith(point.rstrip('/') + '/')) and len(point) >= len(best):
                rest = right.split()
                best, opts = point, fields[5] + ',' + (rest[2] if len(rest) > 2 else '')
        return None if opts is None else 'ro' in opts.split(',')
    except (OSError, IndexError):
        return None


def read_ollama_ram_gb():
    """Resident Ollama model RAM, Windows only (Ollama runs natively there); the decider url (DROVER_OLLAMA_URL or
    decider.url) overrides the local default."""
    if not is_windows():
        return 0.0
    url = os.environ.get('DROVER_OLLAMA_URL') or config_mod.get()['decider']['url'] or 'http://127.0.0.1:11434'
    try:
        from urllib.request import urlopen
        with urlopen(url.rstrip('/') + '/api/ps', timeout=3) as response:
            models = json.load(response).get('models', [])
        return sum(m.get('size', 0) - m.get('size_vram', 0) for m in models) / GB
    except (OSError, ValueError):
        return 0.0


def parse_wslconfig_memory_gb(text):
    """The `memory=` cap of [wsl2] in a .wslconfig, in GB, or None."""
    section = None
    for line in text.splitlines():
        line = line.split('#')[0].split(';')[0].strip()
        if line.startswith('['):
            section = line.strip('[]').lower()
        elif section == 'wsl2' and '=' in line:
            key, _, value = (x.strip() for x in line.partition('='))
            m = re.fullmatch(r'(\d+(?:\.\d+)?)\s*(gb|g|mb|m|tb|t)?', value.lower())
            if key.lower() == 'memory' and m:
                return float(m.group(1)) * {'gb': 1, 'g': 1, 'mb': 1 / 1024, 'm': 1 / 1024, 'tb': 1024, 't': 1024}[
                    m.group(2) or 'gb']
    return None


def wslconfig_path():
    if is_windows():
        return Path.home() / '.wslconfig'
    users = sorted(Path(placement.native(r'C:\Users')).glob('*/.wslconfig'))
    return users[0] if len(users) == 1 else None


def parse_meminfo_available_gb(text):
    for line in (text or '').splitlines():
        if line.startswith('MemAvailable:'):
            try:
                return int(line.split()[1]) * 1024 / GB
            except (ValueError, IndexError):
                return None
    return None


def read_wsl_ram_gb(distro, run=subprocess.run):
    """Available RAM inside WSL (where its workers run), seen from Windows; None when WSL is down or slow.
    Never starts the distro."""
    if not wsl_running(distro):
        return None
    try:
        out = run(placement.WSL(distro).command(['cat', '/proc/meminfo']), text=True, capture_output=True,
                  timeout=WSL_READ_TIMEOUT_S, **placement.NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_meminfo_available_gb(out.stdout)


def seat_sides(config=None):
    """Sides (windows/wsl) where enabled seats place their work."""
    cfg = config_mod.get() if config is None else config
    return {s.get('placement') for s in cfg.get('seats', [])}


def read_wsl(distro, windows, mem_wanted):
    """(wsl_up, {'used_gb','cap_gb'} or None). Memory is read only when a threshold asks for it and WSL is already up."""
    if windows:
        up = wsl_running(distro)
        text = None
        if up and mem_wanted:
            try:
                text = subprocess.run(placement.WSL(distro).command(['cat', '/proc/meminfo']), text=True,
                                      capture_output=True, timeout=20, **placement.NO_WINDOW).stdout
            except (OSError, subprocess.SubprocessError):
                text = None
    else:
        up = True
        try:
            text = Path('/proc/meminfo').read_text() if mem_wanted else None
        except OSError:
            text = None
    mem = None
    if text:
        info = {}
        for line in text.splitlines():
            k, _, rest = line.partition(':')
            if rest.split():
                info[k] = int(rest.split()[0]) / (1024 ** 2)
        if 'MemTotal' in info and 'MemAvailable' in info:
            cap = None
            path = wslconfig_path()
            try:
                cap = parse_wslconfig_memory_gb(path.read_text(errors='replace')) if path else None
            except OSError:
                pass
            mem = {'used_gb': info['MemTotal'] - info['MemAvailable'], 'cap_gb': cap or info['MemTotal']}
    return up, mem


def gather(fake=None, trackers=None, with_wsl_memory=False, config=None):
    """Facts from the machine, or from the fake when one is given."""
    if fake is None:
        raw = os.environ.get('DROVER_RESOURCES_FAKE')
        fake = json.loads(raw) if raw else None
    trackers = trackers if trackers is not None else trackers_of()
    names = [t['name'] for t in trackers if t['kind'] == 'disk']
    wsl_t = next((t for t in trackers if t['kind'] == 'wsl'), None)
    if fake is not None:
        windows = fake.get('platform', 'wsl') == 'windows'
        return {'platform': 'windows' if windows else 'wsl', 'load5': fake.get('load5'),
                'cores': fake.get('cores', 1), 'ram_available_gb': fake.get('ram_available_gb'),
                'ollama_ram_gb': fake.get('ollama_ram_gb', 0.0) if windows else 0.0,
                'disk_free_gb': {k: (fake.get('disk_free_gb') or {}).get(k) for k in names},
                'vram': fake.get('vram'), 'read_only': fake.get('read_only'),
                'wsl_up': fake.get('wsl_up', True), 'wsl_mem': fake.get('wsl_mem'),
                'wsl_ram_available_gb': fake.get('wsl_ram_available_gb'),
                'wsl_disk_free_gb': fake.get('wsl_disk_free_gb')}
    windows = is_windows()
    load = read_load()
    facts = {'platform': 'windows' if windows else 'wsl', 'load5': load[0] if load else None,
             'cores': load[1] if load else os.cpu_count() or 1, 'ram_available_gb': read_ram_gb(),
             'ollama_ram_gb': read_ollama_ram_gb(), 'disk_free_gb': {r['name']: r['free_gb'] for r in disk.read({'trackers': trackers})},
             'vram': read_vram(), 'read_only': None if windows else read_only_mount(),
             'wsl_up': True, 'wsl_mem': None, 'wsl_ram_available_gb': None, 'wsl_disk_free_gb': None}
    if windows and 'wsl' in seat_sides(config):
        facts['wsl_ram_available_gb'] = read_wsl_ram_gb(placement.distro(wsl_t['distro'] or None) if wsl_t else None)
    if wsl_t:
        distro = placement.distro(wsl_t['distro'] or None)
        facts['wsl_up'], facts['wsl_mem'] = read_wsl(
            distro, windows, with_wsl_memory or bool(wsl_t['queue_mem_pct'] or wsl_t['hold_mem_pct']))
    if 'wsl' in seat_sides(config) and any(t['kind'] == 'disk' and t.get('wsl_combined') for t in trackers_of(config)):
        # The one WSL free-space measurement per tick; disk.held and the CI/landing paths reuse it.
        facts['wsl_disk_free_gb'] = placement.wsl_free_gb(disk.tracker_distro({'trackers': trackers}), facts['wsl_up'])
    return facts


# ---- verdict ------------------------------------------------------------------------------------------------

def _wsl_state(tracker, facts):
    """The wsl tracker's own verdict: {verdict, reasons}. It never joins the global verdict."""
    if tracker is None:
        return {'verdict': 'go', 'reasons': []}
    if facts.get('wsl_up') is False:
        return {'verdict': 'queue', 'reasons': ['WSL is not running']}
    mem = facts.get('wsl_mem')
    if not mem or not mem.get('cap_gb'):
        return {'verdict': 'go', 'reasons': []}
    pct = 100.0 * mem['used_gb'] / mem['cap_gb']
    facts['wsl_mem_used_pct'] = round(pct, 1)
    if tracker['hold_mem_pct'] and pct > tracker['hold_mem_pct']:
        return {'verdict': 'hold', 'reasons': ['WSL memory %.0f%% of its %.1f GB cap > %g%%'
                                              % (pct, mem['cap_gb'], tracker['hold_mem_pct'])]}
    if tracker['queue_mem_pct'] and pct > tracker['queue_mem_pct']:
        return {'verdict': 'queue', 'reasons': ['WSL memory %.0f%% of its %.1f GB cap > %g%%'
                                               % (pct, mem['cap_gb'], tracker['queue_mem_pct'])]}
    return {'verdict': 'go', 'reasons': []}


def check(new_workers=1, fake=None, config=None, with_wsl_memory=False):
    trackers = trackers_of(config)
    by_kind = {}
    for t in trackers:
        by_kind.setdefault(t['kind'], []).append(t)
    facts = gather(fake, trackers, with_wsl_memory=with_wsl_memory, config=config)
    hold, queue = [], []
    disk_hold = False
    load5, cores = facts['load5'], max(facts['cores'] or 1, 1)
    load_t = (by_kind.get('load') or [None])[0]
    if load5 is not None and load_t:
        per_core = load5 / cores
        facts['load_per_core'] = round(per_core, 2)
        if per_core > load_t['hold_per_core']:
            hold.append('load %.2f per core > %g' % (per_core, load_t['hold_per_core']))
        elif per_core > load_t['queue_per_core']:
            queue.append('load %.2f per core > %g' % (per_core, load_t['queue_per_core']))
    ram_t = (by_kind.get('ram') or [{'queue_base_gb': 0.0, 'queue_per_worker_gb': 0.0, 'hold_gb': 0.0}])[0]
    # Memory is judged on the side(s) where seats place work. The next claim may land on any such side, so RAM
    # holds or queues only when every measured seat side is short; an unmeasured side is ignored.
    cfg_seats = seat_sides(config)
    own = facts['platform']
    readings = {own: facts['ram_available_gb']}
    if own == 'windows':
        readings['wsl'] = facts.get('wsl_ram_available_gb')
    sides = {side: gb for side, gb in readings.items() if gb is not None and side in cfg_seats}
    if not sides and readings[own] is not None:
        sides = {own: readings[own]}  # no seat side was measured: the hub's own side is the best evidence
    ollama = (facts['ollama_ram_gb'] or 0) if own == 'windows' else 0
    levels = []  # (rank, reason, slots) per side; rank 0 go, 1 queue, 2 hold
    bases = {}
    for side, gb in sides.items():
        base = ram_t['queue_base_gb'] + (ollama if side == 'windows' else 0)
        need = base + ram_t['queue_per_worker_gb'] * new_workers
        bases[side] = base
        tag = f'{side} ' if len(sides) > 1 else ''
        if side == own:
            facts['ram_queue_below_gb'] = round(need, 2)
        if gb < ram_t['hold_gb']:
            levels.append((2, '%sRAM available %.1f GB < %g GB' % (tag, gb, ram_t['hold_gb'])))
        elif gb < need:
            levels.append((1, '%sRAM available %.1f GB < %.1f GB needed for %d new worker(s)' % (tag, gb, need, new_workers)))
        else:
            levels.append((0, ''))
    side_admission = {side: dict(verdict=('go', 'queue', 'hold')[rank], reasons=[reason] if reason else [])
                 for side, (rank, reason) in zip(sides, levels)} if len(sides) > 1 and 'ram' in by_kind else None
    if sides and 'ram' in by_kind and all(rank for rank, _ in levels):
        floor = min(rank for rank, _ in levels)
        (hold if floor == 2 else queue).extend(reason for rank, reason in levels if rank == floor)
    # Disk is judged per seat side: WSL work counts the free space inside WSL too (disk.read), Windows work does not.
    seat_set = seat_sides(config) & {'windows', 'wsl'} or {own}
    disk_sides = {}
    for side in [s for s in ('windows', 'wsl') if s in seat_set]:
        rows = [row for t in by_kind.get('disk', [])
                for row in disk.read({'trackers': [t]}, reader=lambda path, t=t: facts['disk_free_gb'].get(t['name']),
                                     side=side, wsl_free=facts.get('wsl_disk_free_gb'))]
        disk_sides[side] = dict(disk.admission(rows), rows=rows)
    # disks: the first seat side's view (windows when it is a seat); disks_by_side labels each side's own reading.
    facts['disks'] = next(iter(disk_sides.values()))['rows']
    facts['disks_by_side'] = {side: d['rows'] for side, d in disk_sides.items()}
    ranks = {'go': 0, 'queue': 1, 'hold': 2}
    floor = min(ranks[d['verdict']] for d in disk_sides.values())   # blocked only when every seat side is short
    disk_hold = any(d['verdict'] == 'hold' for d in disk_sides.values())
    for d in disk_sides.values():
        if floor and ranks[d['verdict']] == floor:
            (hold if floor == 2 else queue).extend(r for r in d['reasons'] if r not in hold + queue)
    if len(disk_sides) > 1:
        side_admission = side_admission or {}
        for side, d in disk_sides.items():
            r = side_admission.get(side, {'verdict': 'go', 'reasons': []})
            side_admission[side] = dict(verdict=max(r['verdict'], d['verdict'], key=ranks.get),
                                   reasons=r['reasons'] + d['reasons'])
    if facts['read_only']:
        hold.append('filesystem is read-only')
    vram_t = (by_kind.get('vram') or [None])[0]
    vram = facts['vram']
    if vram_t and vram and vram[1] > 0:
        pct = 100.0 * vram[0] / vram[1]
        if vram_t['hold_pct'] and pct > vram_t['hold_pct']:
            hold.append('GPU VRAM %.0f%% > %g%%' % (pct, vram_t['hold_pct']))
        elif vram_t['queue_pct'] and pct > vram_t['queue_pct']:
            queue.append('GPU VRAM %.0f%% > %g%%' % (pct, vram_t['queue_pct']))
    verdict = 'hold' if hold else 'queue' if queue else 'go'
    result = {'verdict': verdict, 'reasons': hold + queue, 'facts': facts, 'slots': 0,
              'wsl': _wsl_state((by_kind.get('wsl') or [None])[0], facts)}
    if side_admission:
        result['side_admission'] = side_admission  # the dispatcher claims only for seats whose side is go
    if vram_t and vram and vram[1] > 0 and 100.0 * vram[0] / vram[1] > vram_t['fallback_pct']:
        device = (config or {}).get('decider', {}).get('device') or config_mod.get()['decider']['device']
        result['decider'] = {'auto': 'decider-cpu', 'cpu': None}.get(device, 'decider-fallback')
        if result['decider']:
            result['reasons'].append('GPU VRAM %.0f%% > %g%%: decider %s' % (
                100.0 * vram[0] / vram[1], vram_t['fallback_pct'],
                'runs on the CPU' if device == 'auto' else 'falls back to rules'))
        else:
            del result['decider']
    if disk_hold:
        result['cleanup_hint'] = 'drover disk-cleanup'
    if verdict == 'go':
        per = ram_t['queue_per_worker_gb']
        fits = [max(0, math.floor((gb - bases[side]) / per + 1e-9)) for side, gb in sides.items()
                if math.isfinite(gb)] if per else []
        result['slots'] = max(fits) if fits else 1
    return result


def cleanup_pressure(config=None, fake=None):
    """Load and RAM verdict (disk excluded) for deciding whether disk cleanup may start or keep running."""
    cfg = config_mod.get() if config is None else config
    trackers = [t for t in trackers_of(cfg) if t['kind'] in ('load', 'ram')]
    return check(1, fake, dict(cfg, trackers=trackers))


def tick(new_workers=1, fake=None, config=None, with_wsl_memory=False):
    """One disk observation feeds resource admission and the independent tracker."""
    cfg = config_mod.get() if config is None else config
    tracked = disk.tick(cfg, gate=lambda: cleanup_pressure(cfg))
    facts = gather(fake, [t for t in trackers_of(cfg) if t['kind'] != 'disk'], with_wsl_memory, cfg)
    facts['disk_free_gb'] = {row['name']: row['free_gb'] for row in tracked['drives']}
    result = check(new_workers, facts, cfg, with_wsl_memory)
    result.update(pause=tracked['pause'], critical_tokens=tracked['critical_tokens'])
    return result


def render(result):
    lines = ['%s (room for %d more worker(s))' % (result['verdict'].upper(), result['slots'])]
    lines += ['  - ' + r for r in result['reasons']]
    if 'cleanup_hint' in result:
        lines.append('  hint: ' + result['cleanup_hint'])
    if result.get('decider'):
        lines.append('  decider: ' + result['decider'])
    f = result['facts']
    lines.append('  facts: ' + json.dumps(f, sort_keys=True))
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='drover.resources')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--new', type=int, default=1, help='new workers to admit')
    args = parser.parse_args(argv)
    result = check(new_workers=args.new)
    print(json.dumps(result, indent=2) if args.json else render(result))
    return EXIT[result['verdict']]


if __name__ == '__main__':
    sys.exit(main())
