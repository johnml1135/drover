"""A yank stops the run wherever it is: the dispatcher cancels it, ends it `yanked` and moves nothing."""
import json
import unittest
import unittest.mock

from drover import launch
from drover.ledger import IllegalTransition, ops
from tests.landing_fixture import MemoryLandingCase
from tests.ledger_fixture import SHA, stage
from tests.test_dispatcher import AdoptHandle, AdoptLauncher, Base, DONE, FakeHandle, res


class RecordingLauncher(AdoptLauncher):
    def __init__(self):
        super().__init__()
        self.cancelled, self.retried = [], []

    def cancel(self, run_id, nonce=None):
        self.cancelled.append((run_id, nonce))
        return True

    def stop_run(self, run_id, nonce=None):
        self.retried.append((run_id, nonce))
        return True


class YankBase(Base):
    def setUp(self):
        super().setUp()
        self.fl = RecordingLauncher()
        self.allow['codex'] = False
        self.found = {}
        self.d = self.restart()

    def restart(self):
        return self.make(launcher=self.fl, scan=lambda want_wsl: self.found, scan_every_s=0)

    def start(self):
        """A task with a worker running: the scan sees its runner until a test says otherwise."""
        tid = self.task(size='S')
        self.d.tick()
        run = self.run_of(tid)
        self.found[run['id']] = 'windows'
        return tid, run['id']

    def run_of(self, tid, role='worker'):
        return next(r for r in ops.list_runs(self.conn, tid) if r['role'] == role)

    def yank(self, tid):
        ops.yank(self.conn, tid, reason='stop')

    def nonce(self, rid):
        return next(e['data']['nonce'] for e in self.events('run-launching') if e['data']['run'] == rid)

    def assert_stopped(self, tid, rid):
        run = next(r for r in ops.list_runs(self.conn, tid) if r['id'] == rid)
        self.assertEqual(run['outcome'], 'yanked')
        self.assertIsNotNone(run['ended'])
        self.assertEqual(self.status(tid), 'yanked')


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


class BetweenClaimAndLaunch(YankBase):
    def assert_nothing_started(self, tid, status='yanked'):
        self.assertEqual(self.status(tid), status)
        self.assertEqual(ops.list_runs(self.conn, tid), [])
        self.assertEqual(self.fl.specs, [])
        self.assertEqual(self.d.live, {})
        self.assertEqual(self.d._seat_load(), {})

    def test_yank_between_claim_and_launch_starts_nothing(self):
        """The status is read just before the run would start."""
        tid = self.task(size='S')
        real = self.pipe.worktree

        def worktree(task, where=None):
            self.yank(task['id'])
            return real(task, where)
        self.pipe.worktree = worktree
        with unittest.mock.patch.object(self.port, 'start_run') as start:
            self.d.tick()
        start.assert_not_called()
        self.assert_nothing_started(tid)

    def settled_during_worktree(self, settle):
        tid = self.task(size='S')
        real = self.pipe.worktree

        def worktree(task, where=None):
            self.yank(task['id'])
            settle(self.conn, task['id'])
            return real(task, where)
        self.pipe.worktree = worktree
        self.d.tick()
        self.assert_nothing_started(tid, 'cancelled')

    def test_abandon_between_claim_and_launch_starts_nothing(self):
        self.settled_during_worktree(ops.abandon)

    def test_handback_between_claim_and_launch_starts_nothing(self):
        self.settled_during_worktree(ops.handback)

    def test_the_ledger_refuses_a_settled_task_after_the_read(self):
        tid = self.task(size='S')
        real = self.pipe.worktree

        def worktree(task, where=None):
            self.yank(task['id'])
            ops.abandon(self.conn, task['id'])
            return real(task, where)
        self.pipe.worktree = worktree
        with unittest.mock.patch.object(self.d, '_is_closed', side_effect=[False, True]):
            self.d.tick()
        self.assert_nothing_started(tid, 'cancelled')

    def test_a_ledger_refusal_of_the_run_starts_nothing(self):
        """The other order: the yank lands after that read, and the ledger refuses the run."""
        tid = self.task(size='S')

        def refused(*args, **kw):
            self.yank(tid)
            raise IllegalTransition('illegal transition yanked -> running')
        with unittest.mock.patch.object(self.port, 'start_run', side_effect=refused):
            self.d.tick()
        self.assert_nothing_started(tid)

    def test_a_yank_during_admission_refuses_its_move_without_ending_the_tick(self):
        tid = self.task(size='S')

        def route(*args):
            self.yank(tid)
            return {'needs_owner': True, 'reason': 'owner must look'}
        with unittest.mock.patch.object(self.d, '_route', side_effect=route):
            self.d.tick()
        self.assert_nothing_started(tid)

    def test_the_real_ledger_refusal_starts_nothing(self):
        """A yank after the dispatcher's own read: ops.start_run refuses the worker run."""
        tid = self.task(size='S')
        real = self.pipe.worktree

        def worktree(task, where=None):
            self.yank(task['id'])
            return real(task, where)
        self.pipe.worktree = worktree
        with unittest.mock.patch.object(self.d, '_is_closed', side_effect=[False, True]):
            self.d.tick()
        self.assert_nothing_started(tid)

    def test_a_refusal_of_another_kind_still_raises(self):
        self.task(size='S')
        with unittest.mock.patch.object(self.port, 'start_run', side_effect=ValueError('not a yank')):
            with self.assertRaises(ValueError):
                self.d.tick()


class BeforeAGateOrReview(YankBase):
    """The ledger refuses worker runs only: these paths read the status themselves."""
    def worker_done(self):
        tid, rid = self.start()
        self.fl.handles[0].result = res('done', DONE)
        return tid

    def assert_no_run_of(self, tid, role):
        self.assertEqual(self.status(tid), 'yanked')
        self.assertEqual([r for r in ops.list_runs(self.conn, tid) if r['role'] == role], [])
        self.assertEqual([r for r in ops.list_runs(self.conn, tid) if r['ended'] is None], [])
        self.assertEqual(self.d.live, {})

    def test_a_yank_as_the_gate_takes_its_slot_starts_no_gate(self):
        tid = self.worker_done()
        real = self.d.ci_runners.reserve

        def reserve(task, **kw):
            ticket = real(task, **kw)
            self.yank(task['id'])
            return ticket
        launch_call = unittest.mock.Mock(side_effect=AssertionError('no gate may launch'))
        self.fl.launch = launch_call
        with unittest.mock.patch.object(self.d.ci_runners, 'reserve', side_effect=reserve) as reserved:
            self.d.tick()
        reserved.assert_called_once()
        launch_call.assert_not_called()
        self.assert_no_run_of(tid, 'gate')
        self.assertNotIn(tid, self.d.ci_runners.tickets)
        self.assertEqual(self.events('gate-error'), [])

    def test_a_settled_task_starts_no_gate(self):
        tid = self.worker_done()
        real = self.d.ci_runners.reserve

        def reserve(task, **kw):
            ticket = real(task, **kw)
            self.conn.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (task['id'],))
            return ticket
        with unittest.mock.patch.object(self.d.ci_runners, 'reserve', side_effect=reserve):
            self.d.tick()
        self.assertEqual([r for r in ops.list_runs(self.conn, tid) if r['role'] == 'gate'], [])
        self.assertNotIn(tid, self.d.ci_runners.tickets)

    def test_a_gate_launch_that_may_still_come_up_keeps_its_slot_after_yank(self):
        tid = self.worker_done()

        def launch_call(spec):
            self.yank(tid)
            raise launch.LaunchError('no answer', cancel_requested=True)
        self.fl.launch = launch_call
        self.d.tick()
        gate = self.run_of(tid, 'gate')['id']
        self.assert_stopped(tid, gate)
        self.assertIn(tid, self.d.ci_runners.tickets)
        self.assertEqual(self.d.cancelling[gate][0], tid)
        self.assertEqual(self.events('launch-cancel')[-1]['data']['run'], gate)
        self.d.tick()                                            # a clean scan shows no runner
        self.assertNotIn(tid, self.d.ci_runners.tickets)

    def test_a_yank_as_the_review_prepares_starts_no_review(self):
        self.allow['codex'] = True
        tid = self.worker_done()
        self.d.tick()                                            # reaps the worker, starts the gate
        self.assertIn(self.run_of(tid, 'gate')['id'], self.d.live)
        launched = len(self.fl.specs)

        def diff_text(repo, base, sha):
            self.yank(tid)
            return 'fixture diff'
        with unittest.mock.patch.object(self.pipe, 'diff_text', side_effect=diff_text) as diff:
            self.d.tick()                                        # reaps the gate; the review would start here
        diff.assert_called_once()
        self.assert_no_run_of(tid, 'review')
        self.assertEqual(len(self.fl.specs), launched)
        self.assertEqual(self.events('review-error'), [])


class Landing(MemoryLandingCase):
    def test_landing_batch_drops_a_task_yanked_before_it_forms(self):
        kept = self.task('kept', {'x.txt': 'x\n'})
        gone = self.task('gone', {'z.txt': 'z\n'})
        ops.yank(self.conn, gone, reason='stop')
        res = self.land()
        self.assertEqual((res['outcome'], res['batch'], res['landed']), ('landed', [kept], [kept]))
        self.assertEqual(self.status(gone), 'yanked')

    def yank_during_gate(self, tid):
        def gate(checkout, commit, tasks):
            ops.yank(self.conn, tid, reason='stop')
            return self.r.gate(checkout, commit, tasks)
        self.gate = gate

    def test_batch_in_flight_when_yanked_lands_or_refuses_without_raising(self):
        kept = self.task('kept', {'x.txt': 'x\n'})
        gone = self.task('gone', {'z.txt': 'z\n'})
        self.yank_during_gate(gone)
        res = self.land()
        self.assertEqual((res['outcome'], res['landed']), ('landed', [kept]))
        self.assertEqual(res['dropped'], {gone: 'yanked'})
        self.assertEqual(self.status(gone), 'yanked')
        self.assertNotIn('z.txt', self.r.nodes[self.r.heads['main']][1])

    def test_a_lone_yanked_task_leaves_main_alone(self):
        before = self.r.heads['main']
        gone = self.task('gone', {'z.txt': 'z\n'})
        self.yank_during_gate(gone)
        res = self.land()
        self.assertEqual((res['outcome'], res['landed'], res['tip']), ('yanked', [], None))
        self.assertEqual(self.r.heads['main'], before)
        self.assertEqual(self.status(gone), 'yanked')

    def test_a_yank_after_the_last_read_drops_the_task_when_main_has_not_moved(self):
        kept = self.task('kept', {'x.txt': 'x\n'})
        gone = self.task('gone', {'z.txt': 'z\n'})
        real = self.r.sync
        done = []

        def sync(*args, **kw):        # _land's sync is the last step before the fast-forward
            head = real(*args, **kw)
            if self.r.work_active and not done:
                done.append(ops.yank(self.conn, gone, reason='stop'))
            return head
        from drover import origin
        with unittest.mock.patch.object(origin, 'sync', side_effect=sync):
            res = self.land()
        self.assertEqual((res['outcome'], res['landed']), ('landed', [kept]))
        self.assertEqual(res['dropped'], {gone: 'yanked'})
        self.assertNotIn('z.txt', self.r.nodes[self.r.heads['main']][1])

    def test_a_task_already_on_main_records_its_landing(self):
        gone = self.task('gone', {'z.txt': 'z\n'})
        real = self.r.advance

        def advance(repo, branch, new, old, **kw):
            real(repo, branch, new, old, **kw)
            if new != old:
                ops.yank(self.conn, gone, reason='stop')
        from drover import origin
        with unittest.mock.patch.object(origin, 'fast_forward', side_effect=advance):
            res = self.land()
        self.assertEqual((res['outcome'], res['landed']), ('landed', [gone]))
        self.assertEqual(self.status(gone), 'landed')

    def test_a_red_gate_never_requeues_a_task_yanked_during_attribution(self):
        gone = self.task('gone', {'bad.txt': 'BAD\n'})

        def attribute(*args):
            ops.yank(self.conn, gone, reason='stop')
            return gone, 'decider', 0
        with unittest.mock.patch('drover.landing.base_status', return_value='green'), \
                unittest.mock.patch('drover.landing.attribute', side_effect=attribute):
            res = self.land()
        self.assertEqual(self.status(gone), 'yanked')
        self.assertEqual(res['landed'], [])


if __name__ == '__main__':
    unittest.main()
