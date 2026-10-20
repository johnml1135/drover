"""Resource checks for admission, driven by the config's [[trackers]]: load, RAM, disks, GPU VRAM and WSL.

check() returns {verdict: go|queue|hold, reasons, facts, slots, wsl: {verdict, reasons}}. Unknown facts never block.
The wsl tracker never feeds the global verdict (a Windows task must not wait for WSL): the dispatcher consults
result['wsl'] only for work that needs WSL. Runs on WSL (full) and on Windows (no /proc: load is unknown, RAM via
GlobalMemoryStatusEx). The old resources.toml ([load] [ram] [disk] [gpu]) is still read from the state dir and
overrides the matching trackers; a config dict in that old shape is accepted by check() and load_config().
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
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from . import config as config_mod

GB = 1024 ** 3
EXIT = {'go': 0, 'queue': 4, 'hold': 5}
LXSS = r'HKCU\Software\Microsoft\Windows\CurrentVersion\Lxss'
REG = placement.native(r'C:\Windows\System32\reg.exe')
VHDX_WARNING = 'cannot locate VHDX host drive; set DROVER_VHDX_DIR'
LEGACY_FILE = 'resources.toml'
LEGACY_DISKS = {'h': 'h', 'wsl': 'wsl', 'c': 'c'}


def trackers_of(config=None):
    """The tracker list for a config: a full config, a legacy-shaped dict ([load] [ram] [disk] [gpu]) or the process
    config with the state dir's resources.toml laid over it."""
    if config is not None and 'trackers' in config:
        return [t for t in config['trackers'] if t.get('enabled', True)]
    base = [dict(t) for t in config_mod.get()['trackers']]
    legacy = config
    if legacy is None:
        path = config_mod.state_path(LEGACY_FILE)
        legacy = _read_legacy(path) if path.exists() else {}
    return [t for t in _apply_legacy(base, legacy) if t.get('enabled', True)]


def _apply_legacy(trackers, legacy):
    by_name = {t['name']: t for t in trackers}
    for kind, values in legacy.items():
        values = values or {}
        if kind in ('load', 'ram') and kind in by_name:
            by_name[kind].update({k: v for k, v in values.items() if k in by_name[kind]})
        elif kind == 'disk':
            for key, value in values.items():
                name, _, rest = key.partition('_')
                field = {'queue_gb': 'queue_gb', 'hold_gb': 'hold_gb'}.get(rest)
                if name in by_name and field:
                    by_name[name][field] = float(value)
        elif kind == 'gpu' and 'vram' in by_name and 'vram_fallback_pct' in values:
            by_name['vram']['fallback_pct'] = float(values['vram_fallback_pct'])
    return trackers


def _read_legacy(path):
    with open(path, 'rb') as handle:
        return tomllib.load(handle)


def load_config(path=None):
    """The thresholds in the old resources.toml shape, from the config's trackers (and the legacy file, if any)."""
    trackers = {t['name']: t for t in trackers_of()}
    out = {'load': {k: trackers['load'][k] for k in ('queue_per_core', 'hold_per_core')},
           'ram': {k: trackers['ram'][k] for k in ('queue_base_gb', 'queue_per_worker_gb', 'hold_gb')},
           'disk': {}, 'gpu': {'vram_fallback_pct': trackers['vram']['fallback_pct']},
           'paths': {'windows_drives': ['C', 'H']}}
    for name in LEGACY_DISKS:
        if name in trackers:
            out['disk'][name + '_queue_gb'] = trackers[name]['queue_gb']
            out['disk'][name + '_hold_gb'] = trackers[name]['hold_gb']
    if path:
        for section, values in _read_legacy(Path(path)).items():
            out.setdefault(section, {}).update(values)
    return out


def is_windows():
    return os.name == 'nt'


# ---- readers (each returns None when unknown) ---------------------------------------------------------------

def read_load():
    """(5-minute load, cores) or None."""
    try:
        load5 = float(Path('/proc/loadavg').read_text().split()[1])
    except (OSError, ValueError, IndexError):
        try:
            load5 = os.getloadavg()[1]
        except (OSError, AttributeError):
            return None
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


def _free_gb(path):
    try:
        return shutil.disk_usage(path).free / GB
    except OSError:
        return None


def vhdx_dir():
    """WSL path of the directory holding this distro's ext4.vhdx, or None."""
    override = os.environ.get('DROVER_VHDX_DIR')
    if override:
        return override
    distro = os.environ.get('WSL_DISTRO_NAME')
    if not distro or not os.path.exists(REG):
        return None
    try:
        out = subprocess.run([REG, 'query', LXSS, '/s'], text=True, capture_output=True, timeout=10, **placement.NO_WINDOW).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    name = base = None
    for line in out.replace('\r', '').splitlines():
        if line.startswith('HKEY_'):
            name = base = None
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0] == 'DistributionName':
            name = parts[2]
        elif len(parts) == 3 and parts[0] == 'BasePath':
            base = parts[2]
        if name == distro and base:
            return placement.to_wsl_path(base) if placement.drive(base) else None
    return None


def disk_path(tracker, windows):
    """Where to measure a disk tracker: a path, or None when this side cannot see it."""
    if windows:
        return config_mod.native(tracker['path']) if tracker['wsl_source'] != 'home' and tracker['path'] else None
    if tracker['wsl_source'] == 'vhdx':
        host = vhdx_dir()
        return host if host and os.path.exists(host) else None
    if tracker['wsl_source'] == 'home':
        return str(Path.home())
    return config_mod.native(tracker['path']) if tracker['path'] else None


def read_disks(windows, trackers=None):
    """{tracker name: free GB} for each disk tracker; None where unknown. The default 'h' is the VHDX host drive on WSL."""
    trackers = [t for t in (trackers if trackers is not None else trackers_of()) if t['kind'] == 'disk']
    out = {}
    for t in trackers:
        path = disk_path(t, windows)
        out[t['name']] = _free_gb(path) if path else None
    return out


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


def wsl_running(default, run=subprocess.run):
    """True when the distro is up right now. `wsl -l --running` does not start anything, unlike `wsl --exec`."""
    try:
        r = run(placement.wsl_control(["-l", "--running", "-q"]), capture_output=True, timeout=20, **placement.NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return False
    names = placement.decode_output(r.stdout).splitlines()
    return r.returncode == 0 and placement.distro(default).lower() in [x.strip().lower() for x in names]


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


def gather(fake=None, trackers=None, with_wsl_memory=False):
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
                'wsl_up': fake.get('wsl_up', True), 'wsl_mem': fake.get('wsl_mem')}
    windows = is_windows()
    load = read_load()
    facts = {'platform': 'windows' if windows else 'wsl', 'load5': load[0] if load else None,
             'cores': load[1] if load else os.cpu_count() or 1, 'ram_available_gb': read_ram_gb(),
             'ollama_ram_gb': read_ollama_ram_gb(), 'disk_free_gb': read_disks(windows, trackers),
             'vram': read_vram(), 'read_only': None if windows else read_only_mount(),
             'wsl_up': True, 'wsl_mem': None}
    if wsl_t:
        distro = placement.distro(wsl_t['distro'] or None)
        facts['wsl_up'], facts['wsl_mem'] = read_wsl(
            distro, windows, with_wsl_memory or bool(wsl_t['queue_mem_pct'] or wsl_t['hold_mem_pct']))
    return facts


# ---- verdict ------------------------------------------------------------------------------------------------

def _disk_label(tracker, platform):
    if tracker['wsl_source'] == 'home':
        return 'WSL ext4'
    drive = tracker['path'].rstrip('\\/') or tracker['name']
    return f'{drive} (VHDX host)' if tracker['wsl_source'] == 'vhdx' and platform == 'wsl' else drive


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
    facts = gather(fake, trackers, with_wsl_memory=with_wsl_memory)
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
    ram = facts['ram_available_gb']
    ram_t = (by_kind.get('ram') or [{'queue_base_gb': 0.0, 'queue_per_worker_gb': 0.0, 'hold_gb': 0.0}])[0]
    base = ram_t['queue_base_gb'] + (facts['ollama_ram_gb'] or 0)
    need = base + ram_t['queue_per_worker_gb'] * new_workers
    if ram is not None and 'ram' in by_kind:
        facts['ram_queue_below_gb'] = round(need, 2)
        if ram < ram_t['hold_gb']:
            hold.append('RAM available %.1f GB < %g GB' % (ram, ram_t['hold_gb']))
        elif ram < need:
            queue.append('RAM available %.1f GB < %.1f GB needed for %d new worker(s)' % (ram, need, new_workers))
    for t in by_kind.get('disk', []):
        free = facts['disk_free_gb'].get(t['name'])
        if free is None:
            continue
        label = _disk_label(t, facts['platform'])
        if free < t['hold_gb']:
            hold.append('%s free %.1f GB < %g GB' % (label, free, t['hold_gb']))
            disk_hold = True
        elif free < t['queue_gb']:
            queue.append('%s free %.1f GB < %g GB' % (label, free, t['queue_gb']))
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
    vhdx_t = [t for t in by_kind.get('disk', []) if t['wsl_source'] == 'vhdx']
    if facts['platform'] == 'wsl' and vhdx_t and facts['disk_free_gb'].get(vhdx_t[0]['name']) is None:
        result['warnings'] = [VHDX_WARNING]
        result['reasons'].append(VHDX_WARNING)
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
        result['cleanup_hint'] = 'run garden'
    if verdict == 'go':
        result['slots'] = 1 if ram is None or not math.isfinite(ram) else max(
            0, math.floor((ram - base) / ram_t['queue_per_worker_gb'] + 1e-9)) if ram_t['queue_per_worker_gb'] else 1
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
