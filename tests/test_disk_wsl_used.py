"""WSL used space is read without starting WSL and triggers cleanup on its own thresholds."""
import os
import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch

from drover import config, disk, placement, placement_host
from tests.disk_fixture import DiskCase


class Reading(unittest.TestCase):
    def setUp(self):
        placement._WSL_USED.clear()
        placement._WSL_FREE.clear()
        self.addCleanup(placement._WSL_USED.clear)
        self.addCleanup(placement._WSL_FREE.clear)
        win = mock.patch.object(placement_host, 'IS_WINDOWS', True)
        win.start()
        self.addCleanup(win.stop)

    def test_used_reads_df_used_column(self):
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs['timeout']))
            return SimpleNamespace(returncode=0, stdout='        Used\n312000000000\n')
        self.assertAlmostEqual(placement.wsl_used_gb('U', True, run=run), 312e9 / 1024 ** 3)
        placement.wsl_used_gb('U', True, run=run)           # shared for a minute: no second spawn
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], 5)
        self.assertIn('--output=used', calls[0][0])
        self.assertIsNone(placement.wsl_used_gb('V', True, run=lambda *a, **k: SimpleNamespace(returncode=1, stdout='')))
        self.assertIsNone(placement.wsl_used_gb('W', True, run=mock.Mock(side_effect=OSError), ttl=0))

    def test_not_up_returns_none_and_runs_nothing(self):
        run = mock.Mock()
        self.assertIsNone(placement.wsl_used_gb('D', False, run=run))
        run.assert_not_called()

    def test_free_and_used_do_not_share_a_value(self):
        run = lambda argv, **k: SimpleNamespace(returncode=0, stdout='h\n%d\n' % (1024 ** 3 * (7 if 'avail' in argv[-2] else 9)))
        self.assertEqual(placement.wsl_free_gb('U', True, run=run), 7)
        self.assertEqual(placement.wsl_used_gb('U', True, run=run), 9)


class Levels(unittest.TestCase):
    def test_levels_at_299_300_499_500(self):
        cfg = {'disk_cleanup': {}}
        got = [disk.wsl_budget(cfg, used=u)['level'] for u in (299, 300, 499, 500)]
        self.assertEqual(got, ['ok', 'purge', 'purge', 'stop'])
        self.assertEqual(disk.wsl_budget(cfg, used=310), dict(used_gb=310, level='purge', target_gb=225.0))

    def test_unknown_reading_is_unknown(self):
        for bad in (None, float('nan'), -1, True):
            self.assertEqual(disk.wsl_budget({'disk_cleanup': {}}, used=bad)['level'], 'unknown')

    def test_configured_thresholds_apply(self):
        cfg = {'disk_cleanup': dict(wsl_purge_gb=100, wsl_target_gb=50, wsl_stop_gb=120)}
        self.assertEqual([disk.wsl_budget(cfg, used=u)['level'] for u in (99, 100, 120)], ['ok', 'purge', 'stop'])
        self.assertEqual(disk.wsl_budget(cfg, used=100)['target_gb'], 50)

    def test_thresholds_are_validated(self):
        for bad in ({'wsl_target_gb': 300}, {'wsl_purge_gb': 500}, {'wsl_stop_gb': 300}, {'wsl_purge_gb': 0},
                    {'wsl_stop_gb': float('inf')}, {'wsl_target_gb': '1'}, {'wsl_purge_gb': True}):
            with self.subTest(bad=bad), self.assertRaises(config.ConfigError):
                config.validate({'disk_cleanup': bad})
        got = config.validate({'disk_cleanup': dict(wsl_target_gb=1, wsl_purge_gb=2, wsl_stop_gb=3)})['disk_cleanup']
        self.assertEqual((got['wsl_target_gb'], got['wsl_purge_gb'], got['wsl_stop_gb']), (1, 2, 3))


class Triggers(DiskCase):
    def setUp(self):
        super().setUp()
        mapping = patch('drover.placement.physical_disk_path', return_value=str(self.root))
        mapping.start()
        self.addCleanup(mapping.stop)
        self.launches = []
        self.free = 200

    def tick(self, used, free=None):
        self.free = self.free if free is None else free
        return disk.tick(self.cfg, now=self.now, reader=lambda path: self.free, wsl_used=lambda: used,
                         enqueue=lambda: self.launches.append(1))

    def episodes(self):
        from drover import disk_state
        return disk_state.read(config.state_dir(self.cfg)).get('episodes', {})

    def test_h_ok_but_wsl_over_triggers(self):
        self.tick(310)
        self.assertEqual(len(self.launches), 1)
        self.assertIsNone(self.episodes()['test-drive'].get('claim'))
        self.assertIsNotNone(self.episodes()['wsl']['claim'])

    def test_stop_level_also_triggers(self):
        self.tick(500)
        self.assertEqual(len(self.launches), 1)

    def test_wsl_ok_but_h_low_triggers(self):
        self.tick(100, free=80)
        self.assertEqual(len(self.launches), 1)
        self.assertNotIn('wsl', self.episodes())

    def test_wsl_cooldown_is_independent_of_drive_episode(self):
        self.tick(100, free=80)                      # drive episode claims; its cooldown starts
        self.assertEqual(len(self.launches), 1)
        self.tick(310, free=80)                      # the drive is still claimed, the wsl episode is not
        self.assertEqual(len(self.launches), 2)
        self.assertEqual(sorted(self.episodes()), ['test-drive', 'wsl'])
        self.tick(310, free=80)                      # both claimed: nothing more
        self.assertEqual(len(self.launches), 2)

    def test_unknown_reading_triggers_nothing(self):
        self.tick(None)
        self.assertEqual(self.launches, [])
        self.assertNotIn('wsl', self.episodes())

    def test_ok_reading_triggers_nothing(self):
        self.tick(299)
        self.assertEqual(self.launches, [])

    def test_default_reading_never_runs_wsl_without_a_combined_tracker(self):
        with patch.object(placement, 'wsl_running') as running, patch.object(placement, 'wsl_used_gb') as used:
            disk.tick(self.cfg, now=self.now, reader=lambda path: 200, enqueue=lambda: None)
        running.assert_not_called()
        used.assert_not_called()

    def test_default_reading_does_not_measure_a_stopped_distro(self):
        self.cfg['trackers'][0].update(wsl_combined=True, host_floor_gb=10.0)
        with patch.object(placement, 'wsl_running', return_value=False), \
                patch.object(placement, 'wsl_used_gb', return_value=None) as used:
            disk.tick(self.cfg, now=self.now, reader=lambda path: 200, enqueue=lambda: None)
        self.assertEqual(used.call_args.args[1], False)

class StopHoldsNewWork(DiskCase):
    def setUp(self):
        super().setUp()
        self.cfg['disk_cleanup'].update(wsl_purge_gb=300, wsl_target_gb=225, wsl_stop_gb=500)
        self.cfg['trackers'][0].update(wsl_combined=True, host_floor_gb=10.0)
        for target in ('drover.placement.physical_disk_path',):
            m = patch(target, return_value=str(self.root))
            m.start()
            self.addCleanup(m.stop)
        free = patch.object(disk, '_free', return_value=200)
        free.start()
        self.addCleanup(free.stop)
        wf = patch.object(placement, 'wsl_free_known', return_value=100.0)
        wf.start()
        self.addCleanup(wf.stop)

    def held(self, side, used):
        with patch.object(placement, 'wsl_used_known', return_value=used), \
                patch.object(disk, '_wsl_used', side_effect=AssertionError('held must not measure')):
            return disk.held(side, self.cfg)

    def test_stop_level_holds_new_wsl_work(self):
        self.assertTrue(self.held('wsl', 500))
        self.assertFalse(self.held('wsl', 499))
        reason = disk.wsl_stop_row(self.cfg, 512)['detail']
        self.assertIn('512', reason)
        self.assertIn('500', reason)

    def test_stop_level_leaves_running_work(self):
        # Only admission consults the budget: a stop reading never produces a pause token or kills anything.
        result = disk.tick(self.cfg, now=self.now, reader=lambda p: 200, wsl_used=lambda: 600,
                           enqueue=lambda: None)
        self.assertFalse(result['pause'])
        self.assertEqual(result['critical_tokens'], [])

    def test_windows_work_still_admitted_at_stop(self):
        self.assertFalse(self.held('windows', 900))

    def test_unknown_used_holds_nothing(self):
        self.assertFalse(self.held('wsl', None))
        self.assertIsNone(disk.wsl_stop_row(self.cfg, None))

    def test_dispatcher_wsl_side_verdict_holds_at_stop(self):
        rows = disk.read(self.cfg, reader=lambda p: 200, side='wsl', wsl_free=100.0)
        self.assertEqual(disk.admission(rows)['verdict'], 'go')
        stop = disk.wsl_stop_row(self.cfg, 520)
        verdict = disk.admission(rows + [stop])
        self.assertEqual(verdict['verdict'], 'hold')
        self.assertIn('520', verdict['reasons'][0])


class StopBypassesCooldown(Triggers):
    def test_stop_claims_at_once_at_the_start_of_an_episode(self):
        self.tick(310)
        self.finish_claim()
        self.now += 60
        self.tick(200)                               # a healthy reading ends the episode
        self.tick(500)
        self.assertEqual(len(self.launches), 2)

    def test_stop_comes_back_at_the_floor_after_bytes_were_freed(self):
        self.tick(500)
        self.finish_claim(reclaimed=True)
        self.now += 29 * 60
        self.tick(500)
        self.assertEqual(len(self.launches), 1)
        self.now += 60
        self.tick(500)
        self.assertEqual(len(self.launches), 2)

    def test_stop_waits_out_the_cooldown_after_a_claim_that_freed_nothing(self):
        self.tick(500)
        self.finish_claim()
        self.now += 6 * 3600 - 1
        self.tick(500)
        self.assertEqual(len(self.launches), 1)
        self.now += 1
        self.tick(500)
        self.assertEqual(len(self.launches), 2)

    def test_the_floor_holds_under_a_short_cooldown(self):
        self.cfg['disk_cleanup']['cooldown_s'] = 60
        self.tick(500)
        self.finish_claim()
        self.now += 29 * 60
        self.tick(500)
        self.assertEqual(len(self.launches), 1)

    def test_purge_still_waits_for_cooldown(self):
        self.tick(310)
        self.finish_claim()
        self.now += 60
        self.tick(310)
        self.assertEqual(len(self.launches), 1)

    def test_stop_respects_partial_retry_spacing(self):
        self.tick(500)
        self.finish_claim(partial=True, reclaimed=True)
        self.now += 60
        self.tick(500)
        self.assertEqual(len(self.launches), 1)
        self.now += 31 * 60
        self.tick(500)
        self.assertEqual(len(self.launches), 2)

    def finish_claim(self, partial=False, reclaimed=False):
        from drover import disk_state

        def change(state):
            state['episodes']['wsl'].update(claim=None, last_cleanup=self.now, partial=partial, reclaimed=reclaimed)
        disk_state.update(config.state_dir(self.cfg), change)


if __name__ == '__main__':
    unittest.main()
