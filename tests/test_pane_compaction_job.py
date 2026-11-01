"""The job's side of the pane stop and resume: it waits on the hub's answer and never depends on it. Every command is a fake."""
import unittest
from unittest import mock

from drover import compaction, compaction_hold
from tests.compaction_fixture import Job, NOW


class Night(Job):
    """The job's side, with the hub's answers on stubs or in the file."""

    def setUp(self):
        super().setUp()
        self.cfg['panes']['enabled'] = True
        self.hub = dict(stop=dict(id='h', phase='stop', settled=True, agents=[self.stopped('left-for-kill')]),
                        resume=dict(id='h', phase='resume', settled=True, agents=[self.stopped('resumed')]))
        self.waits = []

    @staticmethod
    def stopped(outcome, reason=None):
        return dict(pane_id='p', seat='seat-x', placement='wsl', session_id='s', outcome=outcome, reason=reason, stopped='stopped-asked')

    def runtime(self, cls=compaction.Runtime):
        job = super().runtime(cls)
        def wait(phase, deadline_s):
            self.waits.append((phase, deadline_s, (self.hold() or {}).get('phase')))
            return self.hub[phase]
        if self.hub:
            job.wait_for_panes = wait
        return job

    def test_job_goes_on_when_the_hub_never_answers(self):
        self.hub = None
        answer = self.run_job()
        self.assertEqual(answer['status'], 'compacted')
        self.assertIn('the hub gave no pane stop answer in 2 minutes', answer['errors'])
        self.assertTrue(self.fake.seen('--shutdown'))
        self.assertEqual(self.clock.now - NOW, 2 * compaction.PANE_ANSWER_S)
        self.assertEqual(self.report()['panes']['agents'], [])

    def test_stop_and_drain_fit_the_deadlines(self):
        settled = compaction_hold.ASK_S + compaction_hold.INTERRUPT_S
        self.assertEqual(settled, 360)
        self.assertGreater(compaction.PANE_STOP_WAIT_S, settled)
        self.assertGreaterEqual(compaction.PANE_STOP_WAIT_S, compaction.PANE_ANSWER_S)
        self.assertGreater(compaction.DRAIN_WAIT_S, compaction_hold.WIND_DOWN_S)
        self.assertGreater(compaction.PANE_RESUME_WAIT_S, compaction_hold.RESUME_S)
        self.assertLess(compaction.PANE_STOP_WAIT_S + compaction.DRAIN_WAIT_S + compaction.PANE_RESUME_WAIT_S, compaction_hold.MAX_AGE_S)

    def test_the_wait_returns_a_settled_answer_at_once_and_a_last_word_at_the_deadline(self):
        job = compaction.Runtime(self.cfg, run=self.fake, clock=self.clock, sleep=self.clock.sleep)
        record = job.claim(NOW)
        compaction_hold.save_panes(self.state, dict(id=record['id'], phase='stop', settled=False, agents=[]))
        answer = job.wait_for_panes('stop', compaction.PANE_STOP_WAIT_S)
        self.assertEqual(self.clock.now - NOW, compaction.PANE_STOP_WAIT_S)
        self.assertIn('had not settled', answer['error'])
        compaction_hold.save_panes(self.state, dict(id=record['id'], phase='stop', settled=True, agents=[]))
        before = self.clock.now
        self.assertTrue(job.wait_for_panes('stop', compaction.PANE_STOP_WAIT_S)['settled'])
        self.assertEqual(self.clock.now, before)
        self.assertIn('error', job.wait_for_panes('resume', compaction.PANE_RESUME_WAIT_S), 'the stop answer is not the resume answer')
        compaction_hold.save_panes(self.state, dict(id='another-night', phase='stop', settled=True, agents=[]))
        self.assertIn('error', job.wait_for_panes('stop', compaction.PANE_STOP_WAIT_S))

    def test_panes_off_waits_for_nothing_and_reports_nothing(self):
        self.cfg['panes']['enabled'] = False
        answer = self.run_job()
        self.assertEqual(self.waits, [])
        self.assertNotIn('panes', answer)
        self.assertEqual(answer['errors'], [])

    def test_the_answer_goes_with_the_hold(self):
        job = self.runtime()
        record = job.claim(NOW)
        compaction_hold.save_panes(self.state, dict(id=record['id']))
        self.assertEqual(compaction_hold.read_panes(self.state), dict(id=record['id']))
        job.release(record)
        self.assertIsNone(compaction_hold.read_panes(self.state))

    def test_a_new_hold_never_reads_a_dead_nights_answer(self):
        compaction_hold.save_panes(self.state, dict(id='dead', phase='stop', settled=True, agents=[]))
        self.runtime().claim(NOW)
        self.assertIsNone(compaction_hold.read_panes(self.state))

    def test_report_names_stopped_killed_resumed_and_reclaimed(self):
        answer = self.run_job()
        report = self.report()
        self.assertEqual(report['stopped'], [1])
        self.assertEqual(report['killed'], [dict(run=2)])
        self.assertEqual(report['panes']['agents'][0]['outcome'], 'resumed')
        self.assertEqual(report['panes']['agents'][0]['stopped'], 'stopped-asked')
        self.assertEqual((report['before_gb'], report['after_gb']), (answer['before_gb'], answer['after_gb']))
        self.assertEqual(self.outbox(), {})

    def test_a_clean_night_with_a_failed_resume_still_raises_a_notice(self):
        self.hub['resume'] = dict(id='h', phase='resume', settled=True, agents=[self.stopped('resume-failed', 'pane-agent-running')])
        answer = self.run_job()
        self.assertEqual((answer['status'], answer['errors']), ('compacted', []))
        notice, = self.outbox().values()
        self.assertIn('seat-x (wsl) did not resume: pane-agent-running', notice['notice']['body'])

    def test_a_live_agent_the_resume_could_not_tell_is_flagged_as_back(self):
        self.hub['resume'] = dict(id='h', phase='resume', settled=True, agents=[self.stopped('resumed-untold', 'focused')])
        self.run_job()
        notice, = self.outbox().values()
        self.assertIn('seat-x (wsl) is back but was not told to go on: focused', notice['notice']['body'])

    def test_a_row_the_hub_could_not_read_keeps_its_reason(self):
        self.hub['resume'] = dict(id='h', phase='resume', settled=True, agents=[self.stopped(None, 'herdr-unavailable')])
        answer = self.run_job()
        self.assertEqual((answer['panes']['agents'][0]['outcome'], answer['panes']['agents'][0]['reason']),
                         ('resume-failed', 'herdr-unavailable'))

    def test_a_stayed_stopped_agent_is_not_a_failed_resume(self):
        self.hub['resume'] = dict(id='h', phase='resume', settled=True, agents=[self.stopped('stayed-stopped', 'stopped')])
        answer = self.run_job()
        self.assertEqual(answer['panes']['agents'][0]['outcome'], 'stayed-stopped')
        self.assertEqual(self.outbox(), {})

    def test_wsl_shutdown_step_is_unaffected(self):
        steps = []

        class Watching(compaction.Runtime):
            def save(self, record):
                steps.append((record['step'], record['phase']))
                super().save(record)
        self.run_job(Watching)
        self.assertEqual(steps, [('stop', 'draining'), ('kill', 'draining'), ('trim', 'draining'), ('shutdown', 'offline'),
                                 ('compact', 'offline'), ('restart', 'offline'), ('resume', 'resuming'), ('release', 'resuming')])
        self.assertEqual(len(self.fake.seen('--shutdown')), 1)
        self.assertEqual(self.waits, [('stop', compaction.PANE_STOP_WAIT_S, 'draining'), ('resume', compaction.PANE_RESUME_WAIT_S, 'resuming')])

    def test_a_failed_restart_skips_the_resume_and_flags_every_row(self):
        self.fake.answers['--exec true'] = compaction.subprocess.SubprocessError('no WSL')
        answer = self.run_job()
        self.assertEqual([phase for phase, *_ in self.waits], ['stop'])
        self.assertEqual(answer['status'], 'failed')
        self.assertEqual(answer['panes']['agents'][0]['outcome'], 'resume-failed')
        self.assertEqual(answer['panes']['agents'][0]['reason'], 'WSL did not restart')
        self.assertIn('seat-x (wsl) did not resume', next(iter(self.outbox().values()))['notice']['body'])

    def test_a_resume_failure_never_stops_the_release(self):
        self.hub['resume'] = None
        with mock.patch.object(compaction, 'pane_resume', side_effect=ValueError('nope')):
            answer = self.run_job()
        self.assertIsNone(self.hold())
        self.assertIn('the pane step failed: ValueError: nope', answer['errors'])
        self.assertEqual(answer['panes']['agents'][0]['outcome'], 'resume-failed')

    def test_a_stop_failure_never_stops_the_job(self):
        with mock.patch.object(compaction, 'pane_stop', side_effect=ValueError('nope')):
            answer = self.run_job()
        self.assertTrue(self.fake.seen('--shutdown'))
        self.assertIn('the pane step failed: ValueError: nope', answer['errors'])

    def test_a_job_that_died_leaves_its_answer_in_the_report(self):
        record = self.runtime().claim(NOW)
        compaction_hold.save_panes(self.state, dict(id=record['id'], phase='stop', settled=False, agents=[self.stopped('left-for-kill')]))
        with mock.patch.object(compaction, 'holder_dead', return_value=True):
            compaction.recover(self.cfg)
        self.assertEqual(self.report()['panes']['agents'][0]['outcome'], 'left-for-kill')


if __name__ == '__main__':
    unittest.main()
