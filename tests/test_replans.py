"""Durable replan requests, bounded records, graph replacement and dispatch."""
import copy
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from drover import brief, config, contract, launch, pipeline, repos
from drover.ledger import connect, db, ops, states
from drover.port import DirectPort
from tests.test_waiting import LedgerCase, logical, require
from tests.test_pipeline import result
from tests.rcw_fixture import PipelineBase
from tests.tiers import integration

ROUTE = {'backend': 'codex', 'model': 'gpt-6.1-sol', 'seat': 'codex', 'effort': 'high'}
REVISE = {'action': 'revise', 'goal': 'Use the actual API', 'acceptance': ['works'], 'scope_files': ['src/*']}
OWNER = {'action': 'needs-owner', 'question': 'Which API should be supported?'}
SPLIT = {'action': 'split', 'tasks': [
    {'key': 'a', 'title': 'First', 'goal': 'foundation', 'acceptance': ['works'], 'scope_files': ['a/*'], 'deps': []},
    {'key': 'b', 'title': 'Second', 'goal': 'consumer', 'acceptance': ['works'], 'scope_files': ['b/*'], 'deps': ['a']},
]}


def inspected(case, task):
    from drover.replan import Replanning
    for key in ('worktree', 'branch'):
        if task.get(key) != case.get(task['id']).get(key):
            ops.update_task(case.conn, task['id'], **{key: task.get(key)})
    waiting = case.port.waiting
    if not any(r['task_id'] == task['id'] for r in waiting.replans()):
        waiting.request_replan(task['id'], 'owner-request')
    planning = Replanning(case.pipe)
    batch = planning.prepare(lambda candidate, inputs: ROUTE if candidate['id'] == task['id'] else None, now=1000)
    inputs = batch.members[0]['snapshot']['inputs']
    planning.finish(batch.run_id, {'status': contract.UNVERIFIED, 'text': '', 'usage': {}}, now=1000)
    waiting.expire(100000)
    return inputs


class Replans(LedgerCase):
    def inputs(self):
        return {'repo': 'p', 'branch': 'topic', 'head': 'a' * 40, 'dirty_digest': 'b' * 64,
                'checkout': str(Path(self.tmp.name)), 'read_checkouts': []}

    def request(self, **fields):
        w = self.waiting()
        tid = self.task(goal='Original', acceptance=['old acceptance'], scope_files=['src/*'], **fields)
        return w, tid, w.request_replan(tid, 'owner-request')

    def start(self, w, request, now=1000):
        started = w.start_replans([{'request_id': request['id'], 'inputs': self.inputs()}], ROUTE, now=now)
        self.assertIsInstance(started, dict)
        self.assertIsInstance(started['run_id'], int)
        return started

    def test_request_durable_unique(self):
        w, tid, request = self.request()
        self.assertEqual(w.request_replan(tid, 'blocker', {'blocker': {'detail': 'new'}})['id'], request['id'])
        ops.add_comment(self.conn, 'task', tid, 'owner', 'Reconsider', replan=True)
        self.assertEqual([r['id'] for r in w.replans()], [request['id']])
        self.conn.close()
        self.conn = connect(self.path)
        w = self.waiting()
        self.assertEqual([r['id'] for r in w.replans()], [request['id']])
        epic = ops.create_epic(self.conn, 'p', 'another')['id']
        member = self.task(epic_id=epic)
        ops.event(self.conn, 'replan-request', 'director', {}, epic_id=epic)
        self.assertIn(member, [r['task_id'] for r in w.replans()])
        terminal = self.task('cancelled')
        before = logical(self.conn)
        with self.assertRaises(ValueError):
            w.request_replan(terminal, 'owner-request')
        self.assertEqual(logical(self.conn), before)
        ops.event(self.conn, 'replan-request', 'owner', {}, task_id=terminal)
        self.assertNotIn(terminal, [r['task_id'] for r in w.replans()])
        self.assertIsNone(ops.claim_next(self.conn, {'actor': 'dispatcher'}))

    def test_request_fanout_and_urgent(self):
        w = self.waiting()
        epic = ops.create_epic(self.conn, 'p', 'overlap')['id']
        source = self.task(epic_id=epic, scope_files=['src/*'])
        dep, transitive = self.task(), self.task('proposed')
        ops.add_dep(self.conn, dep, source)
        ops.add_dep(self.conn, transitive, dep)
        overlap = self.task('draft', epic_id=epic, scope_files=['src/a'])
        archived = self.task(epic_id=epic, scope_files=['src/b'])
        ops.archive_task(self.conn, archived)
        disjoint = self.task(epic_id=epic, scope_files=['docs/*'])
        r = w.request_replan(source, 'blocker')
        subjects = {row['subject_key'] for row in w.pending() if row['cause'] == self.key('replan', r['id'])}
        self.assertEqual(subjects, {source, dep, transitive, overlap, archived})
        ops.add_comment(self.conn, 'task', source, 'owner', 'Pause followers', urgent=True)
        ops.add_comment(self.conn, 'epic', epic, 'owner', 'Pause epic', urgent=True)
        task_cause = self.key('urgent', 'task', source)
        epic_cause = self.key('urgent', 'epic', epic)
        self.assertTrue(any(row['cause'] == task_cause for row in w.pending(('task', transitive))))
        q = next(q for q in w.questions() if q['id'] == next(row['question_id'] for row in w.pending() if row['cause'] == epic_cause))
        ops.answer_question(self.conn, q['id'], choice='Resume work')
        self.assertFalse(any(row['cause'] == epic_cause for row in w.pending()))
        self.assertEqual(self.status(disjoint), 'ready')
        self.assertTrue(w.pending(('task', dep)))
        ops.set_status(self.conn, source, 'cancelled', actor='owner')
        self.assertEqual(w.pending(('task', dep)), [])
        self.assertEqual(self.status(transitive), 'proposed')
        self.assertIsNone(ops.get_task(self.conn, overlap)['approved_at'])

    def test_owner_ready_completes_replan_only(self):
        for actor in ('owner', 'director'):
            w, tid, r = self.request()
            follower = self.task()
            ops.add_dep(self.conn, follower, tid)
            # Request fanout is fixed at its event; register the follower before another episode.
            ops.set_status(self.conn, tid, 'ready', actor=actor)
            self.assertNotIn(r['id'], [q['id'] for q in w.replans()])
            self.assertEqual(self.status(tid), 'ready')
            r = w.request_replan(tid, 'owner-request')
            ops.block(self.conn, tid, 'independent manual', actor='owner')
            with self.assertRaises((ValueError, PermissionError)):
                ops.set_status(self.conn, tid, 'ready', actor=actor)
            self.assertEqual(self.status(tid), 'blocked')
            ops.unblock(self.conn, tid, actor='owner')
            ops.set_status(self.conn, tid, 'ready', actor=actor)
            self.assertEqual(self.status(follower), 'ready')

    def test_neutral_snapshot_deadline(self):
        w, tid, r = self.request()
        now = 1000
        for n, outcome in enumerate(('provider-error', 'launch-error', 'orphaned', 'unverified',
                                      'provider-error', 'provider-error', 'provider-error'), 1):
            sp = self.start(w, r, now)
            self.assertIsNone(w.start_replans([{'request_id': r['id'], 'inputs': self.inputs()}], ROUTE, now=now))
            fresh = self.inputs()
            if outcome == 'unverified':
                fresh['dirty_digest'] = 'c' * 64
            done = w.finish_replans(sp['run_id'], outcome, {w.replan_run(sp['run_id'])['members'][0]['task_id']: None}, None, fresh={r['id']: fresh}, now=now)[r['id']]
            self.assertEqual(ops.get_task(self.conn, tid)['replans_used'], 0)
            row, = [row for row in w.pending(('task', tid)) if row['kind'] == 'deadline']
            delay = min(1800, 60 * 2 ** min(n - 1, 5))
            self.assertEqual(row['timeout_at'], now + delay)
            self.conn.close()
            self.conn = connect(self.path)
            w = self.waiting()
            self.assertEqual([row['timeout_at'] for row in w.pending(('task', tid)) if row['kind'] == 'deadline'], [now + delay])
            self.assertEqual(w.finish_replans(sp['run_id'], outcome, {w.replan_run(sp['run_id'])['members'][0]['task_id']: None}, None, fresh={r['id']: fresh}, now=now + 1)[r['id']], done)
            now += delay
            w.expire(now)
        for changed in ('goal', 'deps', 'branch', 'read', 'dirty'):
            sp = self.start(w, r, now)
            inputs = self.inputs()
            if changed == 'goal':
                ops.update_task(self.conn, tid, goal='changed while planner ran')
            elif changed == 'deps':
                ops.add_dep(self.conn, tid, self.task('cancelled'))
            else:
                inputs[{'branch': 'head', 'read': 'read_checkouts', 'dirty': 'dirty_digest'}[changed]] = (
                    [{'entry': {'repo': 'p'}, 'path': self.tmp.name, 'sha': 'b' * 40}] if changed == 'read' else 'd' * 40)
            w.finish_replans(sp['run_id'], 'done', {w.replan_run(sp['run_id'])['members'][0]['task_id']: REVISE}, None, fresh={r['id']: inputs}, now=now)[r['id']]
            self.assertNotEqual(ops.get_task(self.conn, tid)['goal'], REVISE['goal'])
            w.expire(now + 1800)
            now += 1800
        before = logical(self.conn)
        # Force a real database failure rather than mocking a private Waiting helper.
        self.conn.execute("CREATE TRIGGER refuse_planner BEFORE INSERT ON runs WHEN new.role='planner' BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite_error()):
            w.start_replans([{'request_id': r['id'], 'inputs': self.inputs()}], ROUTE, now=now)
        self.assertEqual(logical(self.conn), before)

    def test_replan_release_and_owner_replacement(self):
        for record in (REVISE, OWNER):
            w = self.waiting()
            tid = self.task(goal='old', acceptance=['old'], scope_files=['src/*'], branch='topic')
            ops.update_task(self.conn, tid, worktree=self.tmp.name)
            pin = {'backend': 'codex', 'model': 'gpt-6-luna'}
            ops.update_task(self.conn, tid, model_pin=pin)
            follower = self.task()
            ops.add_dep(self.conn, follower, tid)
            r = w.request_replan(tid, 'owner-request')
            approved = ops.get_task(self.conn, tid)['approved_at']
            sp = self.start(w, r)
            out = w.finish_replans(sp['run_id'], 'done', {w.replan_run(sp['run_id'])['members'][0]['task_id']: record}, None, fresh={r['id']: self.inputs()}, now=1001)[r['id']]
            task = ops.get_task(self.conn, tid)
            self.assertEqual((task['branch'], task['worktree'], task['approved_at'], task['model_pin']),
                             ('topic', self.tmp.name, approved, pin))
            if record['action'] == 'revise':
                self.assertEqual((task['goal'], task['acceptance'], task['scope_files']),
                                 (record['goal'], record['acceptance'], record['scope_files']))
                self.assertEqual([self.status(t) for t in (tid, follower)], ['ready'] * 2)
            else:
                self.assertEqual(self.status(tid), 'needs-owner')
                self.assertEqual({row['kind'] for row in w.pending(('task', follower))}, {'owner-answer'})
                ops.block(self.conn, follower, 'independent', actor='owner')
                q, = ops.open_questions(self.conn)
                ops.answer_question(self.conn, q['id'], text='Use the stable API')
                self.assertEqual(self.status(tid), 'ready')
                self.assertEqual(self.status(follower), 'blocked')
            self.assertEqual(w.finish_replans(sp['run_id'], 'done', {w.replan_run(sp['run_id'])['members'][0]['task_id']: record}, None, fresh={r['id']: self.inputs()})[r['id']], out)

    def test_limits_invalid_timeout_and_reset(self):
        w = self.waiting()
        for invalid in ('', '-1', '+1', '1.5', 'true', ' 2 '):
            _, tid, r = self.request()
            ops.set_setting(self.conn, 'replan_limit', invalid)
            before = logical(self.conn)
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                w.start_replans([{'request_id': r['id'], 'inputs': self.inputs()}], ROUTE)
            self.assertEqual(logical(self.conn), before)
            ops.set_status(self.conn, tid, 'cancelled', actor='owner')
        for limit in (0, 1, 2):
            ops.set_setting(self.conn, 'replan_limit', str(limit))
            _, tid, r = self.request()
            for n in range(limit):
                sp = self.start(w, r)
                out = w.finish_replans(sp['run_id'], 'timeout' if n == 0 else 'no-record', {w.replan_run(sp['run_id'])['members'][0]['task_id']: None}, None, fresh={r['id']: self.inputs()})[r['id']]
                self.assertEqual(ops.get_task(self.conn, tid)['replans_used'], n + 1)
                self.assertEqual(w.finish_replans(sp['run_id'], 'timeout', {w.replan_run(sp['run_id'])['members'][0]['task_id']: None}, None, fresh={r['id']: self.inputs()})[r['id']], out)
            self.assertIsNone(w.start_replans([{'request_id': r['id'], 'inputs': self.inputs()}], ROUTE))
            self.assertEqual(self.status(tid), 'needs-owner')
            q = next(q for q in ops.open_questions(self.conn) if q['target_id'] == tid)
            ops.answer_question(self.conn, q['id'], text='Revise the requirements')
            self.assertEqual(ops.get_task(self.conn, tid)['replans_used'], 0)
        ops.set_setting(self.conn, 'replan_limit', '2')
        _, tid, r = self.request()
        sp = self.start(w, r)
        w.finish_replans(sp['run_id'], 'done', {w.replan_run(sp['run_id'])['members'][0]['task_id']: REVISE}, None, fresh={r['id']: self.inputs()})[r['id']]
        self.assertEqual(ops.get_task(self.conn, tid)['replans_used'], 1)
        # Successful replanning consumes budget but only an external owner action resets it.
        self.assertEqual(ops.get_task(self.conn, tid)['retry_after_run'], 0)

    def split_fixture(self):
        w = self.waiting()
        prerequisite = self.task('cancelled')
        source = self.task(branch='topic')
        ops.update_task(self.conn, source, worktree=self.tmp.name)
        follower = self.task()
        ops.add_dep(self.conn, source, prerequisite, kind='waits-for')
        ops.add_dep(self.conn, follower, source, kind='blocks')
        r = w.request_replan(source, 'owner-request')
        return w, source, follower, prerequisite, r, self.start(w, r)

    def test_split_graph_rollback_release(self):
        w, tid, follower, prerequisite, r, sp = self.split_fixture()
        out = w.finish_replans(sp['run_id'], 'done', {w.replan_run(sp['run_id'])['members'][0]['task_id']: SPLIT}, None, fresh={r['id']: self.inputs()})[r['id']]
        a, b = out['children']
        self.assertEqual(len(set(out['children'])), 2)
        for child in (a, b):
            task = ops.get_task(self.conn, child)
            self.assertEqual(task['status'], 'proposed')
            self.assertIsNone(task['approved_at'])
            self.assertIn({'depends_on': prerequisite, 'kind': 'waits-for'}, ops.deps_of(self.conn, child))
        self.assertIn({'depends_on': a, 'kind': 'blocks'}, ops.deps_of(self.conn, b))
        for child in (a, b):
            self.assertIn({'depends_on': child, 'kind': 'blocks'}, ops.deps_of(self.conn, follower))
        self.assertEqual(self.status(tid), 'cancelled')
        self.assertEqual(ops.get_task(self.conn, tid)['worktree'], self.tmp.name)
        ops.set_status(self.conn, a, 'cancelled', actor='owner')
        self.assertEqual(self.status(follower), 'blocked')
        ops.set_status(self.conn, b, 'cancelled', actor='owner')
        self.assertEqual(self.status(follower), 'ready')
        for deps in (['b'], ['missing'], ['a']):
            w, tid, follower, prerequisite, r, sp = self.split_fixture()
            invalid = copy.deepcopy(SPLIT)
            invalid['tasks'][0]['deps'] = deps
            before = {t['id'] for t in ops.list_tasks(self.conn)}
            w.finish_replans(sp['run_id'], 'done', {w.replan_run(sp['run_id'])['members'][0]['task_id']: invalid}, None, fresh={r['id']: self.inputs()})[r['id']]
            self.assertEqual({t['id'] for t in ops.list_tasks(self.conn)}, before)
            self.assertNotEqual(self.status(tid), 'cancelled')

    def test_split_collision_replay(self):
        w, tid, follower, prerequisite, r, sp = self.split_fixture()
        out = w.finish_replans(sp['run_id'], 'done', {w.replan_run(sp['run_id'])['members'][0]['task_id']: SPLIT}, None, fresh={r['id']: self.inputs()})[r['id']]
        before = logical(self.conn)
        self.assertEqual(w.finish_replans(sp['run_id'], 'done', {w.replan_run(sp['run_id'])['members'][0]['task_id']: SPLIT}, None, fresh={r['id']: self.inputs()})[r['id']], out)
        self.assertEqual(logical(self.conn), before)
        self.assertEqual({d['kind'] for d in ops.deps_of(self.conn, follower) if d['depends_on'] in out['children']}, {'blocks'})
        w, tid, follower, prerequisite, r, sp = self.split_fixture()
        before = logical(self.conn)
        self.conn.execute("CREATE TRIGGER refuse_child BEFORE INSERT ON tasks BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite_error()):
            w.finish_replans(sp['run_id'], 'done', {w.replan_run(sp['run_id'])['members'][0]['task_id']: SPLIT}, None, fresh={r['id']: self.inputs()})[r['id']]
        self.assertEqual(logical(self.conn), before)


def sqlite_error():
    import sqlite3
    return sqlite3.DatabaseError


class PlannerDispatchCase(PipelineBase):
    def setUp(self):
        super().setUp()
        # Publish the same configured repos to ledger access validation as to Pipeline.
        self.repo_registry = patch('drover.repos.load_all', return_value=self.pipe.all_repos())
        self.repo_registry.start()
        self.addCleanup(self.repo_registry.stop)
        # Model the launcher's public adoption protocol with the same process handles.
        self.fl.adopt = lambda spec, started=None: self.fl.handles[spec['run_id']]

    def waiting(self):
        self.assertTrue(hasattr(self.port, 'waiting'), 'DirectPort.waiting interface is absent')
        return self.port.waiting

    def planners(self):
        return self.fl.of('planner')


def time_now(d):
    return d.now()


class PlannerSplitBounds(LedgerCase):
    inputs = Replans.inputs
    request = Replans.request
    start = Replans.start

    def test_split_cannot_discard_required_acceptance_or_scope(self):
        for field in ('acceptance', 'scope_files'):
            w, tid, request = self.request()
            started = self.start(w, request)
            record = copy.deepcopy(SPLIT)
            record['tasks'][0][field] = []
            before = {t['id'] for t in ops.list_tasks(self.conn)}
            w.finish_replans(started['run_id'], contract.DONE, {w.replan_run(started['run_id'])['members'][0]['task_id']: record}, None, fresh={request['id']: self.inputs()})[request['id']]
            self.assertEqual({t['id'] for t in ops.list_tasks(self.conn)}, before)
            self.assertNotEqual(self.status(tid), 'cancelled')


class SingletonCharacterization(LedgerCase):
    """Freeze request semantics before replacing the singleton execution path."""
    inputs = Replans.inputs
    request = Replans.request
    start = Replans.start

    def test_three_actions_preserve_approval_and_authority(self):
        for record, expected in ((REVISE, states.READY), (SPLIT, states.CANCELLED),
                                 (OWNER, states.NEEDS_OWNER)):
            with self.subTest(action=record['action']):
                w, tid, request = self.request()
                approved = ops.get_task(self.conn, tid)['approved_at']
                started = self.start(w, request)
                out = w.finish_replans(started['run_id'], contract.DONE, {w.replan_run(started['run_id'])['members'][0]['task_id']: record}, None, fresh={request['id']: self.inputs()}, now=1001)[request['id']]
                self.assertEqual(self.status(tid), expected)
                self.assertEqual(ops.get_task(self.conn, tid)['approved_at'], approved)
                self.assertEqual(ops.get_task(self.conn, tid)['replans_used'], 1)
                if record['action'] == 'split':
                    self.assertEqual(len(out['children']), 2)
                    for child in out['children']:
                        self.assertEqual(self.status(child), states.PROPOSED)
                        self.assertIsNone(ops.get_task(self.conn, child)['approved_at'])
                elif record['action'] == 'revise':
                    self.assertEqual(ops.get_task(self.conn, tid)['goal'], REVISE['goal'])
                else:
                    question, = [q for q in ops.open_questions(self.conn) if q['target_id'] == tid]
                    self.assertEqual(question['title'], OWNER['question'])

    def test_malformed_output_is_charged_and_limit_requires_owner(self):
        w, tid, request = self.request()
        ops.set_setting(self.conn, 'replan_limit', '1')
        self.assertIsNone(contract.parse_replan_record('{"action":"revise"'))
        started = self.start(w, request)
        w.finish_replans(started['run_id'], contract.NO_RECORD, {w.replan_run(started['run_id'])['members'][0]['task_id']: None}, None, fresh={request['id']: self.inputs()}, now=1001)[request['id']]
        self.assertEqual(ops.get_task(self.conn, tid)['goal'], 'Original')
        self.assertEqual(ops.get_task(self.conn, tid)['replans_used'], 1)
        self.assertIsNone(w.start_replans([{'request_id': request['id'], 'inputs': self.inputs()}], ROUTE, now=1002))
        self.assertEqual(self.status(tid), states.NEEDS_OWNER)
        question, = ops.open_questions(self.conn)
        ops.answer_question(self.conn, question['id'], text='Try the supported interface')
        self.assertEqual(ops.get_task(self.conn, tid)['replans_used'], 0)

    def test_neutral_backoff_is_released_only_by_its_deadline(self):
        w, tid, request = self.request()
        for now, outcome, deadline in ((1000, contract.PROVIDER_ERROR, 1060),
                                       (1060, contract.LAUNCH_ERROR, 1180),
                                       (1180, contract.ORPHANED, 1420)):
            started = self.start(w, request, now)
            w.finish_replans(started['run_id'], outcome, {w.replan_run(started['run_id'])['members'][0]['task_id']: None}, None, fresh={request['id']: self.inputs()}, now=now)[request['id']]
            self.assertEqual(ops.get_task(self.conn, tid)['replans_used'], 0)
            self.assertEqual([r['timeout_at'] for r in w.pending(('task', tid))
                              if r['kind'] == 'deadline'], [deadline])
            w.expire(deadline - 1)
            self.assertIsNone(w.start_replans([{'request_id': request['id'], 'inputs': self.inputs()}], ROUTE, now=deadline - 1))
            w.expire(deadline)
            self.assertFalse(any(r['kind'] == 'deadline' for r in w.pending(('task', tid))))

    def test_changed_snapshot_never_applies_late_revision(self):
        for field, value in (('head', 'c' * 40), ('branch', 'refs/heads/other'),
                             ('dirty_digest', 'd' * 64), ('read_checkouts', [
                                 {'entry': {'repo': 'p'}, 'path': self.tmp.name, 'sha': 'e' * 40}])):
            with self.subTest(field=field):
                w, tid, request = self.request()
                started = self.start(w, request)
                w.finish_replans(started['run_id'], contract.DONE, {w.replan_run(started['run_id'])['members'][0]['task_id']: REVISE}, None, fresh={request['id']: {**self.inputs(), field: value}}, now=1001)[request['id']]
                self.assertEqual(ops.get_task(self.conn, tid)['goal'], 'Original')
                self.assertEqual(ops.get_task(self.conn, tid)['replans_used'], 0)
                self.assertEqual(ops.list_runs(self.conn, tid)[0]['outcome'], contract.UNVERIFIED)

    def test_cancelled_request_reaps_without_revision(self):
        w, tid, request = self.request()
        started = self.start(w, request)
        ops.set_status(self.conn, tid, states.CANCELLED, actor='owner')
        w.finish_replans(started['run_id'], contract.DONE, {w.replan_run(started['run_id'])['members'][0]['task_id']: REVISE}, None, fresh={request['id']: self.inputs()}, now=1001)[request['id']]
        self.assertEqual(self.status(tid), states.CANCELLED)
        self.assertEqual(ops.get_task(self.conn, tid)['goal'], 'Original')
        self.assertIsNotNone(ops.list_runs(self.conn, tid)[0]['ended'])
        self.assertEqual(w.pending(('task', tid)), [])

    def test_replay_preserves_children_events_counters_and_usage(self):
        w, tid, request = self.request()
        started = self.start(w, request)
        usage = dict(tokens_in=101, tokens_out=37, tokens_cached=11)
        first = w.finish_replans(started['run_id'], contract.DONE, {w.replan_run(started['run_id'])['members'][0]['task_id']: SPLIT}, usage, fresh={request['id']: self.inputs()}, now=1001)[request['id']]
        before = logical(self.conn)
        second = w.finish_replans(started['run_id'], contract.DONE, {w.replan_run(started['run_id'])['members'][0]['task_id']: SPLIT}, usage, fresh={request['id']: self.inputs()}, now=1002)[request['id']]
        self.assertEqual(second, first)
        self.assertEqual(logical(self.conn), before)
        run, = ops.list_runs(self.conn, tid)
        self.assertEqual((run['tokens_in'], run['tokens_out'], run['tokens_cached']), (101, 37, 11))
