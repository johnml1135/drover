"""A pane closes only by the gates, and only when Drover started it. Port of the skill's test_reap_done.py.

Herdr is tests/fake_herdr.py, git is a table of answers and the process list is a fake CIM snapshot: no test starts a
process or reaches a real server.
"""
import json
import re
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from drover import pane_close
from drover.ledger import ops_panes
from tests.pane_close_fixtures import (CLAUDE_READY, CODEX_READY, DIALOG, DRAFT, HOUR, NOW, REPO, PaneCloseBase as Base8,
                                       agent, snapshot)


class Candidates(Base8):
    def test_only_retired_rows_are_candidates(self):
        for state in ('starting', 'live', 'rotating', 'compaction'):
            self.retired(pane=f'w1:{state}', state=state)
        self.retired(pane='w1:gone', state='gone')
        herdr = self.herdr({p: agent(p) for p in ('w1:starting', 'w1:live', 'w1:rotating', 'w1:compaction', 'w1:gone')})
        self.assertEqual(self.tick(herdr, 0, apply=True), [])
        self.assertEqual(self.tick(herdr, 60, apply=True), [])
        self.assertEqual(self.fake.closes(), [])
        self.assertEqual(self.fake.calls, [])

    def test_only_done_panes_are_candidates(self):
        for pane in ('w1:p2', 'w1:p3'):
            self.retired(pane=pane, terminal_id='t1')
        self.retired()
        herdr = self.herdr({'w1:p1': agent('w1:p1'), 'w1:p2': agent('w1:p2', status='idle'),
                            'w1:p3': agent('w1:p3', status='working')})
        results = {r['pane']: r for r in self.tick(herdr, 0)}
        self.assertEqual((results['w1:p2']['verdict'], results['w1:p3']['verdict'], results['w1:p1']['verdict']),
                         ('keep', 'keep', 'wait'))
        self.assertIn('not done (idle)', results['w1:p2']['reason'])
        self.assertFalse([c for c in self.fake.calls if c[:2] == ['pane', 'read'] and c[2] != 'w1:p1'])

    def test_other_panes_are_never_closed(self):
        self.retired(pane='w1:owner', started_by='owner')
        herdr = self.herdr({p: agent(p) for p in ('w1:owner', 'w1:unrecorded', 'w1:p9')})
        self.retired(pane='w1:p9', state='gone')    # history grants nothing
        for minutes in (0, 60, 120):
            self.tick(herdr, minutes, apply=True)
        self.assertEqual(self.fake.closes(), [])
        self.fake.calls.clear()
        # even handed a row directly, the verdicts refuse
        for row in (dict(self.retired(pane='w1:x'), started_by='owner'), dict(self.retired(pane='w1:y'), state='live')):
            out = pane_close.judge(row, agent(row['pane_id']), CLAUDE_READY, dict(path=REPO, clean=True, busy=False),
                                   now=NOW + 2 * HOUR, observed={row['pane_id']: dict(first_seen=NOW)})
            self.assertEqual(out['verdict'], 'keep')

    def test_live_director_pane_is_never_closed(self):
        old = self.retired()
        ops_panes.record_agent(self.conn, pane_id='w1:p1', kind='claude', seat='claude-main', placement='windows',
                               started_by='drover', terminal_id='t1', state='live', role_id='director')
        herdr = self.herdr()
        for minutes in (0, 60, 120):
            self.assertEqual(self.tick(herdr, minutes, apply=True), [])
        self.assertEqual(self.fake.closes(), [])
        self.assertEqual(ops_panes.by_pane(self.conn, 'w1:p1')['state'], 'live')
        self.assertEqual(self.conn.execute('SELECT state FROM pane_agents WHERE id=?', (old['id'],)).fetchone()[0],
                         'retired')

    def test_a_replaced_occupant_is_kept(self):
        self.retired()
        for label, change in (('terminal', dict(terminal_id='t2')), ('kind', dict(agent='codex'))):
            with self.subTest(label):
                herdr = self.herdr({'w1:p1': agent('w1:p1', **change)})
                self.tick(herdr, 0)
                self.assertEqual(self.tick(herdr, 60, apply=True)[0]['verdict'], 'keep')
                self.assertEqual(self.fake.closes(), [])

    def test_herdr_closed_keeps_everything_and_raises_nothing(self):
        self.retired()
        herdr = self.herdr()
        self.fake.closed = True
        out = self.tick(herdr, 120, apply=True)
        self.assertEqual([r['verdict'] for r in out], ['keep'])
        self.assertEqual(ops_panes.by_pane(self.conn, 'w1:p1')['state'], 'retired')
        self.assertEqual(self.events('pane-closed'), [])

    def test_a_pane_herdr_cannot_find_is_kept_not_marked_gone(self):
        self.retired()
        herdr = self.herdr({})
        out = self.tick(herdr, 120, apply=True)
        self.assertEqual(out[0]['verdict'], 'keep')
        self.assertEqual(ops_panes.by_pane(self.conn, 'w1:p1')['state'], 'retired')


class Clock(Base8):
    def test_an_hour_unchanged_is_the_candidate(self):
        self.retired()
        herdr = self.herdr()
        self.assertEqual(self.verdict(herdr, 0), 'wait')
        self.assertEqual(self.verdict(herdr, 59), 'wait')
        self.assertEqual(self.verdict(herdr, 60), 'close')
        self.assertEqual(self.fake.closes(), [])

    def test_any_change_restarts_the_clock(self):
        def bump(field, value):
            return lambda: self.fake.agents['w1:p1'].__setitem__(field, value)

        changes = (('seq', bump('state_change_seq', 11)), ('revision', bump('revision', 2)),
                   ('screen', lambda: self.fake.screens.__setitem__('w1:p1', 'New result.\n' + CLAUDE_READY)))
        self.retired()
        for label, change in changes:
            with self.subTest(label):
                self.conn.execute('DELETE FROM settings WHERE key=?', (pane_close.SETTING,))
                herdr = self.herdr()
                self.assertEqual(self.verdict(herdr, 0), 'wait')
                change()
                self.assertEqual(self.verdict(herdr, 60), 'wait')
                self.assertEqual(self.verdict(herdr, 119), 'wait')
                self.assertEqual(self.verdict(herdr, 120), 'close')

    def test_leaving_done_restarts_the_clock(self):
        self.retired()
        herdr = self.herdr()
        self.verdict(herdr, 0)
        self.fake.agents['w1:p1']['agent_status'] = 'idle'
        self.assertEqual(self.verdict(herdr, 30), 'keep')
        self.fake.agents['w1:p1']['agent_status'] = 'done'
        self.assertEqual(self.verdict(herdr, 70), 'wait')

    def test_clock_state_is_dropped_for_panes_that_have_gone(self):
        self.retired()
        herdr = self.herdr()
        self.verdict(herdr, 0)
        ops_panes.update_agent(self.conn, ops_panes.by_pane(self.conn, 'w1:p1')['id'], state='gone')
        self.tick(herdr, 1)
        self.assertEqual(self.port.get_setting(pane_close.SETTING), '{}')


class Gates(Base8):
    def watch(self, screen=CLAUDE_READY, **kwargs):
        self.retired()
        herdr = self.herdr(screens={'w1:p1': screen}, **kwargs)
        self.verdict(herdr, 0)
        return self.tick(herdr, 60)[0]

    def test_a_draft_or_an_open_dialog_keeps_the_pane(self):
        for label, screen in (('draft', DRAFT), ('dialog', DIALOG)):
            with self.subTest(label):
                self.conn.execute('DELETE FROM pane_agents')
                result = self.watch(screen)
                self.assertEqual(result['verdict'], 'keep')
                self.assertTrue(result['reason'].startswith(('composer', 'dialog')), result['reason'])
                self.assertEqual(self.fake.closes(), [])

    def test_a_focused_or_unknown_focus_pane_is_kept(self):
        for focused in (True, None):
            with self.subTest(focused=focused):
                self.conn.execute('DELETE FROM pane_agents')
                self.retired()
                herdr = self.herdr({'w1:p1': agent('w1:p1', focused=focused)})
                self.verdict(herdr, 0)
                result = self.tick(herdr, 60, apply=True)[0]
                self.assertEqual((result['verdict'], self.fake.closes()), ('keep', []))
                self.assertIn('focused', result['reason'])

    def test_uncommitted_or_untracked_changes_keep_the_pane(self):
        self.git.status = ' M README.md\n?? notes.txt'
        result = self.watch()
        self.assertEqual(result['verdict'], 'keep')
        self.assertIn('uncommitted or untracked', result['reason'])

    def test_a_pane_outside_any_git_checkout_is_kept(self):
        self.git.top = None
        result = self.watch()
        self.assertEqual(result['verdict'], 'keep')
        self.assertIn('not inside', result['reason'])

    def test_an_unmerged_unpushed_branch_closes_only_the_pane(self):
        self.git.merged = self.git.pushed = False
        self.retired()
        herdr = self.herdr()
        self.verdict(herdr, 0)
        result = self.tick(herdr, 60, apply=True)[0]
        self.assertEqual(result['verdict'], 'closed')
        self.assertIn('neither merged nor pushed', result['reason'])
        self.assertEqual((result['worktree']['merged'], result['worktree']['pushed']), (False, False))
        self.assertFalse([a for a in self.git.calls if a[0] in ('checkout', 'branch', 'worktree', 'reset')])

    def test_a_live_process_outside_the_pane_keeps_the_checkout(self):
        self.processes = snapshot((101, REPO, 100), (4242, REPO))    # 101 is the pane's own CLI
        result = self.watch()
        self.assertEqual(result['verdict'], 'keep')
        self.assertIn('live process', result['reason'])
        self.processes = snapshot((101, REPO, 100), (4242, 'H:/repos/elsewhere'))
        self.assertEqual(self.verdict(self.herdr(), 60), 'close')

    def test_unknown_process_visibility_keeps_the_pane(self):
        for label, processes in (('snapshot unknown', snapshot((101, REPO, 100), unknown=True)),
                                 ('no snapshot for this side', lambda row: None),
                                 ('broken', lambda row: (_ for _ in ()).throw(OSError('x')))):
            with self.subTest(label):
                self.conn.execute('DELETE FROM pane_agents')
                self.processes = processes
                result = self.watch()
                self.assertEqual(result['verdict'], 'keep', label)
                self.assertIn('visibility unavailable', result['reason'])
                self.assertEqual(self.fake.closes(), [])
        self.pids.clear()    # the pane's own processes cannot be listed
        self.processes = snapshot((101, REPO, 100))
        self.conn.execute('DELETE FROM pane_agents')
        self.assertEqual(self.watch()['verdict'], 'keep')

    def test_a_codex_pane_is_judged_by_the_same_gates(self):
        self.retired(kind='codex')
        herdr = self.herdr({'w1:p1': agent('w1:p1', kind='codex')}, {'w1:p1': CODEX_READY})
        self.verdict(herdr, 0)
        self.assertEqual(self.tick(herdr, 60, apply=True)[0]['verdict'], 'closed')
        self.assertEqual(self.fake.closes(), ['w1:p1'])

    def test_a_change_just_before_close_keeps_the_pane(self):
        self.retired()
        herdr = self.herdr()
        self.verdict(herdr, 0)
        reads = []
        self.fake.on_get = lambda fake: fake.agents['w1:p1'].__setitem__('revision', 2) if reads else reads.append(1)
        result = self.tick(herdr, 60, apply=True)[0]
        self.assertEqual((result['verdict'], self.fake.closes()), ('wait', []))
        self.assertIn('rechecked before close', result['reason'])
        self.assertEqual(ops_panes.by_pane(self.conn, 'w1:p1')['state'], 'retired')


class Closing(Base8):
    def test_dry_run_is_the_default(self):
        self.retired()
        herdr = self.herdr()
        self.verdict(herdr, 0)
        out = self.tick(herdr, 60, apply=False)
        self.assertEqual([r['verdict'] for r in out], ['close'])
        self.assertEqual(self.fake.closes(), [])
        self.assertEqual(self.events('pane-closed'), [])
        self.assertEqual(ops_panes.by_pane(self.conn, 'w1:p1')['state'], 'retired')
        self.assertIn('w1:p1', self.port.get_setting(pane_close.SETTING))

    def test_apply_closes_by_pane_id_and_logs_evidence_and_reason(self):
        row = self.retired()
        herdr = self.herdr()
        self.verdict(herdr, 0)
        out = self.tick(herdr, 60, apply=True)
        self.assertEqual([r['verdict'] for r in out], ['closed'])
        self.assertEqual(self.fake.closes(), ['w1:p1'])
        self.assertEqual([c for c in self.fake.calls if c[:2] == ['pane', 'close']], [['pane', 'close', 'w1:p1']])
        (event,) = self.events('pane-closed')
        self.assertEqual(event['actor'], 'pane_close')
        data = event['data'] if isinstance(event['data'], dict) else json.loads(event['data'])
        self.assertEqual((data['pane'], data['agent_id'], data['seat'], data['placement']),
                         ('w1:p1', row['id'], 'claude-main', 'windows'))
        self.assertEqual(data['evidence']['state_change_seq'], 10)
        self.assertEqual(data['idle_s'], HOUR)
        self.assertIn('unchanged 3600 s', data['reason'])
        self.assertNotIn('neither merged nor pushed', data['reason'])
        self.assertIsNone(ops_panes.by_pane(self.conn, 'w1:p1'))
        self.assertEqual(self.conn.execute('SELECT state FROM pane_agents WHERE id=?', (row['id'],)).fetchone()[0],
                         'gone')

    def test_a_refused_close_is_logged_and_the_row_stays_retired(self):
        self.retired()
        herdr = self.herdr()
        self.verdict(herdr, 0)
        self.fake.refuse[('pane', 'close')] = 'pane_busy'
        out = self.tick(herdr, 60, apply=True)
        self.assertEqual((out[0]['verdict'], out[0]['reason']), ('close-failed', 'pane_busy'))
        self.assertEqual(len(self.events('pane-close-failed')), 1)
        self.assertEqual(ops_panes.by_pane(self.conn, 'w1:p1')['state'], 'retired')

    def test_pane_code_has_no_pattern_kill(self):
        source = (Path(pane_close.__file__)).read_text(encoding='utf-8')
        for word in ('pkill', 'shell=True', 'taskkill', 'os.kill', 'terminate', 'Popen', 'subprocess'):
            self.assertNotIn(word, source)
        calls = set(re.findall(r"herdr\.(?:call|text)\('(\w+)', '([\w-]+)'", source))
        self.assertEqual(calls, {('agent', 'get'), ('pane', 'get'), ('pane', 'read'), ('pane', 'process-info')})
        self.assertEqual(re.findall(r"pane_send\.close_pane\(port, herdr, (\w+)\)", source), ['current'])


if __name__ == '__main__':
    unittest.main()
