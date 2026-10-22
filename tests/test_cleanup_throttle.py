"""Cleanup is gated on load and RAM: no launch on a loaded machine, abort mid-run when a hold threshold is crossed."""
import json
import os
import unittest
from unittest.mock import patch

from drover import config, disk, disk_state, placement, resources
from tests import test_disk_trigger as base
from tests.disk_fixture import DiskCase


def pressure(verdict, *reasons):
    return lambda: dict(verdict=verdict, reasons=list(reasons))


class CleanupPressure(unittest.TestCase):
    """resources.cleanup_pressure judges load and RAM only, never disk."""

    def cfg(self):
        return dict(config.DEFAULTS, trackers=config.validate({}).get('trackers'))

    def fake(self, **over):
        fake = {'platform': 'wsl', 'load5': 1.0, 'cores': 4, 'ram_available_gb': 20,
                'disk_free_gb': {}, 'vram': None, 'read_only': False}
        fake.update(over)
        return fake

    def test_disk_pressure_is_ignored(self):
        cfg = config.validate({})
        names = [t['name'] for t in cfg['trackers'] if t['kind'] == 'disk']
        fake = self.fake(disk_free_gb={n: 1 for n in names})
        self.assertEqual(resources.cleanup_pressure(cfg, fake=fake)['verdict'], 'go')

    def test_load_and_ram_drive_the_verdict(self):
        cfg = config.validate({})
        self.assertEqual(resources.cleanup_pressure(cfg, fake=self.fake(load5=12))['verdict'], 'queue')
        self.assertEqual(resources.cleanup_pressure(cfg, fake=self.fake(load5=15))['verdict'], 'hold')
        self.assertEqual(resources.cleanup_pressure(cfg, fake=self.fake(ram_available_gb=3))['verdict'], 'hold')


class Trigger(DiskCase):
    """The trigger fixture's helpers without its inherited tests."""
    def setUp(self):
        super().setUp()
        mapping = patch('drover.placement.physical_disk_path', return_value=str(self.root))
        mapping.start()
        self.addCleanup(mapping.stop)

    activate = base.Trigger.activate
    tick = base.Trigger.tick
    complete = base.Trigger.complete


class LaunchGate(Trigger):
    def setUp(self):
        super().setUp()
        self.launches = []

    def enqueue(self, *a, **kw):
        self.launches.append(1)
        return self.complete()

    def tick_gated(self, gate):
        return disk.tick(self.cfg, now=self.now, reader=lambda path: self.free, enqueue=self.enqueue, gate=gate)

    def test_loaded_machine_defers_and_records_why(self):
        self.activate()
        self.free = 60
        for verdict in ('queue', 'hold'):
            self.tick_gated(pressure(verdict, 'load 4.00 per core > 2.5'))
            self.assertEqual(self.launches, [])
        deferred = disk_state.read(config.state_dir(self.cfg))['cleanup_deferred']
        self.assertEqual(deferred['reasons'], ['load 4.00 per core > 2.5'])
        self.assertEqual(deferred['at'], self.now)

    def test_go_launches_once_load_clears(self):
        self.activate()
        self.free = 60
        self.tick_gated(pressure('queue', 'busy'))
        self.assertEqual(self.launches, [])
        self.tick_gated(pressure('go'))
        self.assertEqual(len(self.launches), 1)
        self.assertNotIn('cleanup_deferred', disk_state.read(config.state_dir(self.cfg)))

    def test_healthy_ticks_never_evaluate_the_gate(self):
        self.free = 500
        calls = []
        self.tick_gated(lambda: calls.append(1) or dict(verdict='go', reasons=[]))
        self.assertEqual(calls, [])


class MidRunAbort(Trigger):
    def run_triggered(self, pressure_fn, apply=True):
        from dataclasses import replace
        from drover import disk_cleanup
        self.approve_report()
        self.calls.clear()
        runtime = replace(self.runtime(), pressure=pressure_fn)
        with patch.object(placement, 'physical_disk_path', return_value=str(self.root)):
            return disk_cleanup.run(self.cfg, mode='triggered', apply=apply, runtime=runtime)

    def test_hold_before_a_stage_aborts_cleanly_and_releases_the_lock(self):
        self.free = 60
        seen = []
        def pressure_fn():
            seen.append(len(self.calls))
            return dict(verdict='go' if len(seen) <= 2 else 'hold', reasons=['load 5.00 per core > 3.5'])
        result = self.run_triggered(pressure_fn)
        self.assertEqual(result['aborted'], 'aborted_load')
        self.assertEqual(result['abort_reasons'], ['load 5.00 per core > 3.5'])
        self.assertEqual(len(self.calls), 2)          # two stage workers ran, no third started
        with disk_state.CleanupLock(config.state_dir(self.cfg)):
            pass                                        # the lock is free again
        self.assertFalse(list((config.state_dir(self.cfg) / 'disk').glob('processes-*')))

    def test_abort_records_the_triggered_result(self):
        self.free = 60
        claim = dict(id='abc', identity=placement.cleanup_identity(), applied=True)
        disk_state.update(config.state_dir(self.cfg), lambda s: s.setdefault('episodes', {}).setdefault(
            'test-drive', {}).update(claim=claim))
        result = self.run_triggered(pressure('hold', 'RAM available 3.0 GB < 4 GB'))
        receipt = json.loads((config.state_dir(self.cfg) / 'disk' / 'result-abc.json').read_text())
        self.assertEqual(receipt['status'], 'aborted_load')
        self.assertFalse(receipt['applied'])
        self.assertEqual(self.calls, [])
        self.assertEqual(result['aborted'], 'aborted_load')
        state = disk_state.read(config.state_dir(self.cfg))
        self.assertIsNone(state['episodes']['test-drive']['claim'])

    def test_queue_level_pressure_does_not_abort_a_running_cleanup(self):
        self.free = 60
        result = self.run_triggered(pressure('queue', 'busy'))
        self.assertNotIn('aborted', result)

    def test_no_pressure_probe_means_no_check(self):
        self.free = 60
        self.assertNotIn('aborted', self.run_triggered(None))


class Priority(unittest.TestCase):
    def test_windows_runs_below_normal(self):
        calls = []
        placement.lower_priority(windows=True, set_class=lambda value: calls.append(value))
        self.assertEqual(calls, [0x4000])

    def test_wsl_nices_by_ten(self):
        with patch('os.nice', create=True) as nice:
            placement.lower_priority(windows=False)
        nice.assert_called_once_with(10)

    def test_failure_to_lower_never_fails_cleanup(self):
        with patch('os.nice', side_effect=OSError, create=True):
            placement.lower_priority(windows=False)


if __name__ == '__main__':
    unittest.main()
