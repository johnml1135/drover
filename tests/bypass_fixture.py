"""A dispatcher whose launcher records what a yank asks of it."""
from drover.ledger import ops
from tests.test_dispatcher import AdoptLauncher, Base


class RecordingLauncher(AdoptLauncher):
    def __init__(self):
        super().__init__()
        self.cancelled, self.retried = [], []

    def cancel(self, run_id, nonce=None):
        self.cancelled.append((run_id, nonce))
        return True

    def stop_run(self, run_id, nonce=None):
        self.retried.append((run_id, nonce))
        return True


class YankBase(Base):
    def setUp(self):
        super().setUp()
        self.fl = RecordingLauncher()
        self.allow['codex'] = False
        self.found = {}
        self.d = self.restart()

    def restart(self):
        return self.make(launcher=self.fl, scan=lambda want_wsl: self.found, scan_every_s=0)

    def start(self):
        """A task with a worker running: the scan sees its runner until a test says otherwise."""
        tid = self.task(size='S')
        self.d.tick()
        run = self.run_of(tid)
        self.found[run['id']] = 'windows'
        return tid, run['id']

    def run_of(self, tid, role='worker'):
        return next(r for r in ops.list_runs(self.conn, tid) if r['role'] == role)

    def yank(self, tid):
        ops.yank(self.conn, tid, reason='stop')

    def nonce(self, rid):
        return next(e['data']['nonce'] for e in self.events('run-launching') if e['data']['run'] == rid)

    def assert_stopped(self, tid, rid):
        run = next(r for r in ops.list_runs(self.conn, tid) if r['id'] == rid)
        self.assertEqual(run['outcome'], 'yanked')
        self.assertIsNotNone(run['ended'])
        self.assertEqual(self.status(tid), 'yanked')
