"""The budget purge's fixture: a memory worktree case with a chosen WSL size."""
import json
from unittest.mock import patch

from drover import disk, disk_budget, disk_state, placement, placement_proof
from tests.disk_fixture import GIB
from tests.disk_memory import MemoryWorktreeCase


HOUR = 3600
DAY = 86400
SIZE = 1000
BUDGET = dict(target_gb=225.0, windows_paths=[])


def row(path, age_s, size=SIZE, **extra):
    return dict(path=path, age_s=age_s, bytes=size, action='candidate', **extra)


class BudgetCase(MemoryWorktreeCase):
    def setUp(self):
        super().setUp()
        self.cfg['trackers'][0].update(wsl_combined=True, host_floor_gb=10.0)
        self.free = 200.         # H: is healthy unless a test says otherwise
        self.outputs = []
        self.allowed = 0         # bytes of output that may stay at the target
        self.ttls = []
        self.clock = 0.
        self.read_cost = self.clock_step = 0.
        self.after_read = None
        # The coordinator's Windows snapshot is real down to the CIM query; a harmless editor elsewhere by default.
        self.windows_rows = [dict(pid=7, name='editor.exe', same_user=True, cwd=r'C:\work', cmd='editor.exe')]
        self.windows_failure = None

        def cim(request, current):
            return self.windows_failure or dict(rows=list(self.windows_rows), identities=current)

        def clock():
            self.clock += self.clock_step
            return self.clock
        for context in (patch.object(placement, 'wsl_used_gb', side_effect=self.reading),
                        patch.object(placement_proof, 'IS_WINDOWS', True),
                        patch.object(placement_proof, '_windows_processes',
                                     side_effect=lambda: {str(r['pid']): (r['name'], 1) for r in self.windows_rows}),
                        patch.object(placement_proof, '_cleanup_cim', side_effect=cim),
                        patch.object(disk_budget, '_clock', side_effect=clock)):
            context.start()
            self.addCleanup(context.stop)

    def reading(self, distro, up=True, *, ttl=60):
        self.ttls.append(ttl)
        self.clock += self.read_cost
        if self.after_read:
            self.after_read(len(self.ttls))
        left = sum(p.stat().st_size for p in self.outputs if p.exists())
        return 225 + (left - self.allowed) / GIB

    def output(self, name, idle_s, size=SIZE):
        tree = self.worktree(name)
        path = tree / 'generated' / 'result'
        path.parent.mkdir()
        path.write_bytes(b'x' * size)
        self.age(tree, idle_s)
        self.outputs.append(path)
        return path

    def claim(self, applied=True):
        def seed(state):
            state.setdefault('episodes', {})['wsl'] = dict(claim=dict(
                id='wsl-claim', identity=dict(pid=1, birth='x', side=self.side), applied=applied, at=self.now),
                last_cleanup=self.now)
        disk_state.update(self.state, seed)

    def episode(self):
        return disk_state.read(self.state)['episodes']['wsl']

    def receipt(self):
        return json.loads((self.state / 'disk' / 'result-wsl-claim.json').read_text(encoding='utf-8'))

    def purge(self):
        self.approve_report()
        self.claim()
        return self.run_cleanup(apply=True, mode='triggered')


class LocalSide(BudgetCase):
    """The whole path on this machine's own side: run, worker, serve, purge and the shared removal."""
    def setUp(self):
        super().setUp()
        side = patch.object(disk_budget, 'SIDE', self.side)
        side.start()
        self.addCleanup(side.stop)


def live_row(pid, name, session):
    """A CIM row exactly as this machine's non-elevated hub gets it for a process it cannot open."""
    return dict(pid=pid, name=name, session_id=session, exe=None, command_unreadable=True, same_user=None, cmd='')
