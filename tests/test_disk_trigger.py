"""Durable falling-space episodes through the cheap public tracker seam."""
import concurrent.futures
import threading
import os
import subprocess
from unittest.mock import patch

from tests.disk_fixture import DiskCase
from tests.fakeci import answer_spawns
from drover import config as _config, disk_runtime

REAL_GET = _config.get      # bound before any test patches it


class Trigger(DiskCase):
    def setUp(self):
        super().setUp()
        mapping = patch('drover.placement.physical_disk_path', return_value=str(self.root))
        mapping.start()
        self.addCleanup(mapping.stop)
        self.cfg['disk_cleanup']['cooldown_s'] = 0   # cooldown has its own tests in test_cleanup_throttle

    def activate(self):
        from drover import disk_schedule
        context = patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.root / 'xdg'),
                                         'WSL_DISTRO_NAME': '', 'WSL_INTEROP': ''})
        context.start()
        self.addCleanup(context.stop)
        self.approve_report()
        answer_spawns(self, lambda argv, **kw: subprocess.CompletedProcess(argv, 0, '', ''))
        disk_schedule.install(self.cfg, apply=True)

    def tick(self, enqueue):
        from drover import disk
        return disk.tick(self.cfg, now=self.now, reader=lambda path: self.free, enqueue=enqueue)

    def complete(self, *args, **kwargs):
        from drover import disk_cleanup
        return disk_cleanup.run(self.cfg, mode='triggered', apply=True, runtime=self.runtime())

    def test_no_reclean_without_meaningful_change(self):
        from drover import disk
        launches = []
        def enqueue(*args, **kwargs):
            launches.append((args, kwargs))
            if len(launches) == 1:
                self.free = 80  # Cleanup gains space: suppression baseline is this post-read, not 60.
            return self.complete()
        self.activate()
        self.free = 60
        self.tick(enqueue)
        self.assertEqual(len(launches), 1)
        for free in (80, 75, 70.01):
            self.free = free
            self.now += 900
            self.tick(enqueue)
            self.assertEqual(len(launches), 1, 'drop is measured from post-cleanup 80 GiB')
        self.free = 70
        self.tick(enqueue)
        self.assertEqual(len(launches), 2)
        self.tick(enqueue)
        self.assertEqual(len(launches), 2)
        self.assertEqual(disk.admission(disk.read(self.cfg, reader=lambda p: self.free))['verdict'], 'queue')

    def test_recovery_rearms_unknown_does_not_and_daily_still_runs(self):
        from drover import disk_cleanup
        launches = []
        def enqueue(*args, **kwargs):
            launches.append(1)
            return self.complete()
        self.activate()
        self.tick(enqueue)
        for free in (None, 80):
            self.free = free
            self.tick(enqueue)
        self.assertEqual(len(launches), 1)
        self.free = 150
        self.tick(enqueue)
        self.assertEqual(len(launches), 1)
        self.free = 149
        self.tick(enqueue)
        self.assertEqual(len(launches), 2)
        for free in (80, 180):
            self.free = free
            self.calls.clear()
            disk_cleanup.run(self.cfg, mode='daily', apply=True, runtime=self.runtime())
            stages = sorted({req['stage'] for _, req in self.calls})
            self.assertEqual(stages, [1, 2, 3, 4, 5])

    def test_duplicate_readers_and_failed_launch_do_not_lose_trigger(self):
        from drover import disk, disk_state
        self.activate()
        launches = []
        entered, release = threading.Event(), threading.Event()
        def enqueue(*args, **kwargs):
            launches.append(1)
            entered.set()
            self.assertTrue(release.wait(60))
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.tick, enqueue)
            try:
                self.assertTrue(entered.wait(60))
                second = pool.submit(self.tick, enqueue)
                value = second.result(timeout=60)
                self.assertIn('drives', value)
                self.assertEqual(len(launches), 1)
            finally:
                release.set()
            first.result(timeout=60)
        self.assertEqual(len(launches), 1)
        # Complete the claim through its receipt, without timing a real cleanup run.
        claim = disk_state.read(self.state)['episodes']['test-drive']['claim']
        disk_state.save_file(self.state, 'result-' + claim['id'] + '.json',
                             dict(applied=claim['applied'], free=self.free, status='completed'))
        # A distinct falling episode must survive a failed detach.
        self.free = 150
        self.tick(enqueue)
        self.free = 149
        def fail(*args, **kwargs):
            raise OSError('fixture detach refused')
        with self.assertRaisesRegex(OSError, 'fixture detach refused'):
            self.tick(fail)
        self.tick(lambda *a, **kw: launches.append(1))
        self.assertEqual(len(launches), 2)
        self.assertEqual(disk.read(self.cfg, reader=lambda p: self.free)[0]['free_gb'], 149)

    def test_critical_persists_while_hub_is_down(self):
        from drover import disk, disk_state
        self.free = 24.9
        # A busy ordinary state writer must not delay or lose the critical token.
        with patch.object(disk_state, 'update', side_effect=BlockingIOError('busy')):
            result = self.tick(lambda *a, **kw: None)
        self.assertTrue(result['pause'])
        self.assertTrue(any('critical' in p.name for p in self.state.rglob('*') if p.is_file()))
        self.free = 180
        result = self.tick(lambda *a, **kw: self.fail('healthy reader launched cleanup'))
        self.assertEqual(disk.admission(disk.read(self.cfg, reader=lambda p: self.free))['verdict'], 'go')
        self.assertTrue(result['pause'], 'healthy capacity does not authorize resuming the hub')

    def test_default_enqueue_executes_composed_command(self):
        import json
        from types import SimpleNamespace
        from drover import placement
        target = self.root / "literal ' command.json"
        (self.state / 'config.toml').write_text('[disk_cleanup]\ncooldown_s = 123\n', encoding='utf-8')
        def detach(argv, env, log):
            shim = ('import sys,types,json,os;'
                    'm=types.ModuleType("drover.disk_cleanup");'
                    'm.run=lambda cfg,**kw: open(os.environ["DROVER_TEST_OUTPUT"],"w").write(json.dumps([cfg,kw]));'
                    'sys.modules["drover.disk_cleanup"]=m;')
            done = subprocess.run([*argv[:2], shim + argv[2], *argv[3:]], env=dict(env, DROVER_TEST_OUTPUT=str(target)),
                                  capture_output=True, text=True, timeout=30)
            self.assertEqual(done.returncode, 0, done.stderr)
            return SimpleNamespace(pid=os.getpid()), False
        with patch.object(placement, 'detach', side_effect=detach):
            disk_runtime.cleanup_enqueue(self.cfg, {'id': 'literal-token', 'applied': True})
        saved, kwargs = json.loads(target.read_text())
        self.assertEqual(saved['disk_cleanup']['cooldown_s'], 123)     # its own read of config.toml, not the hub's
        self.assertEqual(kwargs, {'mode': 'triggered', 'apply': True})

    def test_two_low_drives_share_one_machine_cleanup(self):
        from drover import disk_state
        self.approve_report()
        self.cfg['trackers'].append({**self.cfg['trackers'][0], 'name': 'second-drive'})
        self.approve_report()
        disk_state.update(self.state, lambda s: s.update(automation={'apply': True}))
        launches = []
        def enqueue():
            launches.append(1)
            return self.complete()
        self.tick(enqueue)
        self.assertEqual(launches, [1])

    def test_result_survives_busy_episode_writer(self):
        from drover import disk, disk_cleanup, disk_state
        self.approve_report()
        disk_state.update(self.state, lambda s: s.update(automation={'apply': True}))
        launches = []
        def enqueue():
            launches.append(1)
            with patch.object(disk_state, 'update', side_effect=BlockingIOError('busy')):
                return self.complete()
        self.tick(enqueue)
        self.assertTrue(list((self.state / 'disk').glob('result-*.json')))
        self.tick(lambda: self.fail('durable result was not reconciled'))
        self.assertEqual(launches, [1])

    def test_child_identity_survives_busy_attach(self):
        from drover import disk_state, placement
        self.approve_report()
        disk_state.update(self.state, lambda s: s.update(automation={'apply': True}))
        original = disk_state.update
        count = []
        def busy_attach(*args):
            count.append(1)
            if len(count) == 2:
                raise BlockingIOError('attach busy')
            return original(*args)
        child = dict(pid=123456, birth='child', side='wsl')
        with patch.object(disk_state, 'update', side_effect=busy_attach):
            self.tick(lambda: {'identity': child})
        launches = []
        with patch.object(placement, 'cleanup_claim_alive', side_effect=lambda identity: identity != child):
            self.tick(lambda: launches.append(1))
        self.assertEqual(launches, [1])

    def test_windows_absent_pid_is_proven_dead(self):
        from drover import placement
        # PID12 can be this interpreter inside a sandbox; discard its actual-side cache.
        with patch.object(placement, 'IS_WINDOWS', True) as _placement_patch_1, patch('drover.placement_exec.IS_WINDOWS', _placement_patch_1), patch('drover.placement_git.IS_WINDOWS', _placement_patch_1), patch('drover.placement_host.IS_WINDOWS', _placement_patch_1), patch('drover.placement_paths.IS_WINDOWS', _placement_patch_1), patch('drover.placement_process.IS_WINDOWS', _placement_patch_1), patch('drover.placement_proof.IS_WINDOWS', _placement_patch_1), patch('drover.placement_tree.IS_WINDOWS', _placement_patch_1), patch('drover.placement_worker.IS_WINDOWS', _placement_patch_1), \
                patch('drover.placement_process._SELF_IDENTITY', None), \
                patch.object(placement, '_read_windows_birth', side_effect=ProcessLookupError(87, 'absent')) as birth, patch('drover.placement_process._read_windows_birth', birth):
            self.assertFalse(placement.cleanup_claim_alive(dict(pid=12, birth='old', side='windows')))
        birth.assert_called_once_with(12)


class LockBackoff(DiskCase):
    """A wedged cleanup lock must not spawn a doomed triggered run on every tick."""

    def setUp(self):
        super().setUp()
        mapping = patch('drover.placement.physical_disk_path', return_value=str(self.root))
        mapping.start()
        self.addCleanup(mapping.stop)

    tick = Trigger.tick

    def plant(self, **over):
        import json
        import socket
        lock = self.state / 'disk-cleanup.lock'
        lock.mkdir()
        owner = dict(token='wedge-token', pid=424242, host=socket.gethostname(), created=1.0,
                     side=self.other_side, birth='x')
        owner.update(over)
        (lock / 'owner.json').write_text(json.dumps(owner), encoding='utf-8')
        return lock

    def outbox(self):
        from drover import disk_state
        return disk_state.read(self.state).get('outbox', {})

    def test_wedged_lock_suppresses_launch_then_warns_once(self):
        launches = []
        lock = self.plant()
        self.free = 60
        self.tick(lambda: launches.append(1) or {})
        self.assertEqual(launches, [])
        self.assertEqual(self.outbox(), {})  # not yet past the back-off
        self.now += 31 * 60
        self.tick(lambda: launches.append(1) or {})
        self.tick(lambda: launches.append(1) or {})
        self.assertEqual(launches, [])
        notices = [e['notice'] for e in self.outbox().values()]
        self.assertEqual(len(notices), 1)
        self.assertIn('disk-cleanup.lock', notices[0]['body'])
        self.assertIn('wedge-token', notices[0]['body'])
        self.assertIn('424242', notices[0]['body'])
        # Once the lock is cleared the next tick launches again.
        (lock / 'owner.json').unlink()
        lock.rmdir()
        self.tick(lambda: launches.append(1) or {})
        self.assertEqual(launches, [1])

    def test_notice_is_withdrawn_once_the_lock_clears(self):
        lock = self.plant()
        self.free = 60
        self.tick(lambda: {})
        self.now += 31 * 60
        self.tick(lambda: {})
        self.assertEqual(len(self.outbox()), 1)
        (lock / 'owner.json').unlink()
        lock.rmdir()
        self.tick(lambda: {})
        self.assertEqual(self.outbox(), {})  # delivered nothing yet, so it simply disappears

    def test_delivered_notice_question_is_closed_when_the_lock_clears(self):
        from drover import disk_state
        from drover.ledger import connect, ops, disk_notices
        conn = connect(self.root / 'hub.db')
        self.addCleanup(conn.close)
        lock = self.plant()
        self.free = 60
        self.tick(lambda: {})
        self.now += 31 * 60
        self.tick(lambda: {})
        disk_notices.replay(conn, self.cfg, self.now)
        self.assertEqual(len(ops.open_questions(conn)), 1)
        (lock / 'owner.json').unlink()
        lock.rmdir()
        self.tick(lambda: {})
        disk_notices.replay(conn, self.cfg, self.now)
        self.assertEqual(ops.open_questions(conn), [])
        self.assertEqual(self.outbox(), {})

    def test_reclaimable_lock_does_not_block_launch(self):
        launches = []
        self.plant(side=self.side, birth='boot:1')
        self.free = 60
        ident = lambda pid=None: dict(pid=pid, birth='absent', side=self.side)
        with patch('drover.placement.cleanup_identity', ident) as _placement_patch_4, patch('drover.placement_process.cleanup_identity', _placement_patch_4), patch('drover.placement_proof.cleanup_identity', _placement_patch_4):
            self.tick(lambda: launches.append(1) or {})
        self.assertEqual(launches, [1])


class ProcessQueryTimeout(DiskCase):
    def setUp(self):
        super().setUp()
        mapping = patch('drover.placement.physical_disk_path', return_value=str(self.root))
        mapping.start()
        self.addCleanup(mapping.stop)

    def test_timeout_skips_the_disk_step_instead_of_failing_the_tick(self):
        from drover import disk
        launches = []
        slow = TimeoutError('identity read timed out')
        with patch('drover.placement.cleanup_identity', side_effect=slow) as _placement_patch_5, patch('drover.placement_process.cleanup_identity', _placement_patch_5), patch('drover.placement_proof.cleanup_identity', _placement_patch_5):
            result = disk.tick(self.cfg, now=self.now, reader=lambda path: 60,
                               enqueue=lambda: launches.append(1) or {})
        self.assertEqual(launches, [])
        self.assertEqual(result['drives'][0]['level'], 'queue')

    def test_timeout_checking_a_prior_claim_also_degrades(self):
        from drover import disk, disk_state
        disk_state.update(self.state, lambda s: s.update(episodes={'test-drive': {'claim': dict(
            id='c1', identity=dict(pid=5, birth='b', side=self.side), applied=False)}}))
        with patch('drover.placement.cleanup_identity', side_effect=TimeoutError('identity read timed out')) as _placement_patch_6, patch('drover.placement_process.cleanup_identity', _placement_patch_6), patch('drover.placement_proof.cleanup_identity', _placement_patch_6):
            result = disk.tick(self.cfg, now=self.now, reader=lambda path: 60, enqueue=lambda: {})
        self.assertIn('drives', result)


class BuildRootCadence(DiskCase):
    """The hub launches a build-roots run on its own interval, at any drive level and outside the cleanup cooldown."""
    def setUp(self):
        super().setUp()
        mapping = patch('drover.placement.physical_disk_path', return_value=str(self.root))
        mapping.start()
        self.addCleanup(mapping.stop)
        self.cfg['disk_cleanup']['build_roots'] = [dict(path=str(self.root / 'targets'), placement='windows',
                                                        max_idle_hours=2)]
        self.free = 500.        # level ok: no triggered cleanup is due
        self.launches, self.builds = [], []
        from datetime import datetime
        self.now = datetime(2033, 5, 18, 12, 0).timestamp()      # local noon: outside the 02:30 to 04:00 quiet hours

    def tick(self):
        from drover import disk
        return disk.tick(self.cfg, now=self.now, reader=lambda path: self.free, wsl_used=lambda: None,
                         enqueue=lambda: self.launches.append(1) or {},
                         build_enqueue=lambda: self.builds.append(self.now) or {})

    def test_launches_when_the_interval_has_passed_and_not_again_before(self):
        from drover import disk_state
        self.tick()
        self.assertEqual(self.builds, [self.now])
        self.assertEqual(disk_state.read(self.state)['build_roots']['last_start'], self.now)
        for advance in (60, 3000, 539):
            self.now += advance
            self.tick()
        self.assertEqual(len(self.builds), 1, 'the default interval is 3600 s')
        self.now += 1
        self.tick()
        self.assertEqual(len(self.builds), 2)
        self.assertEqual(self.launches, [])

    def test_the_interval_is_configurable(self):
        self.cfg['disk_cleanup']['build_root_interval_s'] = 600
        self.tick()
        self.now += 600
        self.tick()
        self.assertEqual(len(self.builds), 2)

    def test_no_launch_from_half_past_two_to_four_local_so_the_daily_apply_finds_the_lock_free(self):
        from datetime import datetime
        for hour, minute in ((2, 30), (3, 0), (3, 59)):
            self.now = datetime(2033, 5, 18, hour, minute).timestamp()       # naive: local time
            self.tick()
        self.assertEqual(self.builds, [])
        for hour, minute in ((2, 29), (4, 0)):
            with self.subTest(at=(hour, minute)):
                self.builds.clear()
                from drover import disk_state
                disk_state.update(self.state, lambda s: s.pop('build_roots', None))
                self.now = datetime(2033, 5, 18, hour, minute).timestamp()
                self.tick()
                self.assertEqual(self.builds, [self.now])

    def test_the_hub_tick_launches_an_applied_run(self):
        from drover import disk
        launched = []
        with patch.object(disk_runtime, 'cleanup_enqueue', lambda cfg, claim, mode='triggered':
                          launched.append((claim['applied'], mode)) or {}):
            disk.tick(self.cfg, now=self.now, reader=lambda path: self.free, wsl_used=lambda: None,
                      enqueue=lambda: {})
        self.assertEqual(launched, [(True, 'build-roots')])

    def test_a_command_line_tick_applies_only_with_apply_and_automation(self):
        from drover import disk, disk_state
        cases = ((['tick', '--dry-run'], True, False), (['tick'], False, False), (['tick'], True, True),
                 (['tick', '--apply'], False, False), (['tick', '--apply'], True, True), (['tick', '--dry-run'], False, False))
        for argv, automation, applied in cases:
            with self.subTest(argv=argv, automation=automation):
                disk_state.update(self.state, lambda s: s.update(automation={'apply': automation}))
                launched = []
                def tick(cfg, enqueue=None, build_enqueue=None, **_):
                    build_enqueue()
                    return {}
                with patch.object(disk, 'tick', tick), patch('sys.stdout'),                         patch.object(disk_runtime, 'cleanup_enqueue', lambda cfg, claim, mode='triggered':
                                     launched.append((claim['applied'], mode)) or {}):
                    self.assertEqual(disk.main(argv), 0)
                self.assertEqual(launched, [(applied, 'build-roots')])

    def test_a_restart_does_not_relaunch_at_once(self):
        from drover import disk_state
        disk_state.update(self.state, lambda s: s.update(build_roots=dict(last_start=self.now - 60)))
        self.tick()
        self.assertEqual(self.builds, [])

    def test_no_launch_without_a_windows_build_root(self):
        self.cfg['disk_cleanup']['build_roots'] = [dict(path='/home/someone/targets', placement='wsl', max_idle_hours=2)]
        self.tick()
        del self.cfg['disk_cleanup']['build_roots']
        self.now += 7200
        self.tick()
        self.assertEqual(self.builds, [])

    def test_a_triggered_cleanup_waits_a_tick_behind_a_build_roots_launch(self):
        from drover import disk_state
        self.cfg['disk_cleanup']['cooldown_s'] = 0
        disk_state.update(self.state, lambda s: s.update(automation={'apply': False}))
        self.free = 120.        # warn: a triggered cleanup is due too
        self.tick()
        self.assertEqual((self.builds, self.launches), ([self.now], []), 'the build-roots run is taking the lock')
        self.now += 60
        self.tick()
        self.assertEqual(self.launches, [], 'its child may still be starting')
        self.now += 60
        self.tick()
        self.assertEqual((len(self.builds), self.launches), (1, [1]))

    def test_a_failed_launch_is_logged_recorded_and_retried_once_an_interval_without_raising(self):
        from drover import disk, disk_state
        failures = []
        def fail():
            failures.append(self.now)
            raise OSError('no python')
        def tick():
            return disk.tick(self.cfg, now=self.now, reader=lambda path: 120., wsl_used=lambda: None,
                             enqueue=lambda: self.launches.append(1) or {}, build_enqueue=fail)
        self.cfg['disk_cleanup']['cooldown_s'] = 0
        with self.assertLogs('drover.disk', 'WARNING'):
            result = tick()
        self.assertIn('drives', result)
        builds = disk_state.read(self.state)['build_roots']
        self.assertEqual(builds['error'], dict(at=self.now, error='no python'))
        self.assertEqual(self.launches, [1], 'a failed launch is no lock holder: the triggered cleanup is claimed')
        for _ in range(3):
            self.now += 900
            tick()
        self.assertEqual(len(failures), 1, 'no retry before the interval')
        self.now += 900
        tick()
        self.assertEqual(len(failures), 2)

    def test_a_start_in_the_future_is_due_at_once(self):
        from drover import disk_state
        disk_state.update(self.state, lambda s: s.update(build_roots=dict(last_start=self.now + 86400)))
        self.tick()
        self.assertEqual(self.builds, [self.now])

    def test_no_launch_off_windows(self):
        from drover import placement
        with patch.object(placement, 'cleanup_side', return_value='wsl'):
            self.tick()
        self.assertEqual(self.builds, [])

    def test_no_launch_during_a_compaction_hold(self):
        from drover import compaction_hold
        with patch.object(compaction_hold, 'active', return_value=True):
            self.tick()
        self.assertEqual(self.builds, [])

class DetachedConfig(DiskCase):
    """A detached run reads config.toml itself, so it sees an edit made after the hub loaded its config."""
    def test_the_detached_run_reads_a_config_change_made_after_the_hub_loaded_it(self):
        import json
        import sys
        from types import SimpleNamespace
        from drover import config, disk_cleanup, placement
        home = self.root / 'home'
        home.mkdir()
        (home / 'config.toml').write_text('[disk_cleanup]\ncooldown_s = 100\n', encoding='utf-8')
        with patch.dict(os.environ, {'DROVER_HOME': str(home)}):
            hub = config.load()
            self.assertEqual(hub['disk_cleanup']['cooldown_s'], 100)
            launched = []
            with patch.object(placement, 'detach', lambda argv, env, stream: launched.append(argv) or
                              (SimpleNamespace(pid=4242), False)), \
                    patch.object(placement, 'cleanup_identity', return_value=dict(pid=4242, birth='b', side=self.side)):
                disk_runtime.cleanup_enqueue(hub, dict(id='c1', applied=False))
            [argv] = launched
            self.assertFalse(any('cooldown_s' in arg for arg in argv), 'the hub config is not passed')
            (home / 'config.toml').write_text('[disk_cleanup]\ncooldown_s = 200\n', encoding='utf-8')
            ran = []
            code_args = argv[3:]
            self.addCleanup(config.reset)
            with patch.object(config, 'get', REAL_GET), \
                    patch.object(placement, 'lower_priority'), patch.object(placement, 'limit_cpus'), \
                    patch.object(disk_cleanup, 'run', lambda cfg, **kw: ran.append((cfg, kw))):
                disk_runtime.run_detached(code_args[1] == '1', code_args[2])
        [(cfg, options)] = ran
        self.assertEqual(cfg['disk_cleanup']['cooldown_s'], 200)
        self.assertEqual(options, dict(mode='triggered', apply=False))

    def test_a_build_roots_launch_names_its_mode(self):
        from types import SimpleNamespace
        from drover import placement
        launched = []
        with patch.object(placement, 'detach', lambda argv, env, stream: launched.append(argv) or
                          (SimpleNamespace(pid=4242), False)), \
                patch.object(placement, 'cleanup_identity', return_value=dict(pid=4242, birth='b', side=self.side)):
            disk_runtime.cleanup_enqueue(self.cfg, dict(id='build-roots', applied=True), mode='build-roots')
        [argv] = launched
        self.assertEqual(argv[4:6], ['1', 'build-roots'])


class BuildRootsLock(DiskCase):
    """A build-roots run names its mode in the lock's owner record. A daily or triggered run waits up to 30 minutes
    for such a holder; any other holder still fails it at once. A tick claims no triggered cleanup while a
    build-roots run holds the lock or was launched moments ago, in this process or another."""
    def setUp(self):
        super().setUp()
        mapping = patch('drover.placement.physical_disk_path', return_value=str(self.root))
        mapping.start()
        self.addCleanup(mapping.stop)
        self.clock = [0.0]
        self.slept = []

    def held(self, mode):
        from drover import disk_state
        lock = disk_state.CleanupLock(self.state, mode=mode)
        lock.__enter__()
        self.addCleanup(lambda: lock.acquired and lock.__exit__(None, None, None))
        return lock

    def waiting(self, release_after=None):
        """Patch the lock's clock and sleep: each sleep moves the clock on; the holder is released after `release_after` s."""
        from drover import disk_state
        def sleep(seconds):
            self.slept.append(seconds)
            self.clock[0] += seconds
            if release_after is not None and self.clock[0] >= release_after and self.holder.acquired:
                self.holder.__exit__(None, None, None)
        return patch.multiple(disk_state.CleanupLock, _sleep=staticmethod(sleep),
                              _clock=staticmethod(lambda: self.clock[0]))

    def test_the_owner_record_names_the_mode(self):
        import json
        self.held('build-roots')
        owner = json.loads((self.state / 'disk-cleanup.lock' / 'owner.json').read_text(encoding='utf-8'))
        self.assertEqual(owner['mode'], 'build-roots')

    def test_a_daily_run_waits_for_a_build_roots_holder(self):
        self.holder = self.held('build-roots')
        with self.waiting(release_after=600):
            result = self.run_cleanup(mode='daily', real=False)
        self.assertEqual(result['mode'], 'daily')
        self.assertGreaterEqual(self.clock[0], 600)
        self.assertLess(self.clock[0], 1800)

    def test_a_triggered_run_waits_too_but_not_past_thirty_minutes(self):
        self.holder = self.held('build-roots')
        with self.waiting(), self.assertRaises(BlockingIOError):
            self.run_cleanup(mode='triggered', real=False)
        self.assertGreaterEqual(self.clock[0], 1800)
        self.assertLess(self.clock[0], 1800 + 60)

    def test_any_other_holder_fails_a_daily_run_at_once(self):
        self.holder = self.held('triggered')
        with self.waiting(), self.assertRaises(BlockingIOError):
            self.run_cleanup(mode='daily', real=False)
        self.assertLess(self.clock[0], 5)

    def triggered_due(self):
        from drover import disk, disk_state
        self.cfg['disk_cleanup']['cooldown_s'] = 0
        self.cfg['disk_cleanup']['build_roots'] = [dict(path=str(self.root / 'targets'), placement='windows',
                                                        max_idle_hours=2)]
        launches = []
        def tick():
            disk.tick(self.cfg, now=self.now, reader=lambda path: 120., wsl_used=lambda: None,
                      enqueue=lambda: launches.append(1) or {}, build_enqueue=lambda: {})
        return launches, tick

    def test_a_tick_claims_nothing_while_a_build_roots_run_holds_the_lock(self):
        from drover import disk_state
        launches, tick = self.triggered_due()
        disk_state.update(self.state, lambda s: s.update(build_roots=dict(last_start=self.now - 600)))
        self.holder = self.held('build-roots')
        tick()
        self.assertEqual(launches, [])
        self.assertIsNone(disk_state.read(self.state).get('episodes', {}).get('test-drive', {}).get('claim'))
        self.holder.__exit__(None, None, None)
        self.now += 60
        tick()
        self.assertEqual(launches, [1])

    def test_a_tick_in_another_process_claims_nothing_while_the_child_is_starting(self):
        from drover import disk_state
        launches, tick = self.triggered_due()
        # another process launched a build-roots run 30 s ago; its child has not taken the lock yet
        disk_state.update(self.state, lambda s: s.update(build_roots=dict(last_start=self.now - 30)))
        tick()
        self.assertEqual(launches, [])
        self.now += 120
        tick()
        self.assertEqual(launches, [1])
