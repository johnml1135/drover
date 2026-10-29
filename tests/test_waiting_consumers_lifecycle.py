"""Application behavior at the Waiting, pipeline, router and owner UI seams."""
import ast
import io
import json
import sqlite3
import time
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from drover import ci, config, contract, dispatcher, landing, origin, pipeline, repos, route, tuner
from drover.ledger import db, ops
from drover.ledger import landing as base_cache
from drover.ledger import ci as slots
from drover.port import DirectPort
from hub.dispatch import LandingService
from hub.settings import Settings
from hub.store import Store, UiView
from tests.fakeci import resources_go
from tests.tiers import integration
from tests.test_waiting import LedgerCase, logical, require
from tests.test_pipeline import prog_dict
from tests.rcw_fixture import PipelineBase
from tests.test_waiting_characterization import fixture
from tests.test_landing import fake_decider
from tests.waiting_fixture import LandingCase


class Lifecycle(LedgerCase):
    def store(self):
        store = Store(self.path)
        self.addCleanup(store.close)
        return store, UiView(store)

    def shared(self, w, tids):
        cause = self.key('script', 'p')
        for tid in tids:
            self.owner_wait(w, ('task', tid), 'script', 'p')
        return cause, w.ask(cause, 'task', tids[0], 'Script?')

    def test_shared_question_delete_archive_rollback(self):
        w = self.waiting()
        a, b = self.task(), self.task()
        cause, q = self.shared(w, (a, b))
        store, view = self.store()
        ops.archive_task(self.conn, a)
        self.assertIn(str(q), [row['id'] for row in view.inbox_questions()])
        ops.delete_task(self.conn, a, confirm=True)
        self.assertEqual(ops.get_question(self.conn, q)['target_id'], b)
        ops.answer_question(self.conn, q, text='.drover/scripts/test.ps1')
        self.assertEqual(self.status(b), 'ready')
        accepted = w.answer(cause)
        self.assertEqual(accepted['actor'], 'owner')
        ops.delete_task(self.conn, b, confirm=True)
        self.assertIsNone(w.answer(cause))
        source, follower = self.task(), self.task()
        self.terminal_wait(w, follower, source)
        ops.block(self.conn, follower, 'manual remains', actor='owner')
        ops.archive_task(self.conn, follower)
        before = logical(self.conn)
        self.conn.execute("CREATE TRIGGER refuse_delete BEFORE DELETE ON tasks BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            ops.delete_task(self.conn, source, confirm=True)
        self.assertEqual(logical(self.conn), before)
        self.conn.execute('DROP TRIGGER refuse_delete')
        ops.delete_task(self.conn, source, confirm=True)
        ops.archive_task(self.conn, follower, False)
        self.assertEqual([row['kind'] for row in w.pending(('task', follower))], ['manual'])
        self.assertEqual(self.status(follower), 'blocked')

    def test_delete_rehomes_canonical_unicode_repo_question(self):
        w = self.waiting()
        first, survivor = self.task(repo='Straße'), self.task(repo=' STRASSE ')
        cause = self.key('script', 'strasse')
        for tid in (first, survivor):
            w.wait(('task', tid), 'owner-answer', self.key(cause), cause=cause, data={'mode': 'needs-owner'})
        q = w.ask(cause, 'task', first, 'Script?')
        ops.delete_task(self.conn, first, confirm=True)
        self.assertEqual(ops.get_question(self.conn, q)['target_id'], survivor)
        self.assertEqual(w.pending(('task', survivor))[0]['question_id'], q)
        ops.answer_question(self.conn, q, text='.drover/scripts/test.ps1')
        self.assertEqual(self.status(survivor), 'ready')

    def test_waiting_labels_and_board(self):
        w = self.waiting()
        source = self.task(title='<script>alert(1)</script>|owner')
        follower = self.task()
        self.terminal_wait(w, follower, source)
        owner = self.task()
        cause, q = self.shared(w, (owner,))
        store, view = self.store()
        detail = view.task_detail(follower)
        self.assertIn('Waiting on', json.dumps(detail))
        self.assertIn('Waiting on you', json.dumps(view.task_detail(owner)))
        self.assertIn(str(q), [row['id'] for row in view.inbox_questions()])
        from drover.ledger import board, cli
        rendered = board.render(self.conn)
        self.assertIn('Waiting on', rendered)
        self.assertIn('Waiting on you', rendered)
        self.assertNotIn('<script>', rendered)
        self.assertNotIn('"holds"', rendered)
        output = io.StringIO()
        with redirect_stdout(output):
            cli.cmd_show(self.conn, SimpleNamespace(id=follower, json=False))
        self.assertIn('Waiting on', output.getvalue())
        self.assertNotIn('blocker:', output.getvalue())
        from hub.web import templates
        for tid in (follower, owner):
            context = view.task_detail(tid)
            card = templates.env.get_template('_task_card.html').render(t=context['task'])
            page = templates.env.get_template('task.html').render(**context, partial=True, view='page',
                                                                            dispatch={'enabled': False, 'label': 'Off'})
            for html in (card, page):
                self.assertIn('Waiting on', html)
                self.assertNotIn('<script>', html)
                self.assertNotIn('Blocker:', html)


class FinalReviewRegressions(LedgerCase):
    route = {'backend': 'codex', 'model': 'gpt-6.1-sol'}
    revise = {'action': 'revise', 'goal': 'Revised', 'acceptance': ['works'], 'scope_files': ['src/*']}

    def test_overlap_release_survives_scope_edit(self):
        self._overlap_release_after_edit(scope_files=['elsewhere/*'])

    def test_overlap_release_survives_epic_edit(self):
        epic = ops.create_epic(self.conn, 'p', 'New epic')['id']
        self._overlap_release_after_edit(epic_id=epic)

    def _overlap_release_after_edit(self, **edit):
        w = self.waiting()
        epic = ops.create_epic(self.conn, 'p', 'Overlap')['id']
        sources = sorted(self.task(epic_id=epic, scope_files=['src/*']) for _ in range(2))
        for tid in reversed(sources):
            w.request_replan(tid, 'owner-request')
        requests = {r['task_id']: r for r in w.replans()}
        ops.update_task(self.conn, sources[0], **edit)
        # Reload the public interface: the waits, rather than current metadata,
        # must retain a runnable leader and protect the follower until release.
        w = self.waiting()
        self.assertIsNone(w.start_replans([{'request_id': requests[sources[1]]['id'], 'inputs': {}}], self.route))
        for tid in sources:
            started = w.start_replans([{'request_id': requests[tid]['id'], 'inputs': {}}], self.route)
            self.assertIsNotNone(started, 'metadata edits must not strand both planners')
            self.assertTrue(w.pending(('task', sources[1])))
            w.finish_replans(started['run_id'], contract.DONE, {w.replan_run(started['run_id'])['members'][0]['task_id']: self.revise}, None, fresh={requests[tid]['id']: {}})[requests[tid]['id']]
        self.assertEqual(w.replans(), [])
        self.assertEqual(w.pending(), [])

    def test_overlapping_sources_replan_in_turn(self):
        w = self.waiting()
        epic = ops.create_epic(self.conn, 'p', 'Overlap')['id']
        sources = sorted(self.task(epic_id=epic, scope_files=['src/*']) for _ in range(2))
        follower = self.task('proposed', epic_id=epic, scope_files=['src/a'])
        for tid in reversed(sources):
            w.request_replan(tid, 'owner-request')
        requests = {r['task_id']: r for r in w.replans()}
        started = w.start_replans([{'request_id': requests[t]['id'], 'inputs': {}} for t in sources], self.route)
        self.assertIsNotNone(started, 'overlapping sources share one runnable batch')
        self.assertTrue(w.pending(('task', follower)))
        w.finish_replans(started['run_id'], contract.DONE, {t: self.revise for t in sources},
                         fresh={r['id']: {} for r in requests.values()})
        self.assertEqual(w.replans(), [])
        self.assertEqual(w.pending(('task', follower)), [])
        self.assertEqual(self.status(follower), 'proposed')

    def test_epic_overlapping_sources_replan_in_turn(self):
        w = self.waiting()
        epic = ops.create_epic(self.conn, 'p', 'Overlap')['id']
        sources = sorted(self.task(epic_id=epic, scope_files=['src/*']) for _ in range(2))
        ops.event(self.conn, 'replan-request', 'owner', {}, epic_id=epic)
        requests = {r['task_id']: r for r in w.replans()}
        started = w.start_replans([{'request_id': requests[t]['id'], 'inputs': {}} for t in sources], self.route)
        self.assertIsNotNone(started, 'epic sources share one batch')
        w.finish_replans(started['run_id'], contract.DONE, {t: self.revise for t in sources},
                         fresh={r['id']: {} for r in requests.values()})
        self.assertEqual(w.replans(), [])
        self.assertTrue(all(self.status(tid) == 'ready' for tid in sources))

    def test_overlap_order_respects_dependency_and_follower_protection(self):
        w = self.waiting()
        epic = ops.create_epic(self.conn, 'p', 'Ordered overlap')['id']
        first, second = sorted(self.task(epic_id=epic, scope_files=['src/*']) for _ in range(2))
        ops.add_dep(self.conn, first, second)
        ops.event(self.conn, 'replan-request', 'owner', {}, epic_id=epic)
        requests = {r['task_id']: r for r in w.replans()}
        started = w.start_replans([{'request_id': requests[t]['id'], 'inputs': {}} for t in (first, second)], self.route)
        self.assertIsNotNone(started)
        self.assertEqual([m['task_id'] for m in started['members']], [second, first])
        self.assertTrue(w.pending(('task', second)), 'enrolled prerequisite remains protected')
        self.assertIsNone(ops.claim_next(self.conn, {'actor': 'dispatcher'}))
        w.finish_replans(started['run_id'], contract.DONE, {t: self.revise for t in (first, second)},
                         fresh={r['id']: {} for r in requests.values()})
        self.assertEqual(w.replans(), [])
        self.assertEqual(w.pending(), [])

    def test_historical_answers_cannot_grant_retry(self):
        w = self.waiting()
        cause, qid, choice = self.retry(w, ('task', self.task()))
        ops.answer_question(self.conn, qid, choice=choice)
        first = ops.events(self.conn, kind='question-answered')[-1]
        self.assertTrue(w.consume_retry(cause))
        ops.event(self.conn, 'origin-sync-failed', 'dispatcher', {'repo': 'p', 'reason': 'offline'})
        ops.answer_question(self.conn, qid, choice=choice)
        second = ops.events(self.conn, kind='question-answered')[-1]
        self.assertTrue(w.consume_retry(cause))
        ops.event(self.conn, 'origin-synced', 'dispatcher', {'repo': 'p'})
        before = logical(self.conn)
        for event in (first, second):
            w.resolve(event)
            self.assertFalse(w.consume_retry(cause), 'historical answers must not manufacture retry grants')
            self.assertEqual(logical(self.conn), before)
        ops.event(self.conn, 'origin-sync-failed', 'dispatcher', {'repo': 'p', 'reason': 'offline'})
        before = logical(self.conn)
        w.resolve(second)
        self.assertEqual(logical(self.conn), before)

    def test_completed_request_replay_does_not_reblock(self):
        w = self.waiting()
        tid = self.task()
        request = w.request_replan(tid, 'owner-request')
        event = ops.events(self.conn, task_id=tid, kind='replan-request')[-1]
        started = w.start_replans([{'request_id': request['id'], 'inputs': {}}], self.route)
        w.finish_replans(started['run_id'], contract.DONE, {w.replan_run(started['run_id'])['members'][0]['task_id']: self.revise}, None, fresh={request['id']: {}})[request['id']]
        before = logical(self.conn)
        w.resolve(event)
        self.assertEqual(w.replans(), [], 'completed request replay must not queue another replan')
        self.assertEqual(w.pending(('task', tid)), [])
        self.assertEqual(logical(self.conn), before)
        self.assertNotEqual(w.request_replan(tid, 'new-request')['id'], request['id'])

    def test_coalesced_request_replay_does_not_reblock(self):
        w = self.waiting()
        tid = self.task()
        request = w.request_replan(tid, 'owner-request')
        self.assertEqual(w.request_replan(tid, 'second-request')['id'], request['id'])
        event = ops.events(self.conn, task_id=tid, kind='replan-request')[-1]
        started = w.start_replans([{'request_id': request['id'], 'inputs': {}}], self.route)
        w.finish_replans(started['run_id'], contract.DONE, {w.replan_run(started['run_id'])['members'][0]['task_id']: self.revise}, None, fresh={request['id']: {}})[request['id']]
        before = logical(self.conn)
        self.waiting().resolve(event)
        self.assertEqual(w.replans(), [], 'coalesced event replay must not queue another replan')
        self.assertEqual(w.pending(('task', tid)), [])
        self.assertEqual(logical(self.conn), before)
        self.assertNotEqual(w.request_replan(tid, 'new-request')['id'], request['id'])

    def test_cancelled_replan_supersedes_owner_question(self):
        w = self.waiting()
        source, follower = self.task(), self.task()
        ops.add_dep(self.conn, follower, source)
        request = w.request_replan(source, 'owner-request')
        started = w.start_replans([{'request_id': request['id'], 'inputs': {}}], self.route)
        w.finish_replans(started['run_id'], contract.DONE, {w.replan_run(started['run_id'])['members'][0]['task_id']: {'action': 'needs-owner', 'question': 'Which API?'}}, None, fresh={request['id']: {}})[request['id']]
        question, = w.questions()
        before = logical(self.conn)
        with self.assertRaises(RuntimeError):
            with db.tx(self.conn):
                ops.set_status(self.conn, source, 'cancelled', actor='owner')
                raise RuntimeError('rollback cancellation')
        self.assertEqual(logical(self.conn), before)
        ops.set_status(self.conn, source, 'cancelled', actor='owner')
        self.assertEqual(ops.open_questions(self.conn), [])
        self.assertEqual(w.state(question['cause']), 'closed')
        self.assertEqual(w.pending(('task', follower)), [])
        with self.assertRaises(ValueError):
            ops.answer_question(self.conn, question['id'], text='late answer')

    def test_terminal_urgent_supersedes_question(self):
        w = self.waiting()
        source, follower = self.task(), self.task()
        ops.add_dep(self.conn, follower, source)
        ops.add_comment(self.conn, 'task', source, 'owner', 'Pause', urgent=True)
        question, = w.questions()
        ops.set_status(self.conn, source, 'cancelled', actor='owner')
        self.assertEqual(ops.open_questions(self.conn), [])
        self.assertEqual(w.state(question['cause']), 'closed')
        self.assertEqual(w.pending(('task', follower)), [])
        with self.assertRaises(ValueError):
            ops.answer_question(self.conn, question['id'], choice='Resume work')
