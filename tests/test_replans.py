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


class Records(PipelineBase):
    def test_parser_bounds_and_brief(self):
        parse = require(self, 'drover.contract', 'parse_replan_record')
        assemble = lambda task, runs, notes, inputs: require(self, 'drover.brief', 'assemble_replans')([
            dict(task=task, runs=runs, notes=notes, trigger=None, inputs=inputs)])
        for record in (REVISE, OWNER, SPLIT):
            self.assertEqual(parse('Planner observations\n' + json.dumps(record) + '\n'), record)
        invalid = [
            {}, {'action': 'delete'}, {**REVISE, 'branch': 'main'}, {**OWNER, 'question': ''},
            {**OWNER, 'question': 'x' * 2001}, {**REVISE, 'goal': 'x' * 12001},
            {**REVISE, 'acceptance': ['a'] * 31}, {**REVISE, 'acceptance': ['a' * 1001]},
            {**REVISE, 'scope_files': ['../secret']}, {**REVISE, 'scope_files': ['/etc/passwd']},
            {**REVISE, 'scope_files': ['C:\\owner']}, {**REVISE, 'scope_files': ['x' * 241]},
            {**SPLIT, 'tasks': SPLIT['tasks'][:1]}, {**SPLIT, 'tasks': SPLIT['tasks'] * 5},
            {**SPLIT, 'tasks': [SPLIT['tasks'][0], {**SPLIT['tasks'][1], 'key': 'a'}]},
            {**SPLIT, 'tasks': [{**SPLIT['tasks'][0], 'deps': ['a']}, SPLIT['tasks'][1]]},
            {**SPLIT, 'tasks': [{**SPLIT['tasks'][0], 'deps': ['unknown']}, SPLIT['tasks'][1]]},
            {**SPLIT, 'tasks': [SPLIT['tasks'][0], {**SPLIT['tasks'][1], 'deps': ['a', 'a']}]},
            {**SPLIT, 'tasks': [{**SPLIT['tasks'][0], 'key': 'Upper'}, SPLIT['tasks'][1]]},
            {'nested': OWNER},
        ]
        for record in invalid:
            with self.subTest(record=repr(record)[:150]):
                self.assertIsNone(parse(json.dumps(record)))
        for text in (json.dumps(OWNER) + '\ntrailing text', '{bad json',
                     json.dumps(OWNER) + json.dumps(OWNER), json.dumps({**OWNER, 'question': 'é' * 20000})):
            self.assertIsNone(parse(text))
        self.assertEqual(parse('x' * 40000 + '\n' + json.dumps(OWNER)), OWNER)
        task = self.get(self.task(goal='MANDATORY GOAL', acceptance=['MANDATORY ACCEPTANCE'], scope_files=['src/*']))
        inputs = {'repo': 'p', 'branch': 'topic', 'head': 'a' * 40, 'dirty_digest': 'b' * 64,
                  'checkout': str(self.fix.repo), 'read_checkouts': []}
        built = assemble(task, [{'id': n, 'note': 'history' * 1000, 'gate_receipt': None} for n in range(50)],
                         ['finding' * 1000] * 50, inputs)
        prompt = built['user_prompt']
        self.assertIn('MANDATORY GOAL', prompt)
        self.assertIn('MANDATORY ACCEPTANCE', prompt)
        self.assertIn('topic', prompt)
        self.assertIn('a' * 40, prompt)
        self.assertIn('read-only', (built['system_append'] + prompt).lower())
        self.assertIn('needs-owner', prompt)
        self.assertLessEqual(built['tokens'], 15000)
        with self.assertRaises(brief.BriefTooLarge):
            assemble({**task, 'goal': 'required spec ' * 100000}, [], [], inputs)

    @integration
    def test_inputs_detect_dirty_and_branch_changes(self):
        method = require(self, 'drover.replan', 'Replanning')
        self.assertTrue(callable(method), 'Replanning interface is absent')
        cfg = config.validate({'paths': {'state_dir': str(Path(self.tmp.name) / 'state')}})
        config.use(cfg)
        self.addCleanup(config.reset)
        tid = self.task()
        checkout = Path(self.pipe.worktree(self.get(tid)))
        initial = inspected(self, self.get(tid))
        self.assertEqual(initial['head'], self.fix.git('rev-parse', 'HEAD', cwd=checkout))
        head = initial['head']
        digests = [initial['dirty_digest']]
        for filename, content, staged in [('base.txt', 'changed', False), ('base.txt', 'changed twice', False),
                                          ('base.txt', 'staged now', True), ('new.txt', 'one', False),
                                          ('new.txt', 'two', False)]:
            (checkout / filename).write_text(content)
            if staged:
                self.fix.git('add', filename, cwd=checkout)
            current = inspected(self, self.get(tid))
            self.assertEqual(current['head'], head)
            self.assertNotEqual(current['dirty_digest'], digests[-1])
            digests.append(current['dirty_digest'])
        outside = Path(self.tmp.name) / 'outside'
        outside.write_text('owner bytes')
        link = checkout / 'link'
        try:
            link.symlink_to(outside)
        except OSError as exc:
            # Git on Windows can still exercise tracked/index/untracked digests above.
            self.assertIn(exc.errno, (1, 13, 1314))
        else:
            symlink = inspected(self, self.get(tid))
            outside.write_text('changed outside checkout')
            self.assertEqual(inspected(self, self.get(tid))['dirty_digest'], symlink['dirty_digest'])
            link.unlink()
            link.symlink_to(Path(self.tmp.name) / 'another')
            self.assertNotEqual(inspected(self, self.get(tid))['dirty_digest'], symlink['dirty_digest'])
        owner_head = self.fix.git('rev-parse', 'HEAD')
        owner_index = self.fix.git('ls-files', '--stage')
        self.fix.commit('new-head.txt', cwd=checkout)
        self.assertNotEqual(inspected(self, self.get(tid))['head'], head)
        read_repo = self.fix.repo
        read_task = self.task()
        before = inspected(self, self.get(read_task))
        self.fix.commit('read-moved.txt')
        after = inspected(self, self.get(read_task))
        self.assertNotEqual(before['read_checkouts'], after['read_checkouts'])
        owner_head = self.fix.git('rev-parse', 'HEAD')
        owner_index = self.fix.git('ls-files', '--stage')
        owner_status = self.fix.git('status', '--porcelain')
        missing = {**self.get(tid), 'worktree': str(Path(self.tmp.name) / 'missing')}
        fallback = inspected(self, missing)
        self.assertNotEqual(fallback['checkout'], str(read_repo))
        self.assertEqual(self.fix.git('rev-parse', 'HEAD'), owner_head)
        self.assertEqual(self.fix.git('ls-files', '--stage'), owner_index)
        self.assertEqual(self.fix.git('status', '--porcelain'), owner_status)

        # Same commit, different branch must still invalidate the planner input.
        branch_tid = self.task()
        checkout = self.pipe.worktree(self.get(branch_tid))
        branch_task = self.get(branch_tid)
        before = inspected(self, branch_task)
        self.fix.git('switch', '-c', 'different-branch', cwd=checkout)
        after = inspected(self, branch_task)
        self.assertEqual(before['head'], after['head'])
        self.assertNotEqual(before['branch'], after['branch'])


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


class Dispatch(PlannerDispatchCase):
    def test_request_to_each_result(self):
        w = self.waiting()
        for record in (REVISE, SPLIT, OWNER):
            tid = self.task(goal='old', acceptance=['old'], scope_files=['src/*'])
            ops.add_comment(self.conn, 'task', tid, 'owner', 'Replan this', replan=True)
            self.d.tick()
            sp = next(sp for sp in self.planners() if sp['task']['id'] == tid)
            self.assertFalse(sp['write'])
            self.fl.handles[sp['run_id']].result = result('no-record', json.dumps(record))
            self.d.tick()
            runs = [r for r in ops.list_runs(self.conn, tid) if r['role'] == 'planner']
            self.assertEqual([r['outcome'] for r in runs], ['done'])
            self.assertEqual(self.status(tid), {'revise': 'ready', 'split': 'cancelled', 'needs-owner': 'needs-owner'}[record['action']])
            self.assertFalse(any(sp['task']['id'] == tid for sp in self.fl.of('worker')))
            if record['action'] == 'revise':
                ops.archive_task(self.conn, tid)
        self.assertTrue(any(r['state'] == 'owner' for r in w.replans()))

    def test_admission_readonly_and_fallback(self):
        w = self.waiting()
        tid = self.task()
        w.request_replan(tid, 'owner-request')
        ops.update_task(self.conn, tid, model_pin={'backend': 'codex', 'model': 'gpt-6-luna'})
        ops.set_setting(self.conn, 'dispatch_enabled', 'false')
        self.d.tick()
        self.assertEqual(self.planners(), [])
        ops.set_setting(self.conn, 'dispatch_enabled', 'true')
        self.d = self.make(resources=lambda **kw: {'verdict': 'queue', 'slots': 0, 'reasons': []})
        self.d.tick()
        self.assertEqual(self.planners(), [])
        self.d = self.make(allow=lambda backend: False)
        self.d.tick()
        self.assertEqual(self.planners(), [])
        self.d = self.make()
        self.d.tick()
        self.d.tick()
        sp, = self.planners()
        self.assertNotEqual(sp['model'], 'gpt-6-luna')
        self.assertFalse(sp['write'])
        self.assertEqual((sp['wall_s'], sp['idle_s']), (600, 120))
        for backend in ('claude', 'codex'):
            argv, stdin = launch.build_argv({**sp, 'backend': backend, 'write': True})
            joined = ' '.join(argv)
            self.assertNotIn('dangerously-skip-permissions', joined)
            if backend == 'codex':
                self.assertIn('read-only', argv)
            else:
                settings = json.loads(argv[argv.index('--settings') + 1])
                self.assertNotIn('Edit', settings['permissions']['allow'])
                self.assertNotIn('Write', settings['permissions']['allow'])
                self.assertIn('Edit', settings['permissions']['deny'])
                self.assertIn('Write', settings['permissions']['deny'])
        self.fl.handles[sp['run_id']].result = {**result('error', json.dumps(REVISE)), 'provider_error': 'provider down'}
        self.d.tick()
        self.assertEqual(self.get(tid)['replans_used'], 0)
        self.assertEqual([r['outcome'] for r in ops.list_runs(self.conn, tid)], ['provider-error'])
        # The planner uses the existing director fallback chain, including the personal
        # Claude seat before the final Codex seat, independently of a worker pin.
        cfg = config.validate({'seats': [
            {'name': 'claude', 'backend': 'claude', 'director': True, 'placement': 'windows'},
            {'name': 'personal', 'backend': 'claude', 'priority': 2, 'placement': 'windows'},
            {'name': 'codex', 'backend': 'codex', 'priority': 3, 'placement': 'wsl'},
        ]})
        config.use(cfg)
        self.addCleanup(config.reset)
        from drover import route
        heads = {name: {'remaining_pct': 90} for name in ('claude', 'personal', 'codex')}
        for seat in ('claude', 'personal', 'codex'):
            chosen = route.choose({'role': 'planner', 'model_pin': {'backend': 'codex', 'model': 'gpt-6-luna'}},
                                  {}, seats=cfg['seats'], seat_headroom=heads)
            self.assertEqual(chosen['seat'], seat)
            self.assertNotEqual(chosen['model'], 'gpt-6-luna')
            heads[seat] = {'remaining_pct': 0}
        self.assertFalse(route.choose({'role': 'planner'}, {}, seats=cfg['seats'], seat_headroom=heads).get('backend'))
        # Resource slots are shared across requests, rather than counted per task.
        ops.add_policy_version(self.conn, 'planner-cap', {'parallel_cap': 1})
        for _ in range(2):
            candidate = self.task()
            w.request_replan(candidate, 'owner-request')
        cap = self.make(resources=lambda **kw: {'verdict': 'go', 'slots': 1, 'reasons': []})
        previous = len(self.planners())
        cap.tick()
        cap.tick()
        self.assertEqual(len(self.planners()) - previous, 1)



class Recovery(PlannerDispatchCase):
    def test_active_worker_and_restart(self):
        w = self.waiting()
        tid = self.task()
        self.d.tick()
        worker, = self.fl.of('worker')
        request = w.request_replan(tid, 'owner-request')
        self.d.tick()
        self.assertEqual(self.planners(), [])
        self.run_worker(tid, self.worker_commits(tid))
        self.d.tick()
        self.assertEqual(self.fl.of('gate'), [])
        sp, = self.planners()
        restarted = self.make()
        restarted.recover_orphans()
        restarted.tick()
        self.assertEqual(len(self.planners()), 1)
        self.assertEqual(next(r for r in w.replans() if r['id'] == request['id'])['current_run'], sp['run_id'])
        self.assertEqual(len([r for r in ops.list_runs(self.conn, tid) if r['role'] == 'worker']), 1)

    def test_orphan_cancel_and_snapshot_change(self):
        w = self.waiting()
        tid = self.task()
        w.request_replan(tid, 'owner-request')
        self.d.tick()
        sp, = self.planners()
        ops.update_task(self.conn, tid, goal='changed during planning')
        self.fl.handles[sp['run_id']].result = result('no-record', json.dumps(REVISE))
        self.d.tick()
        self.assertEqual(self.get(tid)['goal'], 'changed during planning')
        self.assertEqual(self.get(tid)['replans_used'], 0)
        self.assertEqual(ops.list_runs(self.conn, tid)[-1]['outcome'], 'unverified')
        w.expire(time_now(self.d) + 1800)
        self.d.tick()
        latest = self.planners()[-1]
        # Two clean scans without the planner process make it orphaned, never worker-ready.
        self.fl.scan = lambda want_wsl=False: {}
        restarted = self.make(scan_every_s=0)
        restarted.recover_orphans()
        restarted.watch_runs()
        self.assertEqual(ops.list_runs(self.conn, tid)[-1]['outcome'], 'orphaned')
        self.assertEqual(self.get(tid)['replans_used'], 0)
        self.fl.handles[latest['run_id']].result = result('error')
        self.assertTrue(any(row['kind'] == 'deadline' for row in w.pending(('task', tid))))
        w.expire(time_now(self.d) + 3600)
        self.fl.scan = lambda want_wsl=False: {sp['run_id']: sp['placement'] for sp in self.planners()
                                              if self.fl.handles[sp['run_id']].poll() is None}
        self.d = self.make()
        self.d.tick()
        latest = self.planners()[-1]
        ops.set_status(self.conn, tid, 'cancelled', actor='owner')
        self.fl.handles[latest['run_id']].result = result('no-record', json.dumps(REVISE))
        self.d.tick()
        self.assertEqual(self.status(tid), 'cancelled')
        self.assertEqual(w.replans(), [])
        self.assertEqual(w.pending(('task', tid)), [])


def time_now(d):
    return d.now()

class PlannerCommandExecution(unittest.TestCase):
    def test_composed_planner_argv_runs_with_readonly_permissions(self):
        for backend in ('claude', 'codex'):
            spec = dict(role='planner', write=True, backend=backend,
                        model='gpt-6.1-sol' if backend == 'codex' else 'opus-5.5', effort='high',
                        cwd='/planner', placement='windows', system_append='planner policy', user_prompt='plan only')
            argv, stdin = launch.build_argv(spec)
            self.assertEqual(stdin, 'plan only')
            if backend == 'codex':
                self.assertEqual(argv[argv.index('-s') + 1], 'read-only')
            else:
                permissions = json.loads(argv[argv.index('--settings') + 1])['permissions']
                self.assertTrue({'Edit', 'Write', 'Bash'} <= set(permissions['deny']))
                self.assertFalse({'Edit', 'Write', 'Bash'} & set(permissions['allow']))

class PlannerRegressions(PlannerDispatchCase):
    def test_cancelled_launch_stays_guarded_across_restart(self):
        w = self.waiting()
        tid = self.task()
        w.request_replan(tid, 'owner-request')
        original = self.fl.launch
        def uncertain(spec):
            original(spec)
            raise launch.LaunchError('runner may still exist', cancel_requested=True)
        self.fl.launch = uncertain
        self.d.tick()
        self.fl.launch = original
        w.expire(self.d.now() + 1800)
        restarted = self.make(scan_every_s=0)
        restarted.tick()
        self.assertEqual(len(self.planners()), 1)
        self.fl.scan = lambda want_wsl=False: {}
        restarted.tick()
        self.assertEqual(len(self.planners()), 2)
        latest = self.planners()[-1]
        self.fl.handles[latest['run_id']].result = result('no-record', json.dumps(REVISE))
        restarted.tick()
        w.request_replan(tid, 'owner-request')
        again = self.make(scan_every_s=0)
        again.tick()
        self.assertEqual(len(self.planners()), 3)



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

class PlannerPredecessors(PlannerDispatchCase):
    def test_worker_error_during_request_is_superseded_without_retry(self):
        tid = self.task()
        self.d.tick()
        worker, = self.fl.of('worker')
        self.waiting().request_replan(tid, 'owner-request')
        self.fl.handles[worker['run_id']].result = result('no-record')
        self.d.tick()
        self.assertEqual(ops.list_runs(self.conn, tid)[0]['outcome'], 'unverified')
        self.assertEqual(ops.events(self.conn, task_id=tid, kind='finish-error'), [])
        self.assertEqual(len(self.planners()), 1)

    def test_started_gate_and_review_finish_before_planner(self):
        for stage in ('gate', 'review'):
            with self.subTest(stage=stage):
                tid = self.task()
                self.d.tick()
                sha = self.worker_commits(tid, stage + '.txt')
                self.run_worker(tid, sha)
                self.d.tick()
                if stage == 'review':
                    self.d.tick()
                spec = next(s for s in reversed(self.fl.of(stage)) if s['task']['id'] == tid)
                completed = self.fl.handles[spec['run_id']].result
                self.fl.handles[spec['run_id']].result = None
                request = self.waiting().request_replan(tid, 'owner-request')
                self.d.tick()
                self.assertFalse(any(s['task']['id'] == tid for s in self.planners()))
                self.fl.handles[spec['run_id']].result = (result('no-record', json.dumps({
                    'status': 'done', 'sha': sha, 'verdict': 'LAND@' + sha, 'findings': [], 'note': 'ok'}))
                    if stage == 'review' else completed)
                self.d.tick()
                planner, = [s for s in self.planners() if s['task']['id'] == tid]
                self.assertEqual(planner['request_ids'], [request['id']])
                self.assertEqual(ops.list_runs(self.conn, tid)[0]['outcome'], 'unverified')
                self.assertTrue(ops.events(self.conn, task_id=tid, kind='replan.stage-finished'))
                self.assertEqual(self.status(tid), 'blocked')
