"""Drop WSL's page cache (echo 1 only) when Windows RAM is short and WSL is already running.

Called from resources.tick after the verdict is computed, so admission is never told memory it does not yet have.
It never starts WSL, never touches a process and never raises: every failure is one warning and a skip."""
import logging
import time

from . import placement, process
from .placement_host import wsl_running

log = logging.getLogger(__name__)
KB = 1024
GB = 1024 ** 3
READ_TIMEOUT_S = 5
DROP_TIMEOUT_S = 10
DROP_SCRIPT = 'sync; echo 1 > /proc/sys/vm/drop_caches'   # page cache only; never 3
_STATE = {}   # 'last': clock time of the latest drop attempt, kept whether or not it worked


def parse_meminfo_cache_gb(text):
    """buff/cache as free(1) shows it: Cached + Buffers + SReclaimable - Shmem, in GB; None when unreadable."""
    kb = {}
    for line in (text or '').splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].rstrip(':') in ('Cached', 'Buffers', 'SReclaimable', 'Shmem'):
            try:
                kb[parts[0].rstrip(':')] = int(parts[1])
            except ValueError:
                return None
    if 'Cached' not in kb:
        return None
    return (kb['Cached'] + kb.get('Buffers', 0) + kb.get('SReclaimable', 0) - kb.get('Shmem', 0)) * KB / GB


def _ended(args, timeout):
    """The ended run of a WSL command. One that did not end raises `OSError`, which `reclaim` logs and skips, as it
    did a timeout or a failed spawn."""
    done = process.run(process.Command(placement.wsl_control(args), timeout))
    if done.kind != 'exited':
        raise OSError(done.detail)
    return done


def _low_level(ram_t, facts):
    level = facts.get('ram_queue_below_gb')
    return max(ram_t.get('hold_gb', 0.0), level if level is not None else 0.0)


def reclaim(config, facts, clock=time.monotonic, running=wsl_running, state=_STATE):
    """True when a drop was issued and succeeded. Any problem is logged and skipped."""
    try:
        return _reclaim(config, facts, clock, running, state)
    except Exception as e:   # includes WslOffline from the compaction hold
        log.warning('WSL cache reclaim skipped: %s', e)
        return False


def _reclaim(config, facts, clock, running, state):
    trackers = config.get('trackers', [])
    ram_t = next((t for t in trackers if t.get('kind') == 'ram' and t.get('enabled', True)), None)
    if ram_t is None or not ram_t.get('wsl_cache_reclaim', True) or facts.get('platform') != 'windows':
        return False
    gb = facts.get('ram_available_gb')
    if gb is None or gb > _low_level(ram_t, facts):
        return False
    last = state.get('last')
    if last is not None and clock() - last < ram_t.get('wsl_cache_interval_s', 600.0):
        return False
    wsl_t = next((t for t in trackers if t.get('kind') == 'wsl'), None)
    distro = placement.distro((wsl_t or {}).get('distro') or None)
    if not running(distro):
        return False
    cache = parse_meminfo_cache_gb(_ended(['-d', distro, '--exec', 'cat', '/proc/meminfo'], READ_TIMEOUT_S).stdout)
    if cache is None or cache < ram_t.get('wsl_cache_min_gb', 8.0):
        return False
    state['last'] = clock()   # before the attempt, so a failure or timeout is not retried inside the interval
    r = _ended(['-d', distro, '-u', 'root', '--exec', 'sh', '-c', DROP_SCRIPT], DROP_TIMEOUT_S)
    if r.code != 0:
        log.warning('WSL cache drop failed with exit %s', r.code)
        return False
    log.info('dropped %.1f GB of WSL page cache; Windows had %.1f GB available', cache, gb)
    return True
