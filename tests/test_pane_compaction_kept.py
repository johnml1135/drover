"""What the compaction must not undo: a fence, a live agent's session, and a hold it cannot read. Herdr is always the fake."""
import json
import unittest
from pathlib import Path
from unittest import mock

from drover import compaction_hold, pane_launch
from drover.ledger import ops_panes
from tests.pane_compaction_support import Overnight, PANE, paste
from tests.pane_launch_support import CLAUDE_WORKING
from tests.pane_send_support import CLAUDE_IDLE, DIALOG, DRAFT, composing

RESUME = paste('restart complete, keep going')
CLAUDE_RESUMED = 'history\n───\n❯ an earlier prompt\n  an earlier answer\n───\n❯ \n───\n⏵⏵ auto mode on\n'


class Case(Overnight):
    def stopped_tonight(self):
        self.up()
        self.at(0)
        self.fake.calls.clear()

    def sessions(self):
        return self.conn.execute('SELECT COUNT(*) FROM director_sessions').fetchone()[0]

    def starts_after_the_release(self):
        self.released()
        with mock.patch.object(pane_launch, 'start') as start:
            self.tick()
        return start


class Fenced(Case):
    def test_a_stopped_run_is_not_relaunched_or_told(self):
        self.stopped_tonight()
        self.notifications.stop('owner', 'stop')
        self.comes_back()
        self.at(900)
        self.assertEqual(self.agent()['outcome'], 'stayed-stopped')
        self.assertTrue(self.answer['settled'])
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.row_now()['state'], 'retired')
        self.assertFalse(self.notifications.admission()['allowed'])
        notice, = self.pane_notices()
        self.assertIn('seat-x (wsl)', notice['data']['notice']['body'])
        self.at(920)
        self.assertEqual(len(self.pane_notices()), 1)
        self.assertEqual(self.starts_after_the_release().call_count, 0)
        self.assertNotEqual(self.outcome(), 'compaction-fresh')

    def test_a_stop_action_alone_is_a_fence(self):
        self.stopped_tonight()
        action = self.conn.execute('SELECT launch_action_id FROM director_runs').fetchone()[0]
        self.conn.execute('UPDATE director_runs SET stop_action_id=?', (action,))
        self.comes_back()
        self.at(900)
        self.assertEqual((self.agent()['outcome'], self.sends()), ('stayed-stopped', []))

    def test_a_stop_while_the_agent_comes_up_gets_it_no_text(self):
        self.stopped_tonight()
        self.comes_back()
        self.fake.after[('pane', 'run', 2)] = lambda: (self.boot(), self.fake.agents[PANE].update(agent_status='working'))
        self.at(900)
        self.notifications.stop('owner', 'stop')
        self.settle()
        self.at(920)
        self.assertEqual(self.agent()['outcome'], 'stayed-stopped')
        self.assertNotIn(RESUME, self.sends())
        self.assertEqual(self.new_row()['state'], 'live')

    def test_a_stopped_agent_still_in_its_pane_keeps_its_row(self):
        self.stopped_tonight()
        self.notifications.stop('owner', 'stop')
        self.restarted()
        self.at(900)
        self.assertEqual((self.agent()['outcome'], self.sends()), ('stayed-stopped', []))
        self.assertEqual(self.row_now()['state'], 'live')

    def test_a_fenced_pane_that_cannot_be_read_waits_for_a_read(self):
        self.stopped_tonight()
        self.notifications.stop('owner', 'stop')
        self.killed()
        self.restarted()
        self.fake.closed = True
        self.at(900)
        self.assertIsNone(self.agent()['outcome'])
        self.assertEqual(self.row_now()['state'], 'compaction')
        self.fake.closed = False
        self.at(920)
        self.assertEqual(self.agent()['outcome'], 'stayed-stopped')
        self.assertEqual(self.row_now()['state'], 'retired')

    def test_a_row_a_hold_left_behind_stays_stopped_too(self):
        self.stopped_tonight()
        self.notifications.stop('owner', 'stop')
        self.killed()
        self.released()
        self.panes.compaction_tick(None)
        self.assertEqual(self.row_now()['state'], 'retired')
        self.assertIsNone(self.outcome())
        self.assertEqual(len(self.pane_notices()), 1)

    def test_a_pause_is_no_fence(self):
        self.stopped_tonight()
        self.fake.settles = True
        self.port.set_setting('dispatch_enabled', 'false')
        self.comes_back()
        self.at(900)
        self.assertEqual(self.agent()['outcome'], 'resumed')


class Kept(Case):
    def assert_kept(self, reason):
        self.assertEqual((self.agent()['outcome'], self.agent()['reason']), ('resumed-untold', reason))
        self.assertTrue(self.answer['settled'])
        self.assertTrue(self.outcome_is_open())
        self.assertEqual(self.sessions(), 1)
        self.assertEqual(self.new_row()['state'], 'live')
        self.assertTrue(self.role()['pending_wake'], 'the wake route tells it')
        self.assertEqual(self.starts_after_the_release().call_count, 0)

    def test_an_agent_that_was_never_killed_keeps_its_session(self):
        self.stopped_tonight()
        self.conn.execute('UPDATE director_runtime SET pending_wake=0')
        self.restarted()
        self.at(900)
        self.assert_kept('pane-agent-running')
        self.assertEqual(self.sends(), [])

    def test_an_agent_back_but_unable_to_take_the_text_keeps_its_session(self):
        self.stopped_tonight()
        self.comes_back()
        self.fake.after[('pane', 'run', 2)] = lambda: (self.boot(), self.fake.agents[PANE].update(focused=True))
        self.at(900)
        self.at(900 + compaction_hold.RESUME_S)
        self.assert_kept('focused')
        self.assertNotIn(RESUME, self.sends())

    def test_a_resume_text_with_no_proof_leaves_the_next_wake_free(self):
        self.stopped_tonight()
        self.comes_back()
        self.sleep.side_effect = lambda seconds: self.fake.screens.update({PANE: composing('something else')})
        self.at(900)
        self.assert_kept('not-own')
        self.assertEqual(ops_panes.open_writes(self.conn, self.new_row()['id']), [])

    def test_a_resumed_screen_that_reads_as_a_draft_gets_no_text(self):
        self.stopped_tonight()
        self.comes_back()
        self.fake.after[('pane', 'run', 2)] = lambda: (self.boot(), self.fake.screens.update({PANE: DRAFT}))
        self.at(900)
        self.at(900 + compaction_hold.RESUME_S)
        self.assert_kept('draft')
        self.assertNotIn(RESUME, self.sends())

    def test_a_new_row_that_failed_with_an_agent_in_it_keeps_the_session(self):
        self.stopped_tonight()
        self.comes_back()
        self.fake.after[('pane', 'run', 2)] = lambda: (self.boot(), self.fake.screens.update({PANE: DIALOG}))
        self.at(900)
        self.assertEqual(self.agent()['outcome'], 'resume-failed')
        self.assertTrue(self.outcome_is_open())
        self.assertEqual(self.sessions(), 1)
        self.assertEqual(self.new_row()['state'], 'failed')

    def test_a_pane_that_is_gone_shows_no_agent(self):
        self.stopped_tonight()
        self.comes_back(boots=False)
        self.fake.refuse[('pane', 'process-info')] = 'pane_not_found'
        self.at(900)
        self.assertEqual(self.agent()['outcome'], 'resume-failed')
        self.assertEqual(self.outcome(), 'compaction-fresh')

    def test_a_job_that_died_before_its_kill_leaves_the_agent_its_session(self):
        self.stopped_tonight()
        self.released()
        self.panes.compaction_tick(None)
        self.assertEqual(self.row_now()['state'], 'live')
        self.assertTrue(self.outcome_is_open())
        self.assertEqual(self.sessions(), 1)
        notice, = self.pane_notices()
        self.assertIn('seat-x (wsl)', notice['data']['notice']['body'])
        self.panes.compaction_tick(None)
        self.assertEqual(len(self.pane_notices()), 1)

    def test_no_read_leaves_the_row_alone_and_the_next_read_decides(self):
        self.stopped_tonight()
        self.comes_back(boots=False)
        self.fake.closed = True
        self.at(900)
        self.assertIsNone(self.agent()['outcome'])
        self.assertEqual(self.agent()['reason'], 'herdr-unavailable')
        self.assertFalse(self.answer['settled'])
        self.at(920)
        self.assertEqual(self.agent()['reason'], 'herdr-unavailable', 'the cause, on every tick that asks')
        self.at(900 + compaction_hold.RESUME_S)
        self.assertTrue(self.answer['settled'], 'the job need not wait past the deadline')
        self.assertEqual(self.row_now()['state'], 'compaction')
        self.assertEqual(self.sessions(), 1)
        self.released()
        self.panes.compaction_tick(None)
        self.assertEqual(self.row_now()['state'], 'compaction')
        self.fake.closed = False
        self.panes.compaction_tick(None)
        self.assertEqual(self.outcome(), 'compaction-fresh')
        self.assertEqual(self.row_now()['state'], 'failed')


class ClaudeInWsl(Case):
    """A resumed Claude session shows its history, which reads as a draft until Task 15 captures an answer row."""

    def configure(self, role):
        super().configure(role)
        configs = json.loads(self.port.get_setting('director_execution'))
        configs[role].update(backend='claude', model='gpt-6.1-sol', seat='seat-c')
        self.port.set_setting('director_execution', json.dumps(configs))

    def scene(self, kind='claude', placement='wsl', **kw):
        fake = super().scene(kind, placement, **kw)
        fake.idle, fake.working = CLAUDE_IDLE, CLAUDE_WORKING
        fake.compose = lambda text: f'history\n───\n❯ {text}\n───\n⏵⏵ auto mode on\n'
        return fake

    def test_history_on_screen_gets_no_stop_and_no_resume_text(self):
        self.up()
        self.fake.screens[PANE] = CLAUDE_RESUMED
        self.fake.calls.clear()
        self.at(0)
        self.assertEqual((self.agent()['outcome'], self.agent()['reason']), ('left-for-kill', 'draft'))
        self.killed()
        self.restarted()
        self.fake.after[('pane', 'run', 2)] = lambda: (self.boot(), self.fake.screens.update({PANE: CLAUDE_RESUMED}))
        self.at(900)
        self.at(900 + compaction_hold.RESUME_S)
        self.assertEqual((self.agent()['outcome'], self.agent()['reason']), ('resumed-untold', 'draft'))
        self.assertTrue(self.outcome_is_open())
        self.assertEqual([call for call in self.sends() if call[:2] == ['pane', 'send-text']], [])


class HoldReads(Case):
    def left_with_no_hold(self):
        self.stopped_tonight()
        self.killed()
        self.hold_ = None               # `active` read None; what follows is what proves it

    def assert_untouched(self):
        self.panes.compaction_tick(None)
        self.assertEqual(self.row_now()['state'], 'compaction')
        self.assertTrue(self.outcome_is_open())

    def test_a_failed_read_of_the_hold_changes_nothing(self):
        self.left_with_no_hold()
        compaction_hold.save(self.state, dict(id='h1', at=self.time))
        with mock.patch.object(Path, 'read_text', side_effect=PermissionError('being replaced')):
            self.assert_untouched()

    def test_a_live_hold_on_disk_changes_nothing(self):
        self.left_with_no_hold()
        compaction_hold.save(self.state, dict(id='h1', at=compaction_hold.clock()))
        self.assert_untouched()

    def test_a_hold_file_that_cannot_be_parsed_changes_nothing(self):
        self.left_with_no_hold()
        compaction_hold.save(self.state, dict(id='h1'))
        (self.state / 'disk' / compaction_hold.HOLD_FILE).write_text('{"id"', encoding='utf-8')
        self.assert_untouched()

    def test_only_a_clean_read_ends_it(self):
        self.assertTrue(compaction_hold.over(self.state), 'no file')
        compaction_hold.save(self.state, dict(id='h1', at=compaction_hold.clock() - compaction_hold.MAX_AGE_S - 1))
        self.assertTrue(compaction_hold.over(self.state), 'aged out')
        compaction_hold.save(self.state, dict(id='h1', at=compaction_hold.clock()))
        self.assertFalse(compaction_hold.over(self.state))


class Superseded(Case):
    def open_write(self, kind, state='unverified'):
        write = ops_panes.note_write(self.conn, self.row['id'], kind, 'a write to the old agent')
        return ops_panes.finish_write(self.conn, write['id'], state, 'pasted')

    def state_of(self, write):
        return self.conn.execute('SELECT state,reason FROM pane_writes WHERE id=?', (write['id'],)).fetchone()

    def test_open_writes_to_a_gone_agent_close_once_the_shell_shows_alone(self):
        self.stopped_tonight()
        self.fake.settles = True
        writes = [self.open_write('wake'), self.open_write('wake', 'wake_unverified'), self.open_write('handoff'),
                  self.open_write('stop'), self.open_write('interrupt')]
        self.comes_back()
        self.at(900)
        self.assertEqual(self.agent()['outcome'], 'resumed')
        self.assertEqual({tuple(self.state_of(write)) for write in writes}, {('observed', 'superseded')})

    def test_a_pane_not_shown_alone_keeps_them_open(self):
        self.stopped_tonight()
        write = self.open_write('wake')
        self.restarted()
        with self.assertRaises(pane_launch.AdmissionHeld) as caught:
            pane_launch.resume(self.port, self.herdr, self.row_now(), self.spec_, cfg=self.cfg)
        self.assertEqual(str(caught.exception), 'pane-agent-running')
        self.assertEqual(self.state_of(write)['state'], 'unverified')

    def test_any_other_open_write_still_stops_a_resume(self):
        self.stopped_tonight()
        self.open_write('brief')
        self.killed()
        with self.assertRaises(pane_launch.AdmissionHeld) as caught:
            pane_launch.resume(self.port, self.herdr, self.row_now(), self.spec_, cfg=self.cfg)
        self.assertEqual(str(caught.exception), 'pane-open-write')


if __name__ == '__main__':
    unittest.main()
