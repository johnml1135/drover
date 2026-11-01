"""What the pane compaction tests share: a Codex director in a WSL pane, a fake that acts on what is typed, and a night's clock."""
import json
from unittest import mock

from drover import compaction_hold
from drover.ledger import ops_panes
from drover.panes import PaneService
from tests.pane_launch_support import LONE_SHELL, PROMPT
from tests.pane_send_support import IDLE, PANE, PASTE, Stage, WORKING, composing
from tests.panes_support import ROLE, Resident

CTRL_C = ['pane', 'send-keys', PANE, 'ctrl+c']
SESSION = 's1'


def paste(text, pane=PANE):
    return [*PASTE[:2], pane, f'\x1b[200~{text}\x1b[201~']


class Typing(Stage):
    """A pane that takes what is typed: a paste sits in the composer, and its Enter starts a turn or, with `settles`, ends one."""
    settles = False

    def _answer(self, args):
        if args[:2] == ['pane', 'send-text'] and args[2] in self.screens:
            self.screens[args[2]] = composing(args[3].removeprefix('\x1b[200~').removesuffix('\x1b[201~'))
        if args[:2] == ['pane', 'send-keys'] and args[3] == 'enter' and args[2] in self.agents:
            self.screens[args[2]] = IDLE if self.settles else WORKING
            self.agents[args[2]]['agent_status'] = 'idle' if self.settles else 'working'
        return super()._answer(args)


class Overnight(Resident):
    """A director up in its WSL pane, and the hold as the hub reads it. `at(s)` is s seconds into the hold."""

    def setUp(self):
        super().setUp()
        self.cfg['models']['ids']['gpt-6.1-sol'] = 'gpt-6.1-sol'
        self.notifications.conn.execute("UPDATE director_roles SET policy=json_set(policy,'$.allowed_models',json('[\"gpt-6.1-sol\"]'))")
        self.conn.commit()
        self.hold_ = None
        patcher = mock.patch.object(compaction_hold, 'active', lambda state_dir=None: self.hold_)
        patcher.start()
        self.addCleanup(patcher.stop)

    def configure(self, role):
        configs = json.loads(self.port.get_setting('director_execution', '{}'))
        configs[role] = {'backend': 'codex', 'model': 'gpt-6.1-sol', 'seat': 'seat-x', 'max_tokens': 20, 'placement': 'wsl',
                         'cwd': str(self.cwd)}
        self.port.set_setting('director_execution', json.dumps(configs))

    def turns(self, *args, **kw):
        """`Typing` plays the turn."""

    def scene(self, kind='codex', placement='wsl', **kw):
        fake = super().scene(kind, placement, **kw)
        fake.__class__ = Typing
        return fake

    def up(self):
        """Live, its first turn over, and working when the hold begins."""
        self.launched()
        self.since = self.time
        self.hold_ = dict(id='h1', since=self.time, at=self.time, step='stop', phase='draining')
        self.fake.calls.clear()
        return self.row

    def at(self, seconds):
        self.time = self.since + seconds
        self.answer = self.panes.compaction_tick(self.hold_)
        return self.answer

    def saved(self):
        return compaction_hold.read_panes(self.state)

    def agent(self):
        agent, = self.answer['agents']
        return agent

    def writes(self, pane=PANE):
        return [call for call in self.sends() if pane in call]

    def settle(self):
        self.fake.screens[PANE] = IDLE
        self.fake.agents[PANE]['agent_status'] = 'idle'

    def killed(self):
        """What `wsl --shutdown` leaves: the pane's shell, alone."""
        self.fake.agents.pop(PANE, None)
        self.fake.processes = LONE_SHELL
        self.fake.screens[PANE] = PROMPT

    def restarted(self):
        self.time = self.since + 900
        self.hold_ = dict(self.hold_, step='resume', phase='resuming', at=self.time)

    def released(self):
        self.hold_ = None
        self.time = self.since + 1800

    def comes_back(self, boots=True):
        """Killed, restarted, and the session's agent comes up, or doesn't, when the resume's line runs."""
        self.killed()
        self.restarted()
        if boots:
            self.fake.after[('pane', 'run', 2)] = self.boot

    def new_row(self):
        return ops_panes.by_pane(self.conn, PANE)

    def hub_restart(self):
        self.panes = PaneService(self.port, self.herdr, cfg=self.cfg, now=lambda: self.time)

    def pane_notices(self):
        return self.port.events(kind='pane.notice')

    def outcome_is_open(self):
        return self.outcome() is None


__all__ = ['CTRL_C', 'IDLE', 'Overnight', 'PANE', 'ROLE', 'SESSION', 'WORKING', 'paste']
