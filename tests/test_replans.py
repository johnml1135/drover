"""Durable replan requests, bounded records, graph replacement and dispatch."""
import copy
import json
from pathlib import Path
from unittest.mock import patch

from drover import brief, config, contract, launch, pipeline, repos
from drover.ledger import connect, db, ops
from drover.port import DirectPort
from tests.test_waiting import LedgerCase, logical, require
from tests.test_pipeline import Base as PipelineBase, result

ROUTE = {'backend': 'codex', 'model': 'gpt-6.1-sol', 'seat': 'codex', 'effort': 'high'}
REVISE = {'action': 'revise', 'goal': 'Use the actual API', 'acceptance': ['works'], 'scope_files': ['src/*']}
OWNER = {'action': 'needs-owner', 'question': 'Which API should be supported?'}
SPLIT = {'action': 'split', 'tasks': [
    {'key': 'a', 'title': 'First', 'goal': 'foundation', 'acceptance': ['works'], 'scope_files': ['a/*'], 'deps': []},
    {'key': 'b', 'title': 'Second', 'goal': 'consumer', 'acceptance': ['works'], 'scope_files': ['b/*'], 'deps': ['a']},
]}


class Records(PipelineBase):
    def test_parser_bounds_and_brief(self):
        parse = require(self, 'drover.contract', 'parse_replan_record')
        assemble = require(self, 'drover.brief', 'assemble_replan')
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

    def test_inputs_detect_dirty_and_branch_changes(self):
        method = getattr(pipeline.Pipeline, 'replan_inputs', None)
        self.assertTrue(callable(method), 'Pipeline.replan_inputs interface is absent')
        cfg = config.validate({'paths': {'state_dir': str(Path(self.tmp.name) / 'state')}})
        config.use(cfg)
        self.addCleanup(config.reset)
        tid = self.task()
        checkout = Path(self.pipe.worktree(self.get(tid)))
        initial = self.pipe.replan_inputs(self.get(tid))
        self.assertEqual(initial['head'], self.fix.git('rev-parse', 'HEAD', cwd=checkout))
        head = initial['head']
        digests = [initial['dirty_digest']]
        for filename, content, staged in [('base.txt', 'changed', False), ('base.txt', 'changed twice', False),
                                          ('base.txt', 'staged now', True), ('new.txt', 'one', False),
                                          ('new.txt', 'two', False)]:
            (checkout / filename).write_text(content)
            if staged:
                self.fix.git('add', filename, cwd=checkout)
            current = self.pipe.replan_inputs(self.get(tid))
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
            symlink = self.pipe.replan_inputs(self.get(tid))
            outside.write_text('changed outside checkout')
            self.assertEqual(self.pipe.replan_inputs(self.get(tid))['dirty_digest'], symlink['dirty_digest'])
            link.unlink()
            link.symlink_to(Path(self.tmp.name) / 'another')
            self.assertNotEqual(self.pipe.replan_inputs(self.get(tid))['dirty_digest'], symlink['dirty_digest'])
        owner_head = self.fix.git('rev-parse', 'HEAD')
        owner_index = self.fix.git('ls-files', '--stage')
        self.fix.commit('new-head.txt', cwd=checkout)
        self.assertNotEqual(self.pipe.replan_inputs(self.get(tid))['head'], head)
        read_repo = self.fix.repo
        read_task = self.task()
        before = self.pipe.replan_inputs(self.get(read_task))
        self.fix.commit('read-moved.txt')
        after = self.pipe.replan_inputs(self.get(read_task))
        self.assertNotEqual(before['read_checkouts'], after['read_checkouts'])
        owner_head = self.fix.git('rev-parse', 'HEAD')
        owner_index = self.fix.git('ls-files', '--stage')
        owner_status = self.fix.git('status', '--porcelain')
        missing = {**self.get(tid), 'worktree': str(Path(self.tmp.name) / 'missing')}
        fallback = self.pipe.replan_inputs(missing)
        self.assertNotEqual(fallback['checkout'], str(read_repo))
        self.assertEqual(self.fix.git('rev-parse', 'HEAD'), owner_head)
        self.assertEqual(self.fix.git('ls-files', '--stage'), owner_index)
        self.assertEqual(self.fix.git('status', '--porcelain'), owner_status)


class Replans(LedgerCase):
    def inputs(self):
        return {'repo': 'p', 'branch': 'topic', 'head': 'a' * 40, 'dirty_digest': 'b' * 64,
                'checkout': str(Path(self.tmp.name)), 'read_checkouts': []}

    def request(self, **fields):
        w = self.waiting()
        tid = self.task(goal='Original', acceptance=['old acceptance'], scope_files=['src/*'], **fields)
        return w, tid, w.request_replan(tid, 'owner-request')

    def start(self, w, request, now=1000):
        started = w.start_replan(request['id'], ROUTE, self.inputs(), now=now)
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
            self.assertIsNone(w.start_replan(r['id'], ROUTE, self.inputs(), now=now))
            fresh = self.inputs()
            if outcome == 'unverified':
                fresh['dirty_digest'] = 'c' * 64
            done = w.finish_replan(r['id'], sp['run_id'], outcome, snapshot=fresh, now=now)
            self.assertEqual(ops.get_task(self.conn, tid)['replans_used'], 0)
            row, = [row for row in w.pending(('task', tid)) if row['kind'] == 'deadline']
            delay = min(1800, 60 * 2 ** min(n - 1, 5))
            self.assertEqual(row['timeout_at'], now + delay)
            self.conn.close()
            self.conn = connect(self.path)
            w = self.waiting()
            self.assertEqual([row['timeout_at'] for row in w.pending(('task', tid)) if row['kind'] == 'deadline'], [now + delay])
            self.assertEqual(w.finish_replan(r['id'], sp['run_id'], outcome, snapshot=fresh, now=now + 1), done)
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
            w.finish_replan(r['id'], sp['run_id'], 'done', REVISE, snapshot=inputs, now=now)
            self.assertNotEqual(ops.get_task(self.conn, tid)['goal'], REVISE['goal'])
            w.expire(now + 1800)
            now += 1800
        before = logical(self.conn)
        # Force a real database failure rather than mocking a private Waiting helper.
        self.conn.execute("CREATE TRIGGER refuse_planner BEFORE INSERT ON runs WHEN new.role='planner' BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite_error()):
            w.start_replan(r['id'], ROUTE, self.inputs(), now=now)
        self.assertEqual(logical(self.conn), before)

    def test_replan_release_and_owner_replacement(self):
        for record in (REVISE, OWNER):
            w = self.waiting()
            tid = self.task(goal='old', acceptance=['old'], scope_files=['src/*'], branch='topic', worktree=self.tmp.name)
            pin = {'backend': 'codex', 'model': 'gpt-6-luna'}
            ops.update_task(self.conn, tid, model_pin=pin)
            follower = self.task()
            ops.add_dep(self.conn, follower, tid)
            r = w.request_replan(tid, 'owner-request')
            approved = ops.get_task(self.conn, tid)['approved_at']
            sp = self.start(w, r)
            out = w.finish_replan(r['id'], sp['run_id'], 'done', record, snapshot=self.inputs(), now=1001)
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
            self.assertEqual(w.finish_replan(r['id'], sp['run_id'], 'done', record, snapshot=self.inputs()), out)

    def test_limits_invalid_timeout_and_reset(self):
        w = self.waiting()
        for invalid in ('', '-1', '+1', '1.5', 'true', ' 2 '):
            _, tid, r = self.request()
            ops.set_setting(self.conn, 'replan_limit', invalid)
            before = logical(self.conn)
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                w.start_replan(r['id'], ROUTE, self.inputs())
            self.assertEqual(logical(self.conn), before)
            ops.set_status(self.conn, tid, 'cancelled', actor='owner')
        for limit in (0, 1, 2):
            ops.set_setting(self.conn, 'replan_limit', str(limit))
            _, tid, r = self.request()
            for n in range(limit):
                sp = self.start(w, r)
                out = w.finish_replan(r['id'], sp['run_id'], 'timeout' if n == 0 else 'no-record', snapshot=self.inputs())
                self.assertEqual(ops.get_task(self.conn, tid)['replans_used'], n + 1)
                self.assertEqual(w.finish_replan(r['id'], sp['run_id'], 'timeout', snapshot=self.inputs()), out)
            self.assertIsNone(w.start_replan(r['id'], ROUTE, self.inputs()))
            self.assertEqual(self.status(tid), 'needs-owner')
            q = next(q for q in ops.open_questions(self.conn) if q['target_id'] == tid)
            ops.answer_question(self.conn, q['id'], text='Revise the requirements')
            self.assertEqual(ops.get_task(self.conn, tid)['replans_used'], 0)
        ops.set_setting(self.conn, 'replan_limit', '2')
        _, tid, r = self.request()
        sp = self.start(w, r)
        w.finish_replan(r['id'], sp['run_id'], 'done', REVISE, snapshot=self.inputs())
        self.assertEqual(ops.get_task(self.conn, tid)['replans_used'], 1)
        # Successful replanning consumes budget but only an external owner action resets it.
        self.assertEqual(ops.get_task(self.conn, tid)['retry_after_run'], 0)

    def split_fixture(self):
        w = self.waiting()
        prerequisite = self.task('cancelled')
        source = self.task(branch='topic', worktree=self.tmp.name)
        follower = self.task()
        ops.add_dep(self.conn, source, prerequisite, kind='waits-for')
        ops.add_dep(self.conn, follower, source, kind='blocks')
        r = w.request_replan(source, 'owner-request')
        return w, source, follower, prerequisite, r, self.start(w, r)

    def test_split_graph_rollback_release(self):
        w, tid, follower, prerequisite, r, sp = self.split_fixture()
        out = w.finish_replan(r['id'], sp['run_id'], 'done', SPLIT, snapshot=self.inputs())
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
            w.finish_replan(r['id'], sp['run_id'], 'done', invalid, snapshot=self.inputs())
            self.assertEqual({t['id'] for t in ops.list_tasks(self.conn)}, before)
            self.assertNotEqual(self.status(tid), 'cancelled')

    def test_split_collision_replay(self):
        w, tid, follower, prerequisite, r, sp = self.split_fixture()
        out = w.finish_replan(r['id'], sp['run_id'], 'done', SPLIT, snapshot=self.inputs())
        before = logical(self.conn)
        self.assertEqual(w.finish_replan(r['id'], sp['run_id'], 'done', SPLIT, snapshot=self.inputs()), out)
        self.assertEqual(logical(self.conn), before)
        self.assertEqual({d['kind'] for d in ops.deps_of(self.conn, follower) if d['depends_on'] in out['children']}, {'blocks'})
        w, tid, follower, prerequisite, r, sp = self.split_fixture()
        before = logical(self.conn)
        self.conn.execute("CREATE TRIGGER refuse_child BEFORE INSERT ON tasks BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite_error()):
            w.finish_replan(r['id'], sp['run_id'], 'done', SPLIT, snapshot=self.inputs())
        self.assertEqual(logical(self.conn), before)


def sqlite_error():
    import sqlite3
    return sqlite3.DatabaseError


class PlannerDispatchCase(PipelineBase):
    def setUp(self):
        super().setUp()
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
