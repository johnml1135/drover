"""What the pane_send tests share: fixture screens, a fake that changes between calls, and a stage to act on."""
import json
from pathlib import Path
from unittest import mock

from drover import pane_send
from drover.herdr_client import Herdr
from drover.ledger import ops_panes
from drover.pane_send import PaneRefused
from drover.port import DirectPort
from tests.fake_herdr import FakeHerdr
from tests.test_ledger import Base

ROOT = Path(__file__).resolve().parents[1]
PANE = 'w1:p1'
TEXT = 'Drover: new notices. Read your inbox and commit a checkpoint.'
IDLE = 'history\n\n› \n\n? for shortcuts\n'
CLAUDE_IDLE = 'history\n───\n❯ \n───\n⏵⏵ auto mode on\n'
WORKING = 'history\n\n• Working (12s • esc to interrupt)\n\n› \n\n? for shortcuts\n'
DRAFT = 'history\n\n› half a thought from the owner\n\n? for shortcuts\n'
DIALOG = 'Allow once?\n› 1. Allow once\n  2. Deny\n'
MODEL = 'Switch to gpt-6.1 sol?\n› 1. Switch to gpt-6.1 sol\n  2. Keep current model\nenter select · esc back\n'
SUSPECT = 'history\n\nShall I delete the branch?\n\n› \n\n? for shortcuts\n'
COPY = 'history\n\n› \n^c copy · enter copy & follow · esc clear\n'
UNREADABLE = 'history\n\n› \n\n? for sho\ufffdtcuts\n'
WRITE_VERBS = (('agent', 'prompt'), ('agent', 'send-keys'), ('pane', 'send-text'), ('pane', 'send-keys'), ('pane', 'run'),
               ('agent', 'start'))
PASTE = ['pane', 'send-text', PANE, f'\x1b[200~{TEXT}\x1b[201~']
ENTER = ['pane', 'send-keys', PANE, 'enter']


def running(*names, shell=1):
    return {'shell_pid': shell, 'foreground_processes': [{'pid': 2 + i, 'name': n} for i, n in enumerate(names)]}


def composing(text):
    return f'history\n\n› {text}\n\n? for shortcuts\n'


class Stage(FakeHerdr):
    """A fake that can change between two calls: `after[(group, verb, n)]` runs after the nth such call."""

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.after, self.seen = {}, {}
        self.processes = None

    def _answer(self, args):
        answer = super()._answer(args)
        if args[:2] == ['pane', 'process-info'] and answer[0] == 0 and self.processes is not None:
            return 0, json.dumps({'result': {'process_info': self.processes}}), ''
        return answer

    def __call__(self, argv, **kwargs):
        done = super().__call__(argv, **kwargs)
        key = tuple(argv[1:3])
        self.seen[key] = self.seen.get(key, 0) + 1
        hook = self.after.get((*key, self.seen[key]))
        if hook:
            hook()
        return done


class Case(Base):
    def setUp(self):
        super().setUp()
        self.port = DirectPort(self.conn)
        sleeper = mock.patch('time.sleep')
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)

    def stage(self, screen=IDLE, *, kind='codex', placement='wsl', row=None, agent=None, pane=None):
        self.row = ops_panes.record_agent(self.conn, **{
            'pane_id': PANE, 'terminal_id': 't1', 'session_id': 's1', 'kind': kind, 'seat': 'seat',
            'placement': placement, 'started_by': 'drover', 'state': 'live', **(row or {})})
        self.fake = Stage(
            {PANE: {'pane_id': PANE, 'terminal_id': 't1', 'agent': kind, 'agent_session': {'value': 's1'},
                    'agent_status': 'idle', 'focused': False, 'name': 'n', **(agent or {})}},
            {PANE: screen}, {PANE: {'pane_id': PANE, 'terminal_id': 't1', 'focused': False,
                                    'scroll': {'offset_from_bottom': 0}, **(pane or {})}})
        self.fake.processes = running('wsl.exe' if placement == 'wsl' else 'claude.exe')
        self.herdr = Herdr('herdr', run=self.fake, retry_s=0)
        return self.fake

    def reset(self):
        self.conn.execute('DELETE FROM pane_writes')
        self.conn.execute('DELETE FROM pane_agents')

    def windows(self, screen=CLAUDE_IDLE, **kw):
        return self.stage(screen, kind='claude', placement='windows', **kw)

    def sends(self):
        return [call for call in self.fake.calls if tuple(call[:2]) in WRITE_VERBS]

    def last(self):
        return ops_panes.last_write(self.conn, self.row['id'])

    def send(self, text=TEXT, kind='wake'):
        return pane_send.send(self.port, self.herdr, self.row, text, kind)

    def stop(self, text='stop now', hold='h1'):
        return pane_send.send_stop(self.port, self.herdr, self.row, text, hold)

    def refused(self, act, reason=None):
        with self.assertRaises(PaneRefused) as caught:
            act()
        if reason:
            self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(self.sends(), [])

    def paste_settles_to(self, screen):
        self.sleep.side_effect = lambda seconds: self.fake.screens.update({PANE: screen})


def sources():
    for top in ('drover', 'hub', 'scripts'):
        yield from (p for p in (ROOT / top).rglob('*.py') if '.venv' not in p.parts)
