import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from drover import config_defaults, process, resources, wsl_cache_reclaim as rc
from tests.fakeci import FakeCommands, answering, process_options, refuse_real_spawns

KB_PER_GB = 1024 * 1024


def meminfo(cached_gb=18, buffers_gb=0, sreclaimable_gb=0, shmem_gb=0):
    return ''.join('%s: %d kB\n' % (k, int(v * KB_PER_GB)) for k, v in
                   (('MemTotal', 32), ('MemAvailable', 5), ('Buffers', buffers_gb), ('Cached', cached_gb),
                    ('SReclaimable', sreclaimable_gb), ('Shmem', shmem_gb)))


class Fakes:
    def __init__(self, info=None, drop=None):
        self.calls, self.info, self.drop, self.now = [], info or meminfo(), drop, 1000.0
        self.state = {}

    def run(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if 'drop_caches' in ' '.join(cmd):
            if isinstance(self.drop, Exception):
                raise self.drop
            return SimpleNamespace(returncode=self.drop if self.drop is not None else 0, stdout='', stderr='')
        return SimpleNamespace(returncode=0, stdout=self.info, stderr='')

    def drops(self):
        return [c for c, _ in self.calls if 'drop_caches' in ' '.join(c)]

    def spawns(self):
        """`process.subprocess` answering through `self.run`, whatever it is when the command runs."""
        return mock.patch.object(process, 'subprocess', answering(lambda cmd, **kw: self.run(cmd, **kw)))

    def go(self, ram=2.8, running=True, cfg=None, platform='windows', queue_below=6.5):
        facts = {'platform': platform, 'ram_available_gb': ram, 'ram_queue_below_gb': queue_below}
        with self.spawns():
            return rc.reclaim(cfg or globals()['cfg'](), facts, clock=lambda: self.now, running=lambda d: running,
                              state=self.state)


def cfg(**over):
    ram = dict(next(t for t in config_defaults.DEFAULT_TRACKERS if t['kind'] == 'ram'), **over)
    return {'trackers': [ram]}



class Reclaim(unittest.TestCase):
    def test_drops_when_low_and_cache_large(self):
        f = Fakes()
        self.assertTrue(f.go())
        self.assertEqual(len(f.drops()), 1)

    def test_exact_wsl_arguments(self):
        drop = ['wsl.exe', '-d', 'Ubuntu', '-u', 'root', '--exec', 'sh', '-c', 'sync; echo 1 > /proc/sys/vm/drop_caches']
        self.assertNotIn('3', ' '.join(drop).replace('drop_caches', ''))
        with tempfile.TemporaryDirectory() as root:
            fake = FakeCommands(root)
            fake.expect(['wsl.exe', '-d', 'Ubuntu', '--exec', 'cat', '/proc/meminfo'], output=meminfo(),
                        **process_options(rc.READ_TIMEOUT_S))
            fake.expect(drop, **process_options(rc.DROP_TIMEOUT_S))
            with refuse_real_spawns(), fake.patch(process):
                self.assertTrue(rc.reclaim(cfg(), {'platform': 'windows', 'ram_available_gb': 2.8},
                                           clock=lambda: 0, running=lambda d: True, state={}))
        self.assertLessEqual(max(rc.READ_TIMEOUT_S, rc.DROP_TIMEOUT_S), 15)

    def test_not_when_ram_is_fine(self):
        f = Fakes()
        self.assertFalse(f.go(ram=12.0))
        self.assertEqual(f.calls, [])

    def test_at_the_level_counts_as_low(self):
        f = Fakes()
        self.assertTrue(f.go(ram=6.5))

    def test_hold_level_without_queue_fact(self):
        f = Fakes()
        facts = {'platform': 'windows', 'ram_available_gb': 3.0}
        with f.spawns():
            self.assertTrue(rc.reclaim(cfg(), facts, clock=lambda: 0, running=lambda d: True, state={}))

    def test_not_when_cache_under_minimum(self):
        f = Fakes(meminfo(cached_gb=10, shmem_gb=3))   # 7 GB reclaimable
        self.assertFalse(f.go())
        self.assertEqual(f.drops(), [])

    def test_cache_formula(self):
        self.assertAlmostEqual(rc.parse_meminfo_cache_gb(meminfo(cached_gb=6, buffers_gb=1, sreclaimable_gb=3, shmem_gb=2)), 8.0)
        self.assertIsNone(rc.parse_meminfo_cache_gb('garbage'))

    def test_not_when_wsl_down(self):
        f = Fakes()
        self.assertFalse(f.go(running=False))
        self.assertEqual(f.calls, [])

    def test_not_off_windows_hub(self):
        f = Fakes()
        self.assertFalse(f.go(platform='wsl'))
        self.assertEqual(f.calls, [])

    def test_rate_limited_across_ticks(self):
        f = Fakes()
        self.assertTrue(f.go())
        f.now += 599
        self.assertFalse(f.go())
        f.now += 2
        self.assertTrue(f.go())
        self.assertEqual(len(f.drops()), 2)

    def test_failed_drop_does_not_raise_or_retry_in_window(self):
        for bad in (subprocess.TimeoutExpired('wsl.exe', 10), OSError('gone'), 1):
            f = Fakes(drop=bad)
            with self.assertLogs('drover.wsl_cache_reclaim', 'WARNING'):
                self.assertFalse(f.go())
            f.now += 60
            self.assertFalse(f.go())
            self.assertEqual(len(f.drops()), 1, bad)

    def test_read_failure_never_raises(self):
        f = Fakes()
        f.run = mock.Mock(side_effect=subprocess.TimeoutExpired('wsl.exe', 5))
        with self.assertLogs('drover.wsl_cache_reclaim', 'WARNING'):
            self.assertFalse(f.go())

    def test_compaction_refusal_is_a_skip(self):
        f = Fakes()
        f.run = mock.Mock(side_effect=AssertionError('must not run'))
        with mock.patch.object(process, 'wsl_argv', side_effect=OSError('WSL is down for compaction')):
            with self.assertLogs('drover.wsl_cache_reclaim', 'WARNING'):
                self.assertFalse(f.go())

    def test_disabled_by_config(self):
        f = Fakes()
        self.assertFalse(f.go(cfg=cfg(wsl_cache_reclaim=False)))
        self.assertEqual(f.calls, [])

    def test_custom_minimum_and_interval(self):
        f = Fakes(meminfo(cached_gb=4))
        self.assertTrue(f.go(cfg=cfg(wsl_cache_min_gb=3.0, wsl_cache_interval_s=30.0)))
        f.now += 31
        self.assertTrue(f.go(cfg=cfg(wsl_cache_min_gb=3.0, wsl_cache_interval_s=30.0)))

    def test_defaults(self):
        ram = next(t for t in config_defaults.DEFAULT_TRACKERS if t['kind'] == 'ram')
        self.assertEqual((ram['wsl_cache_reclaim'], ram['wsl_cache_min_gb'], ram['wsl_cache_interval_s']), (True, 8.0, 600.0))


class TickIntegration(unittest.TestCase):
    def test_tick_verdict_unchanged_and_errors_swallowed(self):
        facts = {'platform': 'windows', 'ram_available_gb': 2.8}
        with mock.patch.object(rc, 'reclaim', side_effect=RuntimeError('boom')):
            self.assertIsNone(resources.reclaim_wsl_cache(cfg(), facts))

    def test_check_does_not_reclaim(self):
        with mock.patch.object(rc, 'reclaim') as r:
            resources.check(fake={'platform': 'windows', 'ram_available_gb': 2.8, 'load5': 0, 'cores': 4,
                                  'disk_free_gb': {'h': 200, 'c': 200}}, config=cfg())
        r.assert_not_called()


if __name__ == '__main__':
    unittest.main()
