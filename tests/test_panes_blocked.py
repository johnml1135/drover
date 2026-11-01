"""A launch that cannot start for a reason outside the agent is stopped before it costs anything, and the owner hears once."""
import shutil
import unittest
from unittest import mock

from drover import pane_launch, panes
from drover.ledger import ops_panes
from tests.pane_launch_support import LONE_SHELL
from tests.pane_send_support import PANE
from tests.panes_support import ROLE, Resident


class Case(Resident):
    def spent(self):
        count = lambda table: self.conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        return count('director_budget_entries'), count('director_runs'), count('questions') - self.notice_questions(), \
            self.role()['generation']

    def notice_questions(self):
        return len(self.port.events(kind='pane.notice'))

    def ticks(self, count=10, step=30):
        for _ in range(count):
            self.time += step
            self.tick()

    def notes(self, cause=''):
        return [n for n in self.notices() if n['id'].startswith(cause if cause.startswith('pane-unknown') else f'pane-blocked:{ROLE}:{cause}')]

    def status(self, notice):
        event = next(e for e in self.port.events(kind='pane.notice') if e['data']['notice']['id'] == notice['id'])
        return self.conn.execute('SELECT status FROM questions WHERE id=?', (event['data']['question'],)).fetchone()[0]


class Blocked(Case):
    def blocked_then_freed(self, cause, block, free):
        block()
        self.wake()
        before, tabs = self.spent(), len(self.calls('tab', 'create'))
        self.ticks()
        self.assertEqual(self.spent(), before)
        self.assertEqual(self.open_questions(), 0)
        self.assertIsNone(self.role()['held_reason'])
        self.assertEqual(len(self.calls('tab', 'create')), tabs)
        notice, = self.notes(cause)
        self.assertEqual(self.status(notice), 'open')
        self.assertTrue(self.role()['pending_wake'], 'the mail waits')
        free()
        self.time += 30
        self.assertEqual(len(self.tick()['launched']), 1)
        self.assertEqual(self.status(notice), 'answered')

    def test_no_workspace_spends_nothing_and_the_launch_follows_one(self):
        self.build()
        workspaces = self.fake.workspaces
        self.blocked_then_freed('herdr-no-workspace', lambda: setattr(self.fake, 'workspaces', []),
                                lambda: setattr(self.fake, 'workspaces', workspaces))

    def test_a_configured_workspace_that_closed_is_the_same(self):
        self.build()
        self.blocked_then_freed('herdr-no-workspace', lambda: self.cfg['panes'].update(workspace='w9'),
                                lambda: self.cfg['panes'].update(workspace='w1'))

    def test_a_missing_checkout_is_the_same(self):
        self.build()
        moved = self.cwd.with_name('away')
        self.blocked_then_freed('pane-cwd-missing', lambda: shutil.move(self.cwd, moved), lambda: shutil.move(moved, self.cwd))

    def test_an_unknown_seat_is_the_same(self):
        self.build()
        seats = self.cfg['seats']
        self.blocked_then_freed('pane-seat-unknown', lambda: self.cfg.update(seats=[]), lambda: self.cfg.update(seats=seats))

    def test_an_agent_in_a_failed_pane_is_the_same(self):
        self.launched()
        ops_panes.update_agent(self.conn, self.row['id'], state='failed')
        self.fake.panes[PANE]['terminal_id'] = 't1'
        self.time += pane_launch.STARTUP_S + 1
        self.conn.execute("UPDATE pane_agents SET run_id=NULL WHERE id=?", (self.row['id'],))
        self.conn.commit()
        self.runtime.finish(self.rid, dict(status='done', usage={}))

        def out():
            del self.fake.agents[PANE]
            self.fake.processes = LONE_SHELL
        self.blocked_then_freed('pane-role-unconfirmed', lambda: None, out)

    def test_a_cause_that_changes_is_a_new_notice_and_the_old_one_closes(self):
        self.build()
        self.fake.workspaces, workspaces = [], self.fake.workspaces
        self.wake()
        self.ticks(2)
        first, = self.notes('herdr-no-workspace')
        self.fake.workspaces = workspaces
        moved = self.cwd.with_name('away')
        shutil.move(self.cwd, moved)
        self.ticks(2)
        self.assertEqual(self.status(first), 'answered')
        self.assertEqual(len(self.notes('pane-cwd-missing')), 1)

    def test_the_same_cause_again_is_a_new_notice(self):
        self.build()
        workspaces = self.fake.workspaces
        self.wake()
        for _ in range(2):
            self.fake.workspaces = []
            self.ticks(2)
            self.fake.workspaces = workspaces
            self.conn.execute('UPDATE director_runtime SET pending_wake=1')
            self.conn.commit()
            with mock.patch.object(pane_launch, 'start', side_effect=OSError('lost')):
                self.tick()
            self.conn.execute("UPDATE director_runs SET state='finished'")
            self.conn.execute("UPDATE runs SET outcome='done'")
            self.conn.commit()
        self.assertEqual(len(self.notes('herdr-no-workspace')), 2)

    def after_the_first_list(self, event):
        self.fake.after[('workspace', 'list', self.fake.seen.get(('workspace', 'list'), 0) + 1)] = event

    def test_herdr_closing_at_the_workspace_read_is_the_outage_notice(self):
        self.build()
        self.wake()
        before = self.spent()
        self.after_the_first_list(lambda: setattr(self.fake, 'closed', True))
        self.ticks(1)
        self.assertEqual(self.spent(), before)
        self.assertEqual(self.notes(), [])
        self.assertEqual(len([n for n in self.notices() if n['id'].startswith('pane-herdr-closed:')]), 1)

    def test_a_refused_workspace_read_is_a_cause(self):
        self.build()
        self.wake()
        before = self.spent()
        self.after_the_first_list(lambda: self.fake.refuse.update({('workspace', 'list'): 'server_busy'}))
        self.ticks(1)
        self.assertEqual(self.spent(), before)
        self.assertEqual(len(self.notes('herdr-refused:server_busy')), 1)

    def test_a_closed_herdr_is_the_outage_notice_alone(self):
        self.build()
        self.fake.closed = True
        self.wake()
        self.ticks(3)
        self.assertEqual(self.notes(), [])
        self.assertEqual(len([n for n in self.notices() if n['id'].startswith('pane-herdr-closed:')]), 1)

    def test_a_held_launch_with_no_room_still_asks_nothing_while_blocked(self):
        self.build()
        self.fake.workspaces = []
        self.capacity['slots'] = 0
        self.wake()
        self.ticks(3)
        self.assertEqual(self.open_questions(), 0)
        self.assertEqual(len(self.notes('herdr-no-workspace')), 1)

    def test_a_resume_needs_no_workspace_of_its_own(self):
        self.launched()
        del self.fake.agents[PANE]
        self.fake.processes = LONE_SHELL
        self.fake.workspaces = []
        self.fake.after[('agent', 'start', self.fake.seen.get(('agent', 'start'), 0) + 1)] = self.boot
        self.fake.screens[PANE] = 'PS C:\\repos> '
        self.time += 90000
        self.assertEqual(len(self.tick()['launched']), 1)
        self.assertEqual(self.notes(), [])

    def test_the_gate_writes_nothing_to_herdr(self):
        self.build()
        self.fake.workspaces = []
        self.wake()
        self.ticks(2)
        self.assertEqual([c for c in self.fake.calls if c[:2] not in (['workspace', 'list'], ['pane', 'list'])], [])


class LapsedAndUnreadable(Case):
    def test_a_lapsed_pane_that_stays_unreadable_is_one_notice_and_one_question(self):
        self.launched()
        del self.fake.panes[PANE]['terminal_id']
        self.time += 90000
        self.wake()
        self.ticks(1)
        self.ticks(1, panes.UNKNOWN_LIMIT_S - 1)
        self.assertEqual(self.notes('pane-unknown:'), [])
        self.assertEqual(self.open_questions(), 0)
        self.ticks(1, 2)
        self.ticks(5, 3600)
        self.assertEqual(len(self.notes('pane-unknown:')), 1)
        self.assertEqual(self.open_questions(), 1)
        self.assertEqual(self.role()['held_reason'], 'consumer-liveness-uncertain')
        self.assertIsNone(self.outcome(), 'a failed read ends nothing')
        self.assertEqual(self.row_now()['state'], 'live')
        self.assertEqual(self.calls('pane', 'send-keys'), [])

    def test_a_lapsed_run_during_an_outage_is_the_outage_notice_alone(self):
        self.launched()
        self.fake.closed = True
        self.time += 90000
        self.ticks(1)
        self.ticks(3, panes.UNKNOWN_LIMIT_S)
        self.assertEqual(self.notes('pane-unknown:'), [])
        self.assertEqual(self.open_questions(), 0)


if __name__ == '__main__':
    unittest.main()
