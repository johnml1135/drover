"""A yank stops the run wherever it is: the dispatcher cancels it, ends it `yanked` and moves nothing."""
import json
import unittest
import unittest.mock

from drover import launch
from drover.ledger import ops
from tests.bypass_fixture import YankBase
from tests.ledger_fixture import SHA, stage
from tests.test_dispatcher import AdoptHandle, DONE, FakeHandle, res


class StoppingTheRun(YankBase):
    def test_tick_cancels_the_run_of_a_yanked_task(self):
        tid, rid = self.start()
        self.yank(tid)
        out = self.d.tick()
        self.assertEqual(out['yanked'], [rid])
        self.assertEqual(self.fl.cancelled, [(rid, self.nonce(rid))])
        self.assertEqual(self.d.cancelling[rid][0], tid)
        self.assertEqual(self.d._stop_yanked(), [])              # once asked, the scan asks again
        self.d.tick()
        self.assertEqual(self.fl.cancelled, [(rid, self.nonce(rid))])
        self.assertEqual(self.fl.retried, [(rid, self.nonce(rid))])
        self.assertEqual(len(self.events('launch-cancel')), 1)

    def test_paused_dispatch_still_stops_it(self):
        tid, rid = self.start()
        self.yank(tid)
        ops.set_setting(self.conn, 'dispatch_enabled', 'false')
        out = self.d.tick()
        self.assertTrue(out['paused'])
        self.assertEqual(self.fl.cancelled, [(rid, self.nonce(rid))])

    def archive(self, tid):
        """The archive refuses a task with an open run, so this is a planner's run or older data."""
        self.conn.execute('UPDATE tasks SET archived=1 WHERE id=?', (tid,))

    def test_an_archived_yanked_task_is_stopped_too(self):
        tid, rid = self.start()
        self.yank(tid)
        self.archive(tid)
        self.assertEqual(self.d._stop_yanked(), [rid])
        self.assertEqual(self.fl.cancelled, [(rid, self.nonce(rid))])

    def test_an_archived_yanked_task_is_adopted_and_closed_after_a_restart(self):
        tid, rid = self.start()
        self.yank(tid)
        self.archive(tid)
        d2 = self.restart()
        d2.tick()
        self.assertIn(rid, d2.live)
        self.assertEqual(self.fl.cancelled, [(rid, self.nonce(rid))])
        self.found.clear()
        d2.tick()
        d2.tick()
        self.assert_stopped(tid, rid)

    def test_stop_still_records_the_yank(self):
        """A STOP returns from the tick early; the yank must already have been asked for."""
        tid, rid = self.start()
        self.yank(tid)
        refused = dict(allowed=False, epoch=1, reason='stop')
        with unittest.mock.patch.object(self.port, 'admission', return_value=refused):
            out = self.d.tick()
        self.assertEqual(out['control'], refused)
        self.assertEqual(out['yanked'], [rid])
        self.assertEqual([e['data']['run'] for e in self.events('launch-cancel')], [rid])
        self.assertIn(rid, self.d.cancelling)

    def test_a_task_with_no_run_asks_for_nothing(self):
        tid = self.task()
        self.yank(tid)
        self.assertEqual(self.d._stop_yanked(), [])
        self.assertEqual(self.fl.cancelled, [])


class WhenTheRunEnds(YankBase):
    def test_worker_exit_after_yank_ends_the_run_and_leaves_status(self):
        for result in (res('done', DONE), res('rotate'), res('error')):
            tid, rid = self.start()
            self.yank(tid)
            self.d.tick()
            self.fl.handles[-1].result = result
            with unittest.mock.patch.object(self.pipe, 'commit_ready') as commit:
                self.d.tick()
            commit.assert_not_called()
            self.assert_stopped(tid, rid)
            self.assertNotIn(rid, self.d.live)
            self.assertEqual(self.events('worker-done'), [])

    def test_gate_finish_after_yank_leaves_status(self):
        tid, rid = self.start()
        self.fl.handles[0].result = res('done', DONE)
        self.d.tick()
        gate = self.run_of(tid, 'gate')['id']
        self.assertIn(gate, self.d.live)
        self.yank(tid)
        self.d.tick()
        self.assert_stopped(tid, gate)
        self.assertEqual(self.run_of(tid)['outcome'], 'done')    # never amended to gated
        self.assertNotIn(tid, self.d.ci_runners.tickets)

    def test_gate_of_unknown_termination_keeps_its_slot_after_yank(self):
        tid, rid = self.start()
        self.fl.handles[0].result = res('done', DONE)
        self.d.tick()
        gate = self.run_of(tid, 'gate')['id']
        self.yank(tid)
        self.found.pop(gate, None)
        unknown = {'outcome': 'termination-unknown'}
        with unittest.mock.patch.object(self.pipe.ci, 'result', return_value=unknown):
            self.d.tick()
            self.d.tick()
        self.assertIsNone(self.run_of(tid, 'gate')['ended'])
        self.assertIn(gate, self.d.live)
        self.assertIn(tid, self.d.ci_runners.tickets)

    def _review(self):
        tid = self.task(size='S')
        stage(self.conn, tid, 'gated')
        worker = self.run_of(tid)['id']
        rid = ops.start_run(self.conn, tid, 'review', 'codex', 'm')
        self.port.append_event('review-started', {'run': rid, 'sha': SHA, 'worker_run': worker}, tid)
        return tid, rid

    def test_review_finish_after_yank_leaves_status(self):
        tid, rid = self._review()
        self.yank(tid)
        text = json.dumps({'status': 'done', 'sha': SHA, 'verdict': f'LAND@{SHA}', 'findings': []})
        self.d.live[rid] = (FakeHandle(res('done', text)), tid, {'role': 'review', 'backend': 'codex', 'model': 'm'})
        self.d.tick()
        self.assert_stopped(tid, rid)

    def test_a_yank_during_the_finish_is_settled_by_its_retry(self):
        tid, rid = self.start()
        self.fl.handles[0].result = res('rotate')
        real = self.port.finish_run

        def yank_after_finish(*args, **kw):
            finished = real(*args, **kw)
            self.yank(tid)
            return finished
        with unittest.mock.patch.object(self.port, 'finish_run', side_effect=yank_after_finish):
            self.d.reap()
        self.assertIn(rid, self.d.live)                          # the move failed; the next reap settles it
        self.assertEqual(self.d.reap(), [rid])
        self.assertEqual(self.status(tid), 'yanked')

    def test_giving_up_on_a_finish_moves_nothing(self):
        tid, rid = self.start()
        self.yank(tid)
        self.d._give_up(rid, tid, RuntimeError('boom'))
        self.assert_stopped(tid, rid)
        self.assertEqual([e for e in self.events('finish-error') if e['data'].get('gave_up')], [])

    def test_seat_slot_is_released(self):
        tid, rid = self.start()
        self.yank(tid)
        d2 = self.restart()
        d2.tick()
        self.assertEqual(sum(d2._seat_load().values()), 1)
        self.found.clear()                                       # the runner dies with no terminal status
        d2.tick()
        d2.tick()
        self.assert_stopped(tid, rid)
        self.assertEqual(d2._seat_load(), {})
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM runs WHERE ended IS NULL').fetchone()[0], 0)


class AfterARestart(YankBase):
    def test_restart_after_yank_kills_the_orphan_and_does_not_requeue(self):
        tid, rid = self.start()
        self.yank(tid)
        launched = len(self.fl.specs)
        d2 = self.restart()
        d2.tick()
        self.assertIn(rid, d2.live)
        self.assertEqual(self.fl.cancelled, [(rid, self.nonce(rid))])
        self.found.clear()
        d2.tick()
        d2.tick()
        self.assert_stopped(tid, rid)
        self.assertNotIn(rid, d2.live)
        self.assertEqual(len(self.fl.specs), launched)

    def test_a_terminal_status_found_at_restart_ends_the_run_yanked(self):
        tid, rid = self.start()
        self.yank(tid)
        self.fl.adopt_with[rid] = AdoptHandle(result=res('done', DONE))
        d2 = self.restart()
        d2.tick()
        d2.tick()
        self.assert_stopped(tid, rid)

    def test_an_unfinished_cancel_survives_the_restart(self):
        tid, rid = self.start()
        self.yank(tid)
        self.d.tick()
        d2 = self.restart()
        d2.tick()
        self.assertEqual(d2.cancelling[rid][0], tid)
        self.assertEqual(len(self.fl.cancelled), 1)


class DuringTheLaunch(YankBase):
    def launch_fails(self, error):
        """The owner yanks while the launch is on its way, and the launch fails."""
        def launch_call(spec):
            self.yank(spec['task']['id'])
            raise error
        self.fl.launch = launch_call
        tid = self.task(size='S')
        self.d.tick()
        return tid, self.run_of(tid)['id']

    def test_a_failed_launch_moves_nothing(self):
        self.assert_stopped(*self.launch_fails(launch.LaunchError('no runner')))

    def test_a_held_launch_moves_nothing(self):
        self.assert_stopped(*self.launch_fails(launch.AdmissionHeld('held')))

    def cancelled_launch(self, readings):
        """A launch whose stop was asked for, with the yank read as `readings` say, one reading per call."""
        error = launch.LaunchError('no runner')
        error.cancel_requested = True
        self.fl.launch = unittest.mock.Mock(side_effect=error)
        tid = self.task(size='S')
        with unittest.mock.patch.object(self.d, '_is_yanked', side_effect=readings):
            self.d.tick()
        run = self.run_of(tid)
        return run, self.d.cancelling

    def test_a_yank_landing_after_the_read_does_not_change_the_answer(self):
        run, cancelling = self.cancelled_launch([False, True, True, True])
        self.assertEqual(run['outcome'], 'launch-error')
        self.assertIn(run['id'], cancelling)

    def test_a_yank_read_once_is_acted_on_in_both_steps(self):
        run, cancelling = self.cancelled_launch([True, False, False, False])
        self.assertEqual(run['outcome'], 'yanked')
        self.assertIn(run['id'], cancelling)


if __name__ == '__main__':
    unittest.main()
