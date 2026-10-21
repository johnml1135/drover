"""Paused dispatch still observes physical capacity and durable offline notices."""
import json
from unittest.mock import patch

from tests.disk_fixture import DiskCase
from tests.dispatch_fixture import FakePipeline, Observations, make_dispatcher
from tests.test_dispatcher import FakeLauncher


class Hub(DiskCase):
    def setUp(self):
        super().setUp()
        mapping = patch('drover.placement.physical_disk_path', return_value=str(self.root))
        mapping.start()
        self.addCleanup(mapping.stop)

    def test_cleanup_receives_hub_snapshot_with_state_flags(self):
        from drover import disk_cleanup
        from drover.ledger import ops, states
        from hub.store import Store
        from hub.services import Services
        from hub.settings import Settings
        from tests.test_hub_ops import FakeTuner
        store = Store(self.root / 'snapshot.db')
        self.addCleanup(store.close)
        cancelled = ops.create_task(store.conn, 'archived terminal task', 'p')
        ops.set_status(store.conn, cancelled['id'], states.CANCELLED, actor='owner')
        ops.archive_task(store.conn, cancelled['id'])
        active = ops.create_task(store.conn, 'live task', 'p')
        ops.approve(store.conn, active['id'])
        ops.claim_next(store.conn, {})
        ops.update_task(store.conn, active['id'], worktree=str(self.root / 'offline-live-tree'))
        svc = Services(store, Settings(runs_dir=self.root / 'runs', use_tailscale=False),
                       wsl=lambda *a: (1, ''), tuner_mod=FakeTuner(False),
                       disks=lambda: {}, ollama=lambda *a: False, clock=lambda: self.now)
        with patch('drover.ownership.read', side_effect=OSError('owning side unavailable')):
            svc.tick()
        self.calls.clear()
        disk_cleanup.run(self.cfg, runtime=self.runtime())
        tasks = self.calls[0][1]['tasks']
        self.assertEqual(tasks['at'], self.now)
        by_id = {t['id']: t for t in tasks['tasks']}
        self.assertEqual(by_id[cancelled['id']]['state'], states.CANCELLED)
        self.assertTrue(by_id[cancelled['id']]['terminal'])
        self.assertFalse(by_id[cancelled['id']]['live'])
        self.assertEqual(by_id[active['id']]['state'], states.CLAIMED)
        self.assertFalse(by_id[active['id']]['terminal'])
        self.assertTrue(by_id[active['id']]['live'])

    def make_hub(self):
        from drover.ledger import connect, ops
        from drover.port import DirectPort
        conn = connect(self.root / 'hub.db')
        self.addCleanup(conn.close)
        port = DirectPort(conn)
        pipe = FakePipeline(port, self.root / 'runs')
        launcher = FakeLauncher()
        observed = Observations(conn, lambda: self.now, lambda b: {'remaining_pct': 90}, lambda b: True)
        reads = []
        def resource_tick(**kwargs):
            from drover import disk, resources
            reads.append(self.free)
            fake = {'platform': 'wsl', 'load5': 0, 'cores': 4, 'ram_available_gb': 100,
                    'ollama_ram_gb': 0, 'disk_free_gb': {'test-drive': self.free},
                    'vram': None, 'read_only': False}
            # Fake the public disk reader, so even default resource composition is hermetic.
            real_read, real_tick = disk.read, disk.tick
            with patch.object(disk, 'read', side_effect=lambda cfg=None, **kw: real_read(self.cfg, reader=lambda p: self.free)), \
                 patch.object(disk, 'tick', side_effect=lambda cfg=None, **kw: real_tick(self.cfg, now=self.now, reader=lambda p: self.free, enqueue=lambda *a, **kw: None)):
                return resources.tick(config=self.cfg, fake=fake, **kwargs)
        dispatch = make_dispatcher(port, pipeline=pipe, launcher=launcher, resources=resource_tick,
                                   now=lambda: self.now, usage_poller=observed)
        return conn, dispatch, reads

    def test_reads_while_paused_and_releases_queue_hold(self):
        from drover import resources
        from drover.ledger import ops, states
        resources.tick  # NEW seam must fail naturally before any external work.
        conn, dispatch, reads = self.make_hub()
        ops.set_setting(conn, 'dispatch_enabled', 'false')
        task = ops.create_task(conn, 'capacity held task', 'p', read_access=[])
        ops.approve(conn, task['id'])
        self.free = 49
        out = dispatch.tick()
        self.assertTrue(out['paused'])
        self.assertIn(49, reads)
        self.assertEqual(ops.get_task(conn, task['id'])['status'], states.READY)
        ops.set_setting(conn, 'dispatch_enabled', 'true')
        for free in (80, 49):
            self.free = free
            self.now += 900
            self.assertEqual(dispatch.tick()['launched'], [])
            self.assertEqual(ops.get_task(conn, task['id'])['status'], states.READY)
        self.free = 180
        self.now += 900
        self.assertEqual(dispatch.tick()['launched'], [task['id']])
        self.assertEqual(ops.get_setting(conn, 'dispatch_enabled'), 'true')
        self.assertEqual(dispatch.port.waiting.pending(), [])

    def test_critical_sticky_pause_and_offline_notice_replay(self):
        from drover import disk, disk_cleanup, resources
        from drover.ledger import ops
        from hub.store import Store
        from hub.services import Services
        from hub.settings import Settings
        from tests.test_hub_ops import FakeTuner
        self.approve_report()
        self.free = 24
        offline = disk.tick(self.cfg, now=self.now, reader=lambda p: self.free, enqueue=lambda *a, **kw: None)
        self.assertTrue(offline['pause'])
        exhausted = disk_cleanup.run(self.cfg, mode='triggered', apply=True, runtime=self.runtime())
        self.assertTrue(exhausted['exhausted'])
        self.assertEqual(len(self.toasts), 1)
        conn, dispatch, reads = self.make_hub()
        ops.set_setting(conn, 'dispatch_enabled', 'true')
        self.free = 180  # Hub was offline for the entire critical interval.
        dispatch.tick()
        self.assertEqual(ops.get_setting(conn, 'dispatch_enabled'), 'false')
        self.assertEqual(dispatch.tick()['launched'], [])
        self.assertTrue(reads)
        published = []
        store = Store(self.root / 'hub.db', publish=lambda *a, **kw: published.append((a, kw)))
        self.addCleanup(store.close)
        svc = Services(store, Settings(runs_dir=self.root / 'runs', use_tailscale=False),
                       wsl=lambda *a: (1, ''), tuner_mod=FakeTuner(False), disks=lambda: {},
                       ollama=lambda *a: False, clock=lambda: self.now)
        with patch.object(disk, 'read', return_value=[{'name': 'test-drive', 'path': str(self.root),
                                                     'free_gb': 180, 'level': 'ok'}]):
            svc.tick()
            svc.tick()
        self.assertEqual(len(ops.events(store.conn, kind='disk.notice')), 1)
        self.assertEqual(len(ops.open_questions(store.conn)), 1)
        self.assertTrue(published, 'Inbox replay publishes a UI change after commit')
        self.assertEqual(ops.get_setting(store.conn, 'dispatch_enabled'), 'false')

    def test_snapshot_worktree_paths_are_translated_for_side_worker(self):
        import subprocess
        from drover import placement
        request = dict(stage=4, apply=False, state_dir=r'H:\state', repos=[], paths={},
                       tasks=dict(at=self.now, active_worktrees=[r'H:\trees\active'],
                                  tasks=[dict(id='T-1', state='irrelevant', terminal=False, live=True,
                                              worktree=r'H:\trees\active')]))
        captured = []
        def execute(argv, **kwargs):
            captured.append(json.loads(kwargs['input']))
            return subprocess.CompletedProcess(argv, 0, '{"items":[],"errors":[],"reclaimed_bytes":0}', '')
        with patch.object(placement, 'IS_WINDOWS', True), patch.object(subprocess, 'run', side_effect=execute):
            placement.cleanup_call('wsl', request)
        snapshot = captured[0]['tasks']
        self.assertEqual(snapshot['active_worktrees'], ['/mnt/h/trees/active'])
        self.assertEqual(snapshot['tasks'][0]['worktree'], '/mnt/h/trees/active')
