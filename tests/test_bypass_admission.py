"""A yanked task starts nothing new, whatever step it had reached, and a landing already on its way is recorded."""
import json
import unittest
import unittest.mock

from drover import launch
from drover.ledger import IllegalTransition, ops
from tests.bypass_fixture import YankBase
from tests.landing_fixture import MemoryLandingCase
from tests.ledger_fixture import SHA, stage
from tests.test_dispatcher import AdoptHandle, DONE, FakeHandle, res


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
