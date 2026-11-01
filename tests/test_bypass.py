"""The owner's Bypass in the ledger: yank, abandon and handback."""
import json
import unittest
from pathlib import Path
from unittest import mock

from drover.ledger import IllegalTransition, disk_notices, ops, states
from drover.ledger.ops_bypass import abandon, handback, yank
from drover.ledger.waiting import TASK_TERMINAL, Waiting, cause_key
from tests import ledger_fixture
from tests.test_ledger import Base

OPEN = (states.PROPOSED, states.DRAFT, states.READY, states.CLAIMED, states.RUNNING, states.GATED,
        states.REVIEWED, states.BLOCKED, states.FAILED, states.NEEDS_OWNER)


class BypassBase(Base):
    def at(self, state):
        """A task standing in `state`, reached through the real lifecycle."""
        if state == states.PROPOSED:
            return self.task(status='proposed')['id']
        if state == states.DRAFT:
            return self.task(status='draft')['id']
        tid = self.task()['id']
        if state in (states.CLAIMED, states.RUNNING, states.GATED, states.REVIEWED):
            ledger_fixture.stage(self.conn, tid, state)
        elif state == states.BLOCKED:
            ops.set_status(self.conn, tid, states.BLOCKED, reason='fixture')
        elif state == states.FAILED:
            ops.set_status(self.conn, tid, states.BLOCKED, reason='fixture')
            ops.set_status(self.conn, tid, states.FAILED)
        elif state == states.NEEDS_OWNER:
            ops.set_status(self.conn, tid, states.NEEDS_OWNER)
        self.assertEqual(self.status(tid), state)
        return tid

    def kinds(self, tid, kind):
        return ops.events(self.conn, task_id=tid, kind=kind)

    def terminal_wait(self, subject, source):
        Waiting(self.conn, 'owner').wait(('task', subject), TASK_TERMINAL, cause_key(source),
                                         cause=cause_key('task', source), data=dict(source=source, mode='blocked'))


class StateTableTests(BypassBase):
    def test_yanked_sits_outside_the_other_groups(self):
        self.assertEqual(states.YANKED, 'yanked')
        self.assertIn(states.YANKED, states.STATES)
        for group in (states.TERMINAL, states.ACTIVE, states.PENDING):
            self.assertNotIn(states.YANKED, group)
        self.assertEqual(states.CLOSED, (*states.TERMINAL, states.YANKED))

    def test_every_open_state_may_be_yanked_by_the_owner_only(self):
        for state in OPEN:
            self.assertIn(states.YANKED, states.TRANSITIONS[state], state)
            self.assertIn((state, states.YANKED), states.OWNER_ONLY, state)
        for state in states.TERMINAL:
            self.assertNotIn(states.YANKED, states.TRANSITIONS[state], state)

    def test_a_yanked_task_only_lands_or_is_cancelled(self):
        self.assertEqual(states.TRANSITIONS[states.YANKED], {states.LANDED, states.CANCELLED})
        self.assertIn((states.YANKED, states.CANCELLED), states.OWNER_ONLY)
        self.assertNotIn((states.YANKED, states.LANDED), states.OWNER_ONLY)


class YankTests(BypassBase):
    def test_yank_is_owner_only(self):
        tid = self.task()['id']
        for actor in ('dispatcher', 'director', 'system'):
            with self.assertRaises(PermissionError):
                yank(self.conn, tid, actor=actor)
            with self.assertRaises(PermissionError):
                ops.set_status(self.conn, tid, states.YANKED, actor=actor)
        self.assertEqual(self.status(tid), states.READY)
        yank(self.conn, tid)
        with self.assertRaises(PermissionError):
            yank(self.conn, tid, actor='director')

    def test_yank_writes_exactly_one_bypass_event(self):
        tid = self.task()['id']
        with ops.tx(self.conn):
            self.conn.execute("UPDATE tasks SET worktree='H:/wt/one' WHERE id=?", (tid,))
        task = yank(self.conn, tid, reason='doing it by hand')
        self.assertEqual(task['status'], states.YANKED)
        records = self.kinds(tid, 'bypass')
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['actor'], 'owner')
        self.assertEqual(records[0]['data'], {'from': 'ready', 'reason': 'doing it by hand', 'worktree': 'H:/wt/one'})

    def test_yank_from_every_open_state(self):
        for state in OPEN:
            with self.subTest(state=state):
                tid = self.at(state)
                self.assertEqual(yank(self.conn, tid)['status'], states.YANKED)
                self.assertEqual(self.kinds(tid, 'bypass')[0]['data']['from'], state)

    def test_yank_of_landed_or_cancelled_refused(self):
        landed = self.task()['id']
        ledger_fixture.stage(self.conn, landed, 'landed')
        cancelled = self.task()['id']
        ops.set_status(self.conn, cancelled, states.CANCELLED, actor='owner')
        for tid in (landed, cancelled):
            with self.assertRaises(IllegalTransition):
                yank(self.conn, tid)
            self.assertEqual(self.kinds(tid, 'bypass'), [])

    def test_second_yank_is_idempotent_and_writes_nothing(self):
        tid = self.task()['id']
        yank(self.conn, tid, reason='first')
        before = len(ops.events(self.conn, task_id=tid))
        self.assertEqual(yank(self.conn, tid, reason='second')['status'], states.YANKED)
        self.assertEqual(len(ops.events(self.conn, task_id=tid)), before)

    def test_yanked_is_never_claimed(self):
        tid = self.task()['id']
        yank(self.conn, tid)
        self.assertEqual(ops.ready_tasks(self.conn), [])
        self.assertIsNone(ops.claim_next(self.conn, self.worker()))
        for target in (states.READY, states.DRAFT, states.BLOCKED, states.NEEDS_OWNER):
            with self.assertRaises(IllegalTransition):
                ops.set_status(self.conn, tid, target, actor='owner', reason='fixture')

    def test_dependent_stays_blocked_while_yanked(self):
        source = self.task()['id']
        follower = self.task(deps=[source])['id']
        held = self.task()['id']
        self.terminal_wait(held, source)
        yank(self.conn, source)
        self.assertEqual(ops.ready_tasks(self.conn), [])
        self.assertIsNone(ops.claim_next(self.conn, self.worker()))
        self.assertEqual(self.status(held), states.BLOCKED)
        self.assertEqual(self.status(follower), states.READY)

    def test_yanked_scope_no_longer_blocks_new_work(self):
        running = self.task(scope_files=['shared/file'])['id']
        ledger_fixture.stage(self.conn, running, 'running')
        waiting = self.task(scope_files=['shared/file'])['id']
        self.assertEqual(ops.ready_tasks(self.conn), [])
        yank(self.conn, running)
        self.assertEqual([t['id'] for t in ops.ready_tasks(self.conn)], [waiting])

    def test_snapshot_marks_yanked_terminal_and_not_live(self):
        tid = self.task()['id']
        with ops.tx(self.conn):
            self.conn.execute("UPDATE tasks SET worktree='H:/wt/one' WHERE id=?", (tid,))
        kept = self.task()['id']
        yank(self.conn, tid)
        state = Path(self.tmp.name) / 'state'
        with mock.patch('drover.config.state_dir', return_value=state):
            disk_notices.replay(self.conn, {}, 1000)
        snapshot = json.loads((state / 'disk' / 'tasks.json').read_text())
        rows = {t['id']: t for t in snapshot['tasks']}
        self.assertTrue(rows[tid]['terminal'])
        self.assertFalse(rows[tid]['live'])
        self.assertEqual(rows[tid]['state'], states.YANKED)
        self.assertTrue(rows[kept]['live'])
        self.assertEqual(snapshot['active_worktrees'], [])


class ClosedSubjectTests(BypassBase):
    """Waits and replans never move a yanked task, and never fail because of one."""

    def test_urgent_epic_comment_with_a_yanked_task_does_not_raise(self):
        epic = ops.create_epic(self.conn, 'p', 'epic')['id']
        tid = self.task(epic_id=epic)['id']
        yank(self.conn, tid)
        ops.add_comment(self.conn, 'epic', epic, 'owner', 'stop everything', urgent=True)
        self.assertEqual(self.status(tid), states.YANKED)

    def test_releasing_a_wait_left_on_a_yanked_task_leaves_it_yanked(self):
        tid = self.at(states.BLOCKED)
        yank(self.conn, tid)
        ops.unblock(self.conn, tid)
        self.assertEqual(self.status(tid), states.YANKED)

    def test_a_replan_is_refused_and_a_queued_one_never_runs(self):
        queued = self.task()['id']
        Waiting(self.conn, 'owner').request_replan(queued, 'fixture')
        yank(self.conn, queued)
        waiting = Waiting(self.conn, 'owner')
        request = next(r for r in waiting.replans() if r['task_id'] == queued)
        followers, order = waiting._replan_order()
        self.assertFalse(waiting._replan_eligible(request, {request['id']}, followers, order))
        fresh = self.task()['id']
        yank(self.conn, fresh)
        with self.assertRaises(IllegalTransition):
            waiting.request_replan(fresh, 'fixture')

    def test_the_board_counts_yanked(self):
        from drover.ledger import board
        yank(self.conn, self.task()['id'])
        self.assertIn('| yanked | 1 |', board.render(self.conn))

    def test_a_yanked_dependent_stops_adding_pressure(self):
        source = self.task()['id']
        follower = self.task(deps=[source])['id']
        before = next(r for r in ops._candidates(self.conn) if r['id'] == source)
        self.assertEqual(before['dependents'], 1)
        yank(self.conn, follower)
        after = next(r for r in ops._candidates(self.conn) if r['id'] == source)
        self.assertEqual(after['dependents'], 0)


class AbandonTests(BypassBase):
    def test_abandon_cancels_and_fires_the_terminal_path(self):
        source = self.task()['id']
        held = self.task()['id']
        self.terminal_wait(held, source)
        yank(self.conn, source)
        self.assertEqual(self.status(held), states.BLOCKED)
        self.assertEqual(abandon(self.conn, source)['status'], states.CANCELLED)
        self.assertEqual(Waiting(self.conn).pending(('task', held)), [])
        self.assertEqual(self.status(held), states.READY)

    def test_abandon_needs_the_owner_and_a_yanked_task(self):
        tid = self.task()['id']
        with self.assertRaises(IllegalTransition):
            abandon(self.conn, tid)
        self.assertEqual(self.status(tid), states.READY)
        yank(self.conn, tid)
        with self.assertRaises(PermissionError):
            abandon(self.conn, tid, actor='director')
        self.assertEqual(self.status(tid), states.YANKED)


class HandbackTests(BypassBase):
    def yanked(self, **kw):
        epic = ops.create_epic(self.conn, 'p', 'epic')['id']
        fields = dict(title='original', epic_id=epic, repo_path='H:/repos/p', goal='the goal', acceptance=['it works'],
                      scope_files=['src/a.py'], priority='P1', size='L', risk='high', **kw)
        tid = self.task(**fields)['id']
        yank(self.conn, tid)
        return tid

    def test_handback_creates_a_draft_task_and_cancels_the_old_one(self):
        old = self.yanked()
        new = handback(self.conn, old)
        self.assertNotEqual(new['id'], old)
        self.assertEqual(new['status'], states.DRAFT)
        self.assertEqual(self.status(old), states.CANCELLED)
        source = ops.get_task(self.conn, old)
        for field in ('title', 'goal', 'acceptance', 'scope_files', 'repo', 'repo_path', 'epic_id', 'priority', 'size',
                      'risk'):
            self.assertEqual(new[field], source[field], field)
        self.assertEqual(self.kinds(old, 'handback')[0]['data'], {'to': new['id']})
        self.assertEqual(self.kinds(new['id'], 'handback')[0]['data'], {'from': old})

    def test_handback_retargets_blocks_deps_so_dependents_keep_waiting(self):
        old = self.yanked()
        follower = self.task(deps=[old])['id']
        also = self.task(deps=[dict(depends_on=old, kind='waits-for')])['id']
        self.terminal_wait(follower, old)
        new = handback(self.conn, old)['id']
        self.assertEqual(ops.deps_of(self.conn, follower), [dict(depends_on=new, kind='blocks')])
        self.assertEqual(ops.deps_of(self.conn, also), [dict(depends_on=new, kind='waits-for')])
        self.assertEqual(ops.dependents_of(self.conn, old), [])
        self.assertNotIn(follower, [t['id'] for t in ops.ready_tasks(self.conn)])

    def test_handback_needs_owner_approval_to_run(self):
        new = handback(self.conn, self.yanked())
        self.assertIsNone(new['approved_at'])
        self.assertEqual(ops.ready_tasks(self.conn), [])
        with self.assertRaises(PermissionError):
            ops.set_status(self.conn, new['id'], states.READY, actor='dispatcher')
        self.assertEqual(ops.approve(self.conn, new['id'])['status'], states.READY)

    def test_handback_keeps_branch_and_worktree_fields_empty(self):
        old = self.yanked(branch='feat/original')
        with ops.tx(self.conn):
            self.conn.execute("UPDATE tasks SET worktree='H:/wt/one' WHERE id=?", (old,))
        new = handback(self.conn, old)
        self.assertIsNone(new['branch'])
        self.assertIsNone(new['worktree'])

    def test_handback_needs_the_owner_and_a_yanked_task(self):
        ready = self.task()['id']
        with self.assertRaises(IllegalTransition):
            handback(self.conn, ready)
        old = self.yanked()
        count = len(ops.list_tasks(self.conn))
        with self.assertRaises(PermissionError):
            handback(self.conn, old, actor='director')
        handback(self.conn, old)
        with self.assertRaises(IllegalTransition):
            handback(self.conn, old)
        self.assertEqual(len(ops.list_tasks(self.conn)), count + 1)

    def test_a_failed_handback_leaves_nothing_behind(self):
        old = self.yanked()
        follower = self.task(deps=[old])['id']
        count = len(ops.list_tasks(self.conn))
        with mock.patch('drover.ledger.ops_bypass.set_status', side_effect=ValueError('boom')):
            with self.assertRaises(ValueError):
                handback(self.conn, old)
        self.assertEqual(self.status(old), states.YANKED)
        self.assertEqual(len(ops.list_tasks(self.conn)), count)
        self.assertEqual(ops.deps_of(self.conn, follower), [dict(depends_on=old, kind='blocks')])


if __name__ == '__main__':
    unittest.main()
