"""What the pane_launch tests share: a config and spec, private files kept in memory, and an agent that boots on cue."""
import itertools
import time
from pathlib import Path
from unittest import mock

from drover import pane_guard, pane_launch
from drover.herdr_client import Herdr
from drover.ledger import ops_panes
from tests.pane_send_support import CLAUDE_IDLE, IDLE, PANE, Case, Stage, WORKING, composing, running

SECRET = 'cred-0123456789-do-not-leak'
ADVANCES = 12
TICK_S = 30
PROMPT = 'PS C:\\repos> '
LONE_SHELL = {'shell_pid': 1, 'foreground_processes': [{'pid': 1, 'name': 'pwsh.exe'}]}
TRUST = '\n'.join(pane_guard.CODEX_TRUST) + '\n'
CLAUDE_TRUST = 'Do you trust the files in this folder?\n❯ 1. Yes, proceed\n  2. No, exit\n'
CLAUDE_WORKING = 'history\n───\n✻ Working (3s · esc to interrupt)\n❯ \n───\n⏵⏵ auto mode on\n'
SEATS = {'claude': {'name': 'seat-c', 'backend': 'claude', 'placement': 'windows', 'config_dir': 'C:\\seats\\c'},
         'codex': {'name': 'seat-x', 'backend': 'codex', 'placement': 'wsl', 'codex_home': '~/.codex'}}


class Launching(Case):
    def setUp(self):
        super().setUp()
        self.state = Path(self.tmp.name) / 'state'
        self.cwd = Path(self.tmp.name) / 'repo'
        self.cwd.mkdir()
        self.cfg = dict(paths=dict(state_dir=str(self.state), wsl_distro='Ubuntu'), models=dict(ids={'opus-5.5': 'claude-opus-5-5'}),
                        panes=dict(workspace=''), hub=dict(api_listen_wsl=True, api_port=8766), director=dict(effort='high'),
                        seats=list(SEATS.values()))
        self.private = []
        self.trusted = False
        ticks = itertools.count()
        for name, value in (('_now', lambda: next(ticks)), ('write_private', self.write_private),
                            ('repo_trusted', lambda cwd, seat: self.trusted), ('state_dir', lambda cfg: self.state)):
            patcher = mock.patch.object(pane_launch, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_private(self, path, text, *, placement):
        self.private.append((str(path), text, placement))
        return str(path)

    def spec(self, kind='claude', placement='windows', **over):
        return {'backend': kind, 'model': 'opus-5.5' if kind == 'claude' else 'gpt-6.1-sol', 'seat': SEATS[kind]['name'],
                'cwd': str(self.cwd), 'placement': placement, 'role': 'director', 'role_id': 'R1', 'run_id': 7, 'effort': None,
                'user_prompt': 'Process the inbox.', **over}

    def scene(self, kind='claude', placement='windows', *, boots=True, status='idle', session='s1', screen=None):
        """A Herdr with an empty shell ready for a tab. The agent appears when the launch goes out, if it `boots`."""
        fake = self.fake = Stage(screens={PANE: PROMPT})
        fake.processes = LONE_SHELL
        self.herdr = Herdr('herdr', run=fake, retry_s=0)
        idle = screen or (IDLE if kind == 'codex' else CLAUDE_IDLE)
        self.foreground = running('wsl.exe' if placement == 'wsl' else 'claude.exe')

        def boot():
            fake.processes = self.foreground
            fake.screens[PANE] = idle
            fake.agents[PANE] = dict(pane_id=PANE, terminal_id='t1', agent=kind, agent_session={'value': session},
                                     agent_status=status, focused=False)
        self.boot = boot
        if boots:
            fake.after[('pane', 'run', 1) if placement == 'wsl' else ('agent', 'start', 1)] = boot
        return fake

    def turns(self, screen=None, *, kind='claude', status='working'):
        """The brief reaches the agent and a turn shows, on either way of sending it."""
        fake = self.fake
        screen = screen or (WORKING if kind == 'codex' else CLAUDE_WORKING)

        def turn():
            fake.screens[PANE] = screen
            fake.agents[PANE]['agent_status'] = status
        fake.after[('agent', 'prompt', 1)] = turn
        fake.after[('pane', 'send-keys', 1)] = turn
        self.sleep.side_effect = lambda seconds: fake.screens.update({PANE: composing(pane_launch.brief(self.spec()))}) \
            if seconds == pane_launch.pane_send.PASTE_SETTLE_S else None

    def start(self, spec=None, guard=lambda: None):
        """A whole launch, as later ticks finish it: past its deadlines if nothing moves."""
        spec = spec or self.spec()
        row = pane_launch.start(self.port, self.herdr, spec, SECRET, guard, cfg=self.cfg)
        when = time.time()
        for _ in range(ADVANCES):
            current = ops_panes.by_pane(self.conn, row['pane_id']) or row
            if current['state'] != 'live' or pane_launch.advance(self.port, self.herdr, current, spec, guard, when, cfg=self.cfg):
                return current
            when += TICK_S
        return ops_panes.by_pane(self.conn, row['pane_id'])

    def writes(self, row=None):
        return [dict(r) for r in self.conn.execute('SELECT * FROM pane_writes WHERE agent_id=? ORDER BY id', ((row or self.row)['id'],))]

    def rows(self):
        return ops_panes.open_agents(self.conn)

    def notices(self):
        return [e['data']['notice'] for e in self.port.events(kind='pane.notice')]

    def reason(self):
        return self.port.events(kind='pane-launch-failed')[-1]['data']['reason']

    def launcher_texts(self):
        return [p.read_text(encoding='utf-8') for p in self.state.rglob('*') if p.is_file()]

    def calls(self, group, verb):
        return [c for c in self.fake.calls if c[:2] == [group, verb]]
