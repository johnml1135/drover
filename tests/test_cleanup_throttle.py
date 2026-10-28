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



class Cooldown(Trigger):
    """After any cleanup run the hub waits the cooldown, and re-triggers only on a meaningful drop."""

    def setUp(self):
        super().setUp()
        self.launches = []
        self.outcome = 'completed'

    def enqueue(self, *a, **kw):
        self.launches.append(self.now)
        if self.outcome == 'completed':
            return self.complete()
        if self.outcome == 'aborted':
            from dataclasses import replace
            from drover import disk_cleanup
            return disk_cleanup.run(self.cfg, mode='triggered', apply=True,
                                    runtime=replace(self.runtime(), pressure=pressure('hold', 'busy')))
        return None   # failed: no receipt, no process

    def go(self, free, advance=0):
        self.free = free
        self.now += advance
        return self.tick(self.enqueue)

    def test_default_cooldown_is_six_hours(self):
        self.assertEqual(config.validate({})['disk_cleanup']['cooldown_s'], 6 * 3600)

    def test_cooldown_blocks_after_each_kind_of_run(self):
        for outcome in ('completed', 'aborted', 'failed'):
            with self.subTest(outcome):
                self.launches.clear()
                self.outcome = outcome
                state = config.state_dir(self.cfg)
                disk_state.update(state, lambda s: [s.pop(k, None) for k in ('episodes', 'last_cleanup', 'cleanup_deferred')])
                self.activate()
                self.go(60)
                self.assertEqual(len(self.launches), 1)
                if outcome == 'failed':
                    # the claimed process is gone: mark it dead through its identity
                    disk_state.update(state, lambda s: s['episodes']['test-drive'].update(claim=None))
                self.go(30, advance=3600)                          # a huge drop, one hour later
                self.assertEqual(len(self.launches), 1, 'cooldown holds')
                deferred = disk_state.read(state)['cleanup_deferred']
                self.assertEqual(deferred['verdict'], 'cooldown')
                self.go(30, advance=6 * 3600)
                self.assertEqual(len(self.launches), 2, 'cooldown elapsed')

    def test_small_drop_does_not_retrigger_but_ten_gb_does(self):
        self.activate()
        self.go(80)
        self.assertEqual(len(self.launches), 1)       # cleanup leaves 80 free
        self.go(72, advance=7 * 3600)
        self.assertEqual(len(self.launches), 1, '8 GB drop is not enough')
        self.go(69.9, advance=60)
        self.assertEqual(len(self.launches), 2, '10 GB drop is')


class SingleProcess(Trigger):
    def test_identity_timeout_after_spawn_keeps_the_claim(self):
        proc = type('P', (), {'pid': 4242})()
        with patch.object(placement, 'detach', return_value=(proc, False)), \
                patch.object(placement, 'cleanup_identity', side_effect=__import__('subprocess').TimeoutExpired('ps', 15)) as _placement_patch_1, patch('drover.placement_process.cleanup_identity', _placement_patch_1), patch('drover.placement_proof.cleanup_identity', _placement_patch_1):
            answer = placement.cleanup_enqueue(self.cfg, dict(id='x', applied=False))
        self.assertEqual(answer['identity']['pid'], 4242)
        self.assertIsNone(answer['identity']['birth'])

    def test_not_yet_visible_process_is_unknown_not_dead(self):
        proc = type('P', (), {'pid': 4242})()
        with patch.object(placement, 'detach', return_value=(proc, False)), \
                patch.object(placement, 'cleanup_identity', return_value=dict(pid=4242, birth='absent', side=placement.cleanup_side())) as _placement_patch_2, patch('drover.placement_process.cleanup_identity', _placement_patch_2), patch('drover.placement_proof.cleanup_identity', _placement_patch_2):
            answer = placement.cleanup_enqueue(self.cfg, dict(id='x', applied=False))
        self.assertIsNone(answer['identity']['birth'])

    def test_running_unknown_birth_claim_blocks_a_second_launch(self):
        self.activate()
        launches = []
        me = placement.cleanup_identity()
        def enqueue(*a, **kw):
            launches.append(1)
            return dict(identity=dict(pid=me['pid'], birth=None, side=me['side']))
        for advance in (0, 900, 900, 7 * 3600):
            self.now += advance
            self.free = 60
            self.tick(enqueue)
        self.assertEqual(len(launches), 1)          # the process is alive; birth resolves from its pid

    def test_unknown_claim_of_a_vanished_process_is_released(self):
        self.activate()
        launches = []
        def enqueue(*a, **kw):
            launches.append(1)
            return dict(identity=dict(pid=2 ** 22 + 1, birth=None, side=placement.cleanup_side()))
        self.free = 60
        self.tick(enqueue)
        self.now += 3600
        self.tick(enqueue)                       # released as dead; the cooldown now runs from here
        self.assertEqual(len(launches), 1)
        self.now += 7 * 3600
        self.tick(enqueue)
        self.assertEqual(len(launches), 2)


class Defaults(unittest.TestCase):
    def test_example_documents_the_cooldown_key(self):
        from pathlib import Path
        self.assertIn('cooldown_s', (Path(config.__file__).parents[1] / 'drover.example.toml').read_text())


class StuckClaim(Trigger):
    """A claim whose process cannot be read (PowerShell keeps timing out) is released by age."""

    def stuck(self, at):
        import subprocess
        self.cfg['disk_cleanup']['cooldown_s'] = 0
        self.activate()
        self.free = 60
        launches = []
        me = placement.cleanup_identity()
        original = placement.cleanup_identity

        def enqueue(*a, **kw):
            launches.append(1)
            return dict(identity=dict(pid=me['pid'], birth=None, side=me['side']))
        self.tick(enqueue)

        def age(state):
            claim = state['episodes']['test-drive']['claim']
            if at is None:
                del claim['at']
            else:
                claim['at'] = at
        disk_state.update(config.state_dir(self.cfg), age)

        def flaky(pid=None):
            if pid is not None:
                raise subprocess.TimeoutExpired('powershell', 15)
            return original()
        return launches, enqueue, flaky

    def test_old_claim_is_released_although_the_query_keeps_timing_out(self):
        launches, enqueue, flaky = self.stuck(self.now - 25 * 3600)
        with patch.object(placement, 'cleanup_identity', side_effect=flaky) as _placement_patch_3, patch('drover.placement_process.cleanup_identity', _placement_patch_3), patch('drover.placement_proof.cleanup_identity', _placement_patch_3):
            self.tick(enqueue)
        self.assertEqual(len(launches), 2)

    def test_young_claim_waits_when_the_query_times_out(self):
        launches, enqueue, flaky = self.stuck(self.now - 3600)
        with patch.object(placement, 'cleanup_identity', side_effect=flaky) as _placement_patch_4, patch('drover.placement_process.cleanup_identity', _placement_patch_4), patch('drover.placement_proof.cleanup_identity', _placement_patch_4):
            self.tick(enqueue)
        self.assertEqual(len(launches), 1)

    def test_legacy_claim_without_at_counts_as_old(self):
        launches, enqueue, flaky = self.stuck(None)
        with patch.object(placement, 'cleanup_identity', side_effect=flaky) as _placement_patch_5, patch('drover.placement_process.cleanup_identity', _placement_patch_5), patch('drover.placement_proof.cleanup_identity', _placement_patch_5):
            self.tick(enqueue)
        self.assertEqual(len(launches), 2)


class DeferralNotice(Trigger):
    def setUp(self):
        super().setUp()
        self.launches = []

    def enqueue(self, *a, **kw):
        self.launches.append(1)
        return self.complete()

    def tick_gated(self, gate):
        return disk.tick(self.cfg, now=self.now, reader=lambda path: self.free, enqueue=self.enqueue, gate=gate)

    def outbox(self):
        return disk_state.read(config.state_dir(self.cfg)).get('outbox', {})

    def test_notice_after_thirty_minutes_then_retracted_when_cleanup_runs(self):
        self.cfg['disk_cleanup']['cooldown_s'] = 0
        self.activate()
        self.free = 60                                   # queue level
        self.tick_gated(pressure('queue', 'busy'))
        self.now += 20 * 60
        self.tick_gated(pressure('queue', 'busy'))
        self.assertNotIn('cleanup_deferred', self.outbox())
        self.now += 11 * 60
        self.tick_gated(pressure('queue', 'busy'))
        entry = self.outbox()['cleanup_deferred']
        self.assertEqual(entry['notice']['kind'], 'cleanup_deferred')
        self.tick_gated(pressure('queue', 'busy'))
        self.assertEqual(len(self.outbox()), 1)          # one notice, not one per tick
        self.tick_gated(pressure('go'))
        self.assertEqual(len(self.launches), 1)
        self.assertNotIn('cleanup_deferred', self.outbox())


class CriticalDisk(Trigger):
    def setUp(self):
        super().setUp()
        self.launches = []

    def enqueue(self, *a, **kw):
        self.launches.append(1)
        return self.complete()

    def tick_gated(self, gate):
        return disk.tick(self.cfg, now=self.now, reader=lambda path: self.free, enqueue=self.enqueue, gate=gate)

    def test_critical_launches_under_queue_pressure_but_not_hold(self):
        self.activate()
        self.free = 20                                   # critical (below 25)
        self.tick_gated(pressure('hold', 'RAM'))
        self.assertEqual(self.launches, [])
        self.tick_gated(pressure('queue', 'busy'))
        self.assertEqual(len(self.launches), 1)

    def test_critical_bypasses_the_cooldown_but_queue_level_does_not(self):
        self.cfg['disk_cleanup']['cooldown_s'] = 6 * 3600
        self.activate()
        self.free = 60
        self.tick_gated(pressure('go'))
        self.assertEqual(len(self.launches), 1)           # leaves 60 free
        self.free = 45
        self.now += 600
        self.tick_gated(pressure('go'))
        self.assertEqual(len(self.launches), 1, 'queue-level drop waits out the cooldown')
        self.free = 20
        self.now += 600
        self.tick_gated(pressure('go'))
        self.assertEqual(len(self.launches), 2, 'critical does not')

    def test_cooldown_is_per_drive(self):
        self.cfg['disk_cleanup']['cooldown_s'] = 6 * 3600
        self.cfg['trackers'].append(dict(self.cfg['trackers'][0], name='other'))
        self.activate()
        disk_state.update(config.state_dir(self.cfg), lambda s: s.setdefault('episodes', {}).setdefault(
            'test-drive', {}).update(last_cleanup=self.now - 60))
        self.free = 60
        self.tick_gated(pressure('go'))
        episodes = disk_state.read(config.state_dir(self.cfg))['episodes']
        self.assertIsNotNone(episodes['other'].get('claim') or episodes['other'].get('last_cleanup'))
        self.assertIsNone(episodes['test-drive'].get('claim'))


if __name__ == '__main__':
    unittest.main()
