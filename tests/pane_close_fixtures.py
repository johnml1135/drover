"""Fixtures for tests.test_pane_close: a fake Herdr fleet, fake git answers and a fake process snapshot."""
from drover import pane_close, placement_proof
from drover.herdr_client import Herdr
from drover.ledger import ops_panes
from drover.port import DirectPort
from tests.fake_herdr import FakeHerdr, _result
from tests.test_ledger import Base

HOUR = 3600
NOW = 1_800_000_000.0
CLAUDE_READY = 'Work summary.\n\n❯\n\n────────────────\n  ⏵⏵ auto mode on\n'
CODEX_READY = 'Work summary.\n\n› \n'
DRAFT = 'Work summary.\n\n❯ half-typed follow-up\n'
DIALOG = 'Do you trust the files in this folder?\n1. Yes, continue\n2. No, exit\n'
REPO = 'H:/repos/dip-fixture'


def agent(pane, *, status='done', kind='claude', name='dir', seq=10, revision=1, terminal='t1', **fields):
    row = dict(pane_id=pane, agent=kind, agent_status=status, focused=False, name=name, cwd=REPO,
               foreground_cwd=REPO, state_change_seq=seq, revision=revision, terminal_id=terminal)
    row.update(fields)
    return row


class Git:
    """Answers `git` for one checkout from a table; `top` None is a folder outside any checkout."""

    def __init__(self, top=REPO, status='', merged=True, pushed=True):
        self.top, self.status, self.merged, self.pushed = top, status, merged, pushed
        self.calls = []

    def __call__(self, cwd, *args):
        self.calls.append(args)
        if args[0] == 'rev-parse' and '--show-toplevel' in args:
            return (0, self.top) if self.top else (128, '')
        if args[0] == 'status':
            return 0, self.status
        if args[:2] == ('rev-parse', '--verify'):
            return 0, 'abc'
        if '--symbolic-full-name' in args:
            return (0, 'origin/x') if self.pushed else (128, '')
        if args[0] == 'rev-list':
            return 0, '0'
        if args[0] == 'merge-base':
            return (0 if self.merged else 1), ''
        return 0, 'main'


class Fleet(FakeHerdr):
    """FakeHerdr plus `pane process-info` and a hook that runs on every `agent get`."""

    def __init__(self, *args, info=None, on_get=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.info = info or {}
        self.on_get = on_get

    def _agent(self, args):
        if self.on_get:
            self.on_get(self)
        return super()._agent(args)

    def _answer(self, args):
        if args[:2] == ['pane', 'process-info'] and not self.closed:
            info = self.info.get(args[3])
            return _result({'process_info': info}) if info else _result({})
        return super()._answer(args)

    def closes(self):
        return [c[2] for c in self.calls if c[:2] == ['pane', 'close']]


def snapshot(*rows, unknown=False):
    """A real placement proof over fake processes: (pid, cwd) pairs, all the invoking user's."""
    cim = [dict(pid=pid, name='node.exe', same_user=True, cwd=cwd, cmd=['node.exe']) for pid, cwd in rows]
    proof = placement_proof._CleanupProcesses(windows=True, cim=lambda: cim)
    proof.unknown = proof.unknown or unknown
    return lambda row: proof


class PaneCloseBase(Base):
    def setUp(self):
        super().setUp()
        self.port = DirectPort(self.conn)
        self.git = Git()
        self.pids = {'w1:p1': dict(shell_pid=100, foreground_processes=[dict(pid=101)])}
        self.processes = snapshot((101, REPO))

    def retired(self, pane='w1:p1', **over):
        fields = dict(pane_id=pane, kind='claude', seat='claude-main', placement='windows', started_by='drover',
                      terminal_id='t1', cwd=REPO, name='dir', state='retired')
        fields.update(over)
        return ops_panes.record_agent(self.conn, **fields)

    def herdr(self, agents=None, screens=None, **kwargs):
        agents = {'w1:p1': agent('w1:p1')} if agents is None else agents
        screens = {p: CLAUDE_READY for p in agents} if screens is None else screens
        fake = Fleet(agents, screens, {p: {'pane_id': p} for p in agents}, info=self.pids, **kwargs)
        self.fake = fake
        return Herdr('herdr', run=fake, clock=lambda: 0.0)

    def tick(self, herdr, minutes, **kwargs):
        kwargs.setdefault('run', self.git)
        kwargs.setdefault('processes', self.processes)
        kwargs.setdefault('apply', False)
        return pane_close.tick(self.port, herdr, NOW + minutes * 60, **kwargs)

    def verdict(self, herdr, minutes, **kwargs):
        return self.tick(herdr, minutes, **kwargs)[0]['verdict']

    def events(self, kind):
        return self.port.events(kind=kind)


