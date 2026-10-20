"""Paused dispatch still observes physical capacity and durable offline notices."""
from unittest.mock import patch

from tests.disk_fixture import DiskCase
from tests.dispatch_fixture import FakePipeline, Observations, make_dispatcher
from tests.test_dispatcher import FakeLauncher


class Hub(DiskCase):
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
        task = ops.create_task(conn, 'capacity held task', 'p')
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
