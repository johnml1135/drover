from drover import placement
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock

from drover import resources, disk, config


def wsl(**over):
    fake = {'platform': 'wsl', 'load5': 4.0, 'cores': 4, 'ram_available_gb': 20, 'ollama_ram_gb': 0,
            'disk_free_gb': {'h': 200, 'wsl': 200, 'c': 200}, 'vram': [1000, 24000], 'read_only': False}
    fake.update(over)
    return fake


def disks(**over):
    d = {'h': 200, 'wsl': 200, 'c': 200}
    d.update(over)
    return d


def verdict(fake, n=1):
    return resources.check(new_workers=n, fake=fake)


class Load(unittest.TestCase):
    def test_edges(self):
        self.assertEqual(verdict(wsl(load5=10.0))['verdict'], 'go')  # 2.5/core exactly
        self.assertEqual(verdict(wsl(load5=10.04))['verdict'], 'queue')
        self.assertEqual(verdict(wsl(load5=14.0))['verdict'], 'queue')  # 3.5/core exactly
        self.assertEqual(verdict(wsl(load5=14.04))['verdict'], 'hold')


class Ram(unittest.TestCase):
    def test_queue_edge_scales_with_workers(self):
        self.assertEqual(verdict(wsl(ram_available_gb=6.5), 1)['verdict'], 'go')
        self.assertEqual(verdict(wsl(ram_available_gb=6.49), 1)['verdict'], 'queue')
        self.assertEqual(verdict(wsl(ram_available_gb=7.0), 2)['verdict'], 'go')
        self.assertEqual(verdict(wsl(ram_available_gb=6.9), 2)['verdict'], 'queue')

    def test_ollama_ram_raises_the_bar_on_windows(self):
        def win(ram):
            return {'platform': 'windows', 'ram_available_gb': ram, 'ollama_ram_gb': 4}
        self.assertEqual(verdict(win(10))['verdict'], 'queue')
        self.assertEqual(verdict(win(10.5))['verdict'], 'go')
        self.assertEqual(verdict(win(10))['facts']['ram_queue_below_gb'], 10.5)

    def test_ollama_ram_ignored_on_wsl(self):
        r = verdict(wsl(ram_available_gb=6.5, ollama_ram_gb=4))
        self.assertEqual((r['verdict'], r['facts']['ollama_ram_gb']), ('go', 0.0))

    def test_hold_edge(self):
        self.assertEqual(verdict(wsl(ram_available_gb=4.0))['verdict'], 'queue')
        self.assertEqual(verdict(wsl(ram_available_gb=3.99))['verdict'], 'hold')

    def test_slots(self):
        self.assertEqual(verdict(wsl(ram_available_gb=8.0))['slots'], 4)
        self.assertEqual(verdict(wsl(ram_available_gb=6.5))['slots'], 1)
        self.assertEqual(verdict(wsl(ram_available_gb=6.0))['slots'], 0)
        self.assertEqual(verdict({'platform': 'windows', 'ram_available_gb': 10, 'ollama_ram_gb': 4})['slots'], 0)


class Disk(unittest.TestCase):
    def test_edges_each_drive(self):
        for key, queue, hold in (('h', 100, 50), ('c', 40, 25)):
            with self.subTest(key):
                self.assertEqual(verdict(wsl(disk_free_gb=disks(**{key: queue})))['verdict'], 'go')
                r = verdict(wsl(disk_free_gb=disks(**{key: queue - 0.1})))
                self.assertEqual(r['verdict'], 'queue')
                self.assertNotIn('cleanup_hint', r)
                self.assertEqual(verdict(wsl(disk_free_gb=disks(**{key: hold})))['verdict'], 'queue')
                r = verdict(wsl(disk_free_gb=disks(**{key: hold - 0.1})))
                self.assertEqual(r['verdict'], 'hold')
                self.assertEqual(r['cleanup_hint'], 'drover disk-cleanup')

    def test_unknown_never_blocks(self):
        r = verdict(wsl(disk_free_gb={'h': None, 'wsl': 200, 'c': None}))
        self.assertEqual(r['verdict'], 'go')
        r = verdict(wsl(disk_free_gb=None, ram_available_gb=None, load5=None))
        self.assertEqual(r['verdict'], 'go')

    def test_non_disk_hold_has_no_hint(self):
        self.assertNotIn('cleanup_hint', verdict(wsl(ram_available_gb=1)))

    def test_read_only_holds(self):
        self.assertEqual(verdict(wsl(read_only=True))['verdict'], 'hold')


class Gpu(unittest.TestCase):
    def test_vram_is_a_fallback_not_a_hold(self):
        r = verdict(wsl(vram=[19200, 24000]))
        self.assertEqual((r['verdict'], r.get('decider')), ('go', None))
        r = verdict(wsl(vram=[19201, 24000]))
        self.assertEqual((r['verdict'], r['decider']), ('go', 'decider-fallback'))
        self.assertEqual(verdict(wsl(vram=None)).get('decider'), None)


class Windows(unittest.TestCase):
    def test_configured_physical_drive_is_measured_on_windows(self):
        from types import SimpleNamespace
        tracker = config.validate({'trackers': [{'kind': 'disk', 'name': 'physical', 'path': 'R:/'}]})
        tracker['trackers'] = [t for t in tracker['trackers'] if t['name'] == 'physical']
        with mock.patch.object(placement, 'IS_WINDOWS', True) as _placement_patch_1, mock.patch('drover.placement_exec.IS_WINDOWS', _placement_patch_1), mock.patch('drover.placement_git.IS_WINDOWS', _placement_patch_1), mock.patch('drover.placement_host.IS_WINDOWS', _placement_patch_1), mock.patch('drover.placement_paths.IS_WINDOWS', _placement_patch_1), mock.patch('drover.placement_process.IS_WINDOWS', _placement_patch_1), mock.patch('drover.placement_proof.IS_WINDOWS', _placement_patch_1), mock.patch('drover.placement_tree.IS_WINDOWS', _placement_patch_1), mock.patch('drover.placement_worker.IS_WINDOWS', _placement_patch_1), \
                mock.patch.object(disk.shutil, 'disk_usage', return_value=SimpleNamespace(free=10 * resources.GB)) as usage:
            rows = disk.read(tracker)
        usage.assert_called_once_with(placement.to_windows_path('R:/'))
        self.assertEqual(rows[0]['free_gb'], 10)
        self.assertEqual(disk.admission(rows)['verdict'], 'hold')

    def test_windows_unknown_load_and_no_wsl_disk(self):
        r = verdict({'platform': 'windows', 'load5': None, 'ram_available_gb': 16,
                     'disk_free_gb': {'c': 60, 'h': 80}, 'vram': None})
        self.assertEqual(r['verdict'], 'queue')
        self.assertEqual(r['facts']['load5'], None)
        self.assertEqual(r['reasons'], ['h: disk queue (80.0 GB free)'])

    def test_windows_hold_on_c(self):
        r = verdict({'platform': 'windows', 'ram_available_gb': 16, 'disk_free_gb': {'c': 10}})
        self.assertEqual((r['verdict'], r['cleanup_hint']), ('hold', 'drover disk-cleanup'))

    def test_real_gather_on_this_host_does_not_crash(self):
        facts = resources.gather()
        self.assertIn(facts['platform'], ('wsl', 'windows'))




class Trackers(unittest.TestCase):
    """The config's [[trackers]] drive check()."""

    def cfg(self, *trackers):
        from drover import config
        return config.validate({"trackers": list(trackers)})

    def test_a_tracker_override_changes_the_edge(self):
        cfg = self.cfg({'kind': 'load', 'name': 'load', 'queue_per_core': 1.0, 'hold_per_core': 2.0})
        self.assertEqual(resources.check(fake=wsl(load5=4.1), config=cfg)['verdict'], 'queue')   # 1.025 per core
        self.assertEqual(resources.check(fake=wsl(load5=8.4), config=cfg)['verdict'], 'hold')
        self.assertEqual(resources.check(fake=wsl(load5=4.0), config=cfg)['verdict'], 'go')

    def test_an_extra_disk_tracker_and_a_disabled_one(self):
        cfg = self.cfg({'kind': 'disk', 'name': 'd', 'path': 'D:\\', 'queue_gb': 100, 'hold_gb': 40},
                       {'kind': 'disk', 'name': 'c', 'enabled': False})
        r = resources.check(fake=wsl(disk_free_gb=disks(d=90, c=1)), config=cfg)
        self.assertEqual((r['verdict'], r['reasons']), ('queue', ['d: disk queue (90.0 GB free)']))
        self.assertEqual(resources.check(fake=wsl(disk_free_gb=disks(d=39)), config=cfg)['verdict'], 'hold')

    def test_ram_tracker_levels_and_slots(self):
        cfg = self.cfg({'kind': 'ram', 'name': 'ram', 'queue_base_gb': 10, 'hold_gb': 8, 'queue_per_worker_gb': 1})
        self.assertEqual(resources.check(fake=wsl(ram_available_gb=9), config=cfg)['verdict'], 'queue')
        self.assertEqual(resources.check(fake=wsl(ram_available_gb=7.9), config=cfg)['verdict'], 'hold')
        self.assertEqual(resources.check(fake=wsl(ram_available_gb=13), config=cfg)['slots'], 3)

    def test_vram_tracker_can_queue_and_hold_when_asked(self):
        cfg = self.cfg({'kind': 'vram', 'name': 'vram', 'queue_pct': 85, 'hold_pct': 95})
        self.assertEqual(resources.check(fake=wsl(vram=[20400, 24000]), config=cfg)['verdict'], 'go')
        self.assertEqual(resources.check(fake=wsl(vram=[21000, 24000]), config=cfg)['verdict'], 'queue')
        self.assertEqual(resources.check(fake=wsl(vram=[23000, 24000]), config=cfg)['verdict'], 'hold')

    def test_wsl_tracker_never_changes_the_global_verdict(self):
        cfg = self.cfg({'kind': 'wsl', 'name': 'wsl-up', 'queue_mem_pct': 80, 'hold_mem_pct': 90})
        win = {'platform': 'windows', 'ram_available_gb': 30, 'wsl_up': True, 'wsl_mem': {'used_gb': 15, 'cap_gb': 16}}
        r = resources.check(fake=win, config=cfg)
        self.assertEqual((r['verdict'], r['wsl']['verdict']), ('go', 'hold'))
        self.assertEqual(r['reasons'], [])
        self.assertIn('94%', r['wsl']['reasons'][0])
        win['wsl_mem'] = {'used_gb': 13, 'cap_gb': 16}
        self.assertEqual(resources.check(fake=win, config=cfg)['wsl']['verdict'], 'queue')
        win['wsl_mem'] = {'used_gb': 8, 'cap_gb': 16}
        self.assertEqual(resources.check(fake=win, config=cfg)['wsl']['verdict'], 'go')

    def test_wsl_down_is_a_wsl_queue_and_unknown_memory_never_blocks(self):
        cfg = self.cfg({'kind': 'wsl', 'name': 'wsl-up', 'queue_mem_pct': 80})
        r = resources.check(fake={'platform': 'windows', 'ram_available_gb': 30, 'wsl_up': False}, config=cfg)
        self.assertEqual((r['verdict'], r['wsl']), ('go', {'verdict': 'queue', 'reasons': ['WSL is not running']}))
        r = resources.check(fake={'platform': 'windows', 'ram_available_gb': 30, 'wsl_up': True, 'wsl_mem': None}, config=cfg)
        self.assertEqual(r['wsl']['verdict'], 'go')

    def test_wslconfig_memory_cap_parses(self):
        p = resources.parse_wslconfig_memory_gb
        self.assertEqual(p('[wsl2]\nmemory=16GB\nprocessors=8\n'), 16.0)
        self.assertEqual(p('[wsl2]\nmemory = 2048MB # cap\n'), 2.0)
        self.assertEqual(p('[interop]\nmemory=4GB\n'), None)
        self.assertEqual(p('[wsl2]\nprocessors=2\n'), None)

    def test_wsl_memory_fact_comes_from_meminfo_and_the_cap(self):
        info = 'MemTotal:       16777216 kB\nMemAvailable:    2097152 kB\n'
        with mock.patch('pathlib.Path.read_text', return_value=info), \
                mock.patch.object(resources, 'wslconfig_path', return_value=None):
            up, mem = resources.read_wsl('Ubuntu', False, True)
        self.assertTrue(up)
        self.assertEqual((mem['used_gb'], mem['cap_gb']), (14.0, 16.0))





REG_OUT = (
    'HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Lxss\\{aaa}\r\n'
    '    BasePath    REG_SZ    \\\\?\\C:\\Other\\Debian\r\n'
    '    DistributionName    REG_SZ    Debian\r\n\r\n'
    'HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Lxss\\{bbb}\r\n'
    '    DistributionName    REG_SZ    Ubuntu\r\n'
    '    BasePath    REG_SZ    R:\\WSL\\Ubuntu\r\n')


class Vhdx(unittest.TestCase):
    def vhdx(self, distro='Ubuntu', env=None, out=REG_OUT):
        env = dict(env or {}, WSL_DISTRO_NAME=distro)
        done = mock.Mock(stdout=out)
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch('os.path.exists', return_value=True), \
                mock.patch('subprocess.run', return_value=done):
            return resources.placement.vhdx_dir()

    def test_registry_parsing_multi_distro_any_order(self):
        self.assertEqual(self.vhdx('Ubuntu'), '/mnt/r/WSL/Ubuntu')
        self.assertEqual(self.vhdx('Debian'), '/mnt/c/Other/Debian')
        self.assertIsNone(self.vhdx('Arch'))

    def test_env_overrides_with_drover_fallback(self):
        self.assertEqual(self.vhdx(env={'DROVER_VHDX_DIR': '/x'}), '/x')
        self.assertEqual(self.vhdx(env={'DROVER_VHDX_DIR': '/y'}), '/y')


    def test_unknown_physical_drive_is_visible_without_blocking(self):
        r = verdict(wsl(disk_free_gb=disks(h=None)))
        self.assertEqual(r['verdict'], 'go')
        self.assertEqual(next(row for row in r['facts']['disks'] if row['name'] == 'h')['level'], 'unknown')

    def test_reader_failure_is_unknown_without_host_discovery(self):
        with mock.patch('subprocess.run', side_effect=AssertionError('no host probe')), \
                mock.patch.object(disk.shutil, 'disk_usage', side_effect=OSError('unavailable')):
            rows = disk.read()
        self.assertTrue(all(row['free_gb'] is None for row in rows))


MOUNTINFO = (
    '1 0 8:1 / / rw,relatime - ext4 /dev/sda1 rw\n'
    '2 1 8:2 /data /mnt/data ro,relatime - ext4 /dev/sdb1 rw\n'
    '3 1 8:3 / /mnt/my\\040disk rw,relatime - ext4 /dev/sdc1 ro,errors=remount-ro\n'
    '4 2 8:4 / /mnt/data/sub rw,relatime - ext4 /dev/sdd1 rw\n')


class MountInfo(unittest.TestCase):
    def ro(self, path):
        with mock.patch('pathlib.Path.read_text', return_value=MOUNTINFO), \
                mock.patch('os.path.realpath', side_effect=lambda p: p):
            return resources.read_only_mount(path)

    def test_longest_prefix_wins(self):
        self.assertFalse(self.ro('/home/me'))
        self.assertTrue(self.ro('/mnt/data/file'))
        self.assertFalse(self.ro('/mnt/data/sub/file'))
        self.assertFalse(self.ro('/mnt/database'))

    def test_escaped_space_and_super_options(self):
        self.assertTrue(self.ro('/mnt/my disk/x'))

    def test_unreadable_is_unknown(self):
        with mock.patch('pathlib.Path.read_text', side_effect=OSError):
            self.assertIsNone(resources.read_only_mount('/'))


class Ollama(unittest.TestCase):
    def read(self, body, windows=True, url_env=None):
        response = mock.MagicMock()
        response.__enter__.return_value = io.BytesIO(json.dumps(body).encode())
        env = {'DROVER_OLLAMA_URL': url_env} if url_env else {}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(resources, 'is_windows', return_value=windows), \
                mock.patch('urllib.request.urlopen', return_value=response) as opened:
            return resources.read_ollama_ram_gb(), opened

    def test_sums_non_vram_bytes(self):
        gb = resources.GB
        ram, opened = self.read({'models': [{'size': 10 * gb, 'size_vram': 6 * gb}, {'size': 2 * gb}]})
        self.assertEqual(ram, 6.0)
        self.assertEqual(opened.call_args[0][0], 'http://127.0.0.1:11434/api/ps')

    def test_url_override_and_wsl_ignored(self):
        _, opened = self.read({'models': []}, url_env='http://h:1/')
        self.assertEqual(opened.call_args[0][0], 'http://h:1/api/ps')
        ram, opened = self.read({'models': [{'size': 5 * resources.GB}]}, windows=False)
        self.assertEqual(ram, 0.0)
        opened.assert_not_called()

    def test_failure_is_zero(self):
        with mock.patch.object(resources, 'is_windows', return_value=True), \
                mock.patch('urllib.request.urlopen', side_effect=OSError):
            self.assertEqual(resources.read_ollama_ram_gb(), 0.0)


class Nan(unittest.TestCase):
    def test_nan_ram_does_not_crash_slots(self):
        r = verdict(wsl(ram_available_gb=float('nan')))
        self.assertEqual((r['verdict'], r['slots']), ('go', 1))


class Cli(unittest.TestCase):
    def run_cli(self, fake, *args):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {'DROVER_RESOURCES_FAKE': json.dumps(fake)}), redirect_stdout(out):
            code = resources.main(list(args))
        return code, out.getvalue()

    def test_exit_codes(self):
        self.assertEqual(self.run_cli(wsl())[0], 0)
        self.assertEqual(self.run_cli(wsl(ram_available_gb=5))[0], 4)
        self.assertEqual(self.run_cli(wsl(ram_available_gb=3))[0], 5)

    def test_json_and_new(self):
        code, out = self.run_cli(wsl(ram_available_gb=7.0), '--json', '--new', '3')
        self.assertEqual(code, 4)
        self.assertEqual(json.loads(out)['verdict'], 'queue')

    def test_human_output(self):
        code, out = self.run_cli(wsl(disk_free_gb=disks(c=10)))
        self.assertIn('HOLD', out)
        self.assertIn('drover disk-cleanup', out)


if __name__ == '__main__':
    unittest.main()


class RamWhereWorkRuns(unittest.TestCase):
    """Windows hub, WSL workers: the side that would run the next claim decides the RAM verdict."""

    def cfg(self, *placements):
        cfg = {'trackers': config.DEFAULTS['trackers']}
        cfg['seats'] = [dict(name=f's{i}', placement=p) for i, p in enumerate(placements)]
        cfg['decider'] = config.DEFAULTS['decider']
        return cfg

    def check(self, win_gb, wsl_gb, *placements):
        fake = {'platform': 'windows', 'ram_available_gb': win_gb, 'wsl_ram_available_gb': wsl_gb,
                'disk_free_gb': {'h': 200, 'wsl': 200, 'c': 200}}
        return resources.check(fake=fake, config=self.cfg(*placements))

    def test_wsl_seats_ignore_starved_windows(self):
        r = self.check(1.4, 25.0, 'wsl')
        self.assertEqual(r['verdict'], 'go')
        self.assertEqual(r['facts']['wsl_ram_available_gb'], 25.0)

    def test_wsl_seats_hold_when_wsl_is_short(self):
        self.assertEqual(self.check(30.0, 1.0, 'wsl')['verdict'], 'hold')

    def test_windows_seats_still_use_windows(self):
        self.assertEqual(self.check(1.4, 25.0, 'windows')['verdict'], 'hold')

    def test_mixed_seats_hold_only_when_every_side_is_short(self):
        self.assertEqual(self.check(1.4, 25.0, 'windows', 'wsl')['verdict'], 'go')
        self.assertEqual(self.check(30.0, 1.0, 'windows', 'wsl')['verdict'], 'go')
        self.assertEqual(self.check(1.4, 1.0, 'windows', 'wsl')['verdict'], 'hold')

    def test_mixed_seats_report_each_side_verdict(self):
        sides = self.check(1.4, 25.0, 'windows', 'wsl')['side_admission']
        self.assertEqual((sides['windows']['verdict'], sides['wsl']['verdict']), ('hold', 'go'))
        self.assertNotIn('side_admission', self.check(1.4, 25.0, 'wsl'))

    def test_unreadable_wsl_falls_back_to_windows(self):
        self.assertEqual(self.check(1.4, None, 'wsl')['verdict'], 'hold')

    def test_wsl_ram_read_uses_the_exec_helper_with_a_short_timeout(self):
        seen = {}
        def run(argv, **kw):
            seen.update(kw, argv=argv)
            return mock.Mock(stdout='MemTotal: 33554432 kB\nMemAvailable: 26214400 kB\n', returncode=0)
        with mock.patch.object(resources, 'wsl_running', return_value=True):
            gb = resources.read_wsl_ram_gb('Ubuntu', run=run)
        self.assertAlmostEqual(gb, 25.0)
        self.assertLessEqual(seen['timeout'], 10)
        self.assertIn('/proc/meminfo', seen['argv'])

    def test_wsl_ram_read_is_unknown_when_down_or_slow(self):
        with mock.patch.object(resources, 'wsl_running', return_value=False):
            self.assertIsNone(resources.read_wsl_ram_gb('Ubuntu'))
        def slow(argv, **kw):
            raise __import__('subprocess').TimeoutExpired(argv, 5)
        with mock.patch.object(resources, 'wsl_running', return_value=True):
            self.assertIsNone(resources.read_wsl_ram_gb('Ubuntu', run=slow))
