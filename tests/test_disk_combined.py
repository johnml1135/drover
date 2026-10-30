"""WSL-side work counts free space inside the distro's VHDX on top of the host drive; Windows work does not."""
import unittest
from types import SimpleNamespace
from unittest import mock

from drover import config, disk, placement, placement_host, resources


def cfg(**over):
    tracker = {'kind': 'disk', 'name': 'h', 'path': 'H:/', 'warn_gb': 150, 'queue_gb': 100, 'hold_gb': 75,
               'critical_gb': 25, 'wsl_combined': True, **over}
    return config.validate({'trackers': [tracker]})


def check(host, wsl, **over):
    fake = {'platform': 'windows', 'load5': 1.0, 'cores': 8, 'ram_available_gb': 30, 'wsl_ram_available_gb': 30,
            'disk_free_gb': {'h': host, 'c': 500}, 'wsl_disk_free_gb': wsl, 'vram': None}
    return resources.check(fake=fake, config=cfg(**over))


class Combined(unittest.TestCase):
    def test_wsl_work_admitted_at_h47_plus_wsl364_windows_work_held(self):
        r = check(47, 364)
        sides = r['side_admission']
        self.assertEqual((sides['wsl']['verdict'], sides['windows']['verdict']), ('go', 'hold'))
        self.assertEqual(r['verdict'], 'go')   # blocked only when every seat side is short
        text = r['facts']['disks_by_side']['wsl'][0]['detail']
        self.assertEqual(text, 'H: 47 GB + WSL 364 GB = 411 GB for WSL work; floor 10 GB')
        self.assertIn('h: disk hold (47.0 GB free)', sides['windows']['reasons'])

    def test_host_floor_holds_wsl_work_whatever_wsl_has(self):
        r = check(9, 900)
        self.assertEqual(r['side_admission']['wsl']['verdict'], 'hold')
        self.assertEqual(r['verdict'], 'hold')
        self.assertTrue(any('floor 10 GB' in reason for reason in r['reasons']))
        self.assertEqual(check(10, 900)['side_admission']['wsl']['verdict'], 'go')

    def test_unmeasurable_wsl_falls_back_to_host_alone(self):
        r = check(47, None)
        self.assertEqual(r['side_admission']['wsl']['verdict'], 'hold')
        self.assertEqual(r['verdict'], 'hold')
        self.assertNotIn('detail', r['facts']['disks_by_side']['wsl'][0])

    def test_not_combined_tracker_is_host_alone_everywhere(self):
        r = check(47, 364, wsl_combined=False)
        self.assertEqual(r['verdict'], 'hold')
        self.assertNotIn('detail', r['facts']['disks_by_side']['wsl'][0])

    def test_read_and_validation(self):
        tracker = next(t for t in cfg()['trackers'] if t['name'] == 'h')
        self.assertEqual(tracker['host_floor_gb'], 10.0)
        rows = disk.read({'trackers': [tracker]}, reader=lambda p: 47, side='wsl', wsl_free=364)
        self.assertEqual((rows[0]['level'], rows[0]['free_gb']), ('ok', 411))
        self.assertEqual(disk.read({'trackers': [tracker]}, reader=lambda p: 47)[0]['level'], 'hold')
        with self.assertRaises(config.ConfigError):
            cfg(host_floor_gb=-1)
        with self.assertRaises(config.ConfigError):
            cfg(wsl_combined='yes')

    def test_floor_applies_even_when_wsl_is_unmeasured_and_h_unreadable(self):
        tracker = next(t for t in cfg(hold_gb=5, critical_gb=2, queue_gb=6, warn_gb=7, host_floor_gb=20)['trackers']
                       if t['name'] == 'h')
        rows = disk.read({'trackers': [tracker]}, reader=lambda p: 15, side='wsl', wsl_free=None)
        self.assertEqual(rows[0]['level'], 'hold')
        rows = disk.read({'trackers': [tracker]}, reader=lambda p: None, side='wsl', wsl_free=300)
        self.assertEqual(rows[0]['level'], 'hold')
        self.assertEqual(disk.read({'trackers': [tracker]}, reader=lambda p: 15, side='windows')[0]['level'], 'ok')

    def tick(self, host):
        import tempfile
        from pathlib import Path
        config_ = cfg()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(config, 'state_dir', return_value=Path(tmp)), \
                mock.patch('drover.disk_cleanup.retry_notices'):
            return disk.tick(config_, reader=lambda p: host, enqueue=lambda: None)

    def test_sticky_pause_is_per_side(self):
        self.assertFalse(self.tick(15)['pause'])      # H below critical 25 but above the floor: WSL work runs
        self.assertEqual(self.tick(15)['critical_tokens'], [])
        self.assertTrue(self.tick(8)['pause'])        # below the floor too: every side paused

    def test_wsl_free_measurement(self):
        ok = SimpleNamespace(returncode=0, stdout='   Avail\n391000000000\n')
        win = mock.patch.object(placement_host, 'IS_WINDOWS', True)
        placement._WSL_FREE.clear()
        calls = []
        def run(*a, **k):
            calls.append(k['timeout'])
            return ok
        with win:
            self.assertAlmostEqual(placement.wsl_free_gb('U', True, run=run), 391e9 / 1024 ** 3)
            placement.wsl_free_gb('U', True, run=run)            # shared for a minute: no second spawn
            self.assertEqual(calls, [5])
            self.assertAlmostEqual(placement.wsl_free_known('U'), 391e9 / 1024 ** 3)
            self.assertIsNone(placement.wsl_free_known('other'))   # held() never measures
            bad = SimpleNamespace(returncode=1, stdout='')
            self.assertIsNone(placement.wsl_free_gb('V', True, run=lambda *a, **k: bad, ttl=0))
            self.assertIsNone(placement.wsl_free_gb('V', True, run=mock.Mock(side_effect=OSError), ttl=0))
            self.assertIsNone(placement.wsl_free_gb('W', False, run=run, ttl=0))   # distro down: not asked
        with mock.patch.object(placement_host, 'IS_WINDOWS', False), \
                mock.patch.object(placement_host.os, 'statvfs', create=True,
                                  return_value=SimpleNamespace(f_bavail=10, f_frsize=1024 ** 3)):
            self.assertEqual(placement.wsl_free_gb('X', ttl=0), 10)
        placement._WSL_FREE.clear()
