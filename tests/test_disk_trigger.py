"""Durable falling-space episodes through the cheap public tracker seam."""
import concurrent.futures
import threading
import os
import subprocess
from unittest.mock import patch

from tests.disk_fixture import DiskCase


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
        disk_schedule.install(self.cfg, apply=True,
                              runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 0, '', ''))

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
            self.assertEqual(stages, [1, 2, 3, 4])

    def test_duplicate_readers_and_failed_launch_do_not_lose_trigger(self):
        from drover import disk
        self.activate()
        launches = []
        entered, release = threading.Event(), threading.Event()
        def enqueue(*args, **kwargs):
            launches.append(1)
            entered.set()
            self.assertTrue(release.wait(5))
            return self.complete()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.tick, enqueue)
            self.assertTrue(entered.wait(5))
            second = pool.submit(self.tick, enqueue)
            try:
                value = second.result(timeout=5)
                self.assertIn('drives', value)
                self.assertEqual(len(launches), 1)
            finally:
                release.set()
            first.result(timeout=5)
        # A distinct falling episode must survive a failed detach.
        self.free = 150
        self.tick(enqueue)
        self.free = 149
        def fail(*args, **kwargs):
            raise OSError('fixture detach refused')
        try:
            self.tick(fail)
        except OSError:
            pass
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
        cfg = {**self.cfg, 'test_output': str(target)}
        def detach(argv, env, log):
            shim = ('import sys,types,json;'
                    'm=types.ModuleType("drover.disk_cleanup");'
                    'm.run=lambda cfg,**kw: open(cfg["test_output"],"w").write(json.dumps([cfg,kw]));'
                    'sys.modules["drover.disk_cleanup"]=m;')
            done = subprocess.run([*argv[:2], shim + argv[2], *argv[3:]], env=env,
                                  capture_output=True, text=True, timeout=10)
            self.assertEqual(done.returncode, 0, done.stderr)
            return SimpleNamespace(pid=os.getpid()), False
        with patch.object(placement, 'detach', side_effect=detach):
            placement.cleanup_enqueue(cfg, {'id': 'literal-token', 'applied': True})
        saved, kwargs = json.loads(target.read_text())
        self.assertEqual(saved, cfg)
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
        slow = subprocess.TimeoutExpired('powershell.exe', 15)
        with patch('drover.placement.cleanup_identity', side_effect=slow) as _placement_patch_5, patch('drover.placement_process.cleanup_identity', _placement_patch_5), patch('drover.placement_proof.cleanup_identity', _placement_patch_5):
            result = disk.tick(self.cfg, now=self.now, reader=lambda path: 60,
                               enqueue=lambda: launches.append(1) or {})
        self.assertEqual(launches, [])
        self.assertEqual(result['drives'][0]['level'], 'queue')

    def test_timeout_checking_a_prior_claim_also_degrades(self):
        from drover import disk, disk_state
        disk_state.update(self.state, lambda s: s.update(episodes={'test-drive': {'claim': dict(
            id='c1', identity=dict(pid=5, birth='b', side=self.side), applied=False)}}))
        with patch('drover.placement.cleanup_identity', side_effect=subprocess.TimeoutExpired('p', 15)) as _placement_patch_6, patch('drover.placement_process.cleanup_identity', _placement_patch_6), patch('drover.placement_proof.cleanup_identity', _placement_patch_6):
            result = disk.tick(self.cfg, now=self.now, reader=lambda path: 60, enqueue=lambda: {})
        self.assertIn('drives', result)
