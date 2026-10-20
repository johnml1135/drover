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

from drover import ci, config, dispatcher, landing, origin, pipeline, repos, route, tuner
from drover.ledger import db, ops
from drover.ledger import landing as base_cache
from drover.ledger import ci as slots
from drover.port import DirectPort
from hub.dispatch import LandingService
from hub.settings import Settings
from hub.store import Store, UiView
from tests.test_waiting import LedgerCase, logical, require
from tests.test_pipeline import Base as PipelineBase, prog_dict
from tests.test_waiting_characterization import fixture
from tests.test_landing import LandingCase, fake_decider


class ConsumerCase(PipelineBase):
    def waiting(self):
        self.assertTrue(hasattr(self.port, 'waiting'), 'DirectPort.waiting interface is absent')
        return self.port.waiting

    def key(self, *parts):
        return require(self, 'drover.ledger.waiting', 'cause_key')(*parts)


class ScriptsOrigin(ConsumerCase):
    def test_abandoned_origin_attempt_reopens_before_parking(self):
        w = self.waiting()
        tid = self.task()
        cause = self.key('origin', 'p')
        w.wait(('task', tid), 'owner-answer', self.key(cause), cause=cause, data={'mode': 'needs-owner'})
        q = w.ask(cause, 'task', tid, 'Origin?', options=[{'label': 'Retry origin sync'}], allow_text=False)
        ops.answer_question(self.conn, q, choice='Retry origin sync')
        self.assertTrue(w.consume_retry(cause))
        with patch('drover.ledger.waiting.PROCESS_SESSION', 'replacement-process'):
            self.assertFalse(self.pipe.prepare_dispatch(self.get(tid)))
        self.assertEqual(w.state(cause), 'open')
        self.assertEqual(ops.get_question(self.conn, q)['status'], 'open')
        self.assertTrue(w.pending(('task', tid)))

    def test_script_immediate_reload_and_authority(self):
        w = self.waiting()
        self.pipe.all_repos()['p'] = prog_dict(self.fix.repo, Path(self.tmp.name) / 'wts', unknown=True)
        tids = [self.task() for _ in range(3)]
        self.d.tick()
        self.assertEqual(self.fl.of('worker'), [])
        q, = ops.open_questions(self.conn)
        cause = self.key('script', 'p')
        self.assertEqual({row['question_id'] for row in w.pending()}, {q['id']})
        for command in ('../owner.ps1', '/tmp/owner.ps1', 'script.py'):
            with self.assertRaises(ValueError):
                ops.answer_question(self.conn, q['id'], text=command)
        with self.assertRaises(PermissionError):
            ops.answer_question(self.conn, q['id'], text='.drover/scripts/test.ps1', actor='dispatcher')
        ops.set_setting(self.conn, 'gate_cmd_p', json.dumps({'cmd': '.drover/scripts/evil.ps1', 'question': q['id'], 'actor': 'owner'}))
        self.assertTrue(self.pipe.repo(self.get(tids[0]))['gate']['unknown'])
        ops.answer_question(self.conn, q['id'], text='.drover/scripts/drover/test.ps1')
        self.assertEqual([self.status(tid) for tid in tids], ['ready'] * 3)
        self.assertEqual(w.pending(), [])
        self.assertEqual(w.answer(cause)['actor'], 'owner')
        reloaded = pipeline.Pipeline(self.port, repos_map={'p': self.pipe.all_repos()['p']}, runs_dir=self.runs)
        self.assertEqual(reloaded.repo(self.get(tids[0]))['scripts']['test'], '.drover/scripts/drover/test.ps1')
        self.fix.commit('.drover/scripts/committed.ps1', 'exit 0\n')
        self.fix.commit('.drover/config.toml', 'schema_version=1\n[scripts]\ntest=".drover/scripts/committed.ps1"\n')
        committed = repos.load({'name': 'p', 'repo': str(self.fix.repo), 'worktrees': str(Path(self.tmp.name) / 'wts2')})
        new = pipeline.Pipeline(self.port, repos_map={'p': committed}, runs_dir=self.runs)
        self.assertEqual(new.repo(self.get(tids[0]))['scripts']['test'], '.drover/scripts/committed.ps1')

    def test_origin_release_reopen(self):
        w = self.waiting()
        self.fix.git('remote', 'set-url', 'origin', str(self.fix.root / 'offline.git'))
        tid = self.task()
        self.d.tick()
        cause = self.key('origin', 'p')
        q, = ops.open_questions(self.conn)
        # The queue discovers the same offline origin through its own public operation.
        out = landing.run_batch(self.conn, self.fix.repo, self.pipe.ci.run,
                                workdir=Path(self.tmp.name) / 'batch', repo_name='p')
        self.assertFalse(out.get('landed'))
        self.assertTrue(w.pending(('merge_queue', 'p')))
        self.assertEqual(len(ops.open_questions(self.conn)), 1)
        ops.answer_question(self.conn, q['id'], choice='Retry origin sync')
        self.assertEqual(self.status(tid), 'ready')
        self.assertEqual(w.pending(('merge_queue', 'p')), [])
        self.d.tick()
        self.assertEqual(w.state(cause), 'open')
        self.assertEqual([q2['id'] for q2 in ops.open_questions(self.conn)], [q['id']])
        self.d.tick()
        self.assertEqual([q2['id'] for q2 in ops.open_questions(self.conn)], [q['id']])
        self.fix.git('remote', 'set-url', 'origin', str(self.fix.remote))
        ops.answer_question(self.conn, q['id'], choice='Retry origin sync')
        self.assertEqual(self.d.tick()['launched'], [tid])
        self.assertEqual(w.state(cause), 'resolved')
        self.assertFalse(w.consume_retry(cause))


class BorrowTuner(LedgerCase):
    def test_borrow_answer_selection(self):
        w = self.waiting()
        cfg = config.validate({})
        default = cfg['seats'][0]
        other = {**default, 'name': 'other', 'director': False}
        readings = {default['name']: {'remaining_pct': 1}, 'other': {'remaining_pct': 90}}
        for choice in ('Approve borrowing', 'Keep default seat'):
            tid = self.task()
            cause = self.owner_wait(w, ('task', tid), 'borrow', tid, 'other')
            q = w.ask(cause, 'task', tid, 'Borrow?', options=[{'label': 'Approve borrowing'}, {'label': 'Keep default seat'}], allow_text=False)
            self.assertEqual(w.pending(('task', tid))[0]['handler'], 'choice')
            requested = {**ops.get_task(self.conn, tid), 'role': 'director', 'director_seat': 'other'}
            self.assertTrue(route.choose(requested, {}, seats=[default, other], seat_headroom=readings)['needs_owner'])
            ops.answer_question(self.conn, q, choice=choice)
            self.assertEqual(self.status(tid), 'ready')
            second = type(w)(self.conn, 'dispatcher')
            self.assertEqual(second.answer(cause)['choice'], choice)
            approved = second.answer(cause)['choice'] == 'Approve borrowing'
            selection = requested if approved else {k: v for k, v in requested.items() if k != 'director_seat'}
            selected = route.choose(selection, {}, seats=[default, other], seat_headroom=readings,
                                    director_borrow_approved=approved)
            self.assertEqual(selected['seat'], 'other' if approved else default['name'])

    def test_tuner_identity_no_adoption(self):
        w = self.waiting()
        tid = self.task('proposed')
        acc = {'task_size': {'version': 'v1', 'n': 100, 'model_acc': .95, 'rule_acc': .7}}
        before = ops.current_policy(self.conn)
        first = tuner.proposals(self.conn, acc, {})
        self.assertEqual(len(first), 1)
        cause = self.key('tuner', 'promote', 'task_size', 'v1')
        self.assertIsNotNone(w.state(cause))
        self.assertEqual(w.ask(cause, 'epic', ops.get_question(self.conn, first[0])['target_id'],
                               'A changed display title',
                               options=ops.get_question(self.conn, first[0])['options']), first[0])
        self.assertEqual(tuner.proposals(self.conn, acc, {}), [])
        q = ops.get_question(self.conn, first[0])
        ops.answer_question(self.conn, q['id'], choice=q['options'][0]['label'])
        self.assertEqual(tuner.proposals(self.conn, acc, {}), [])
        acc['task_size']['version'] = 'v2'
        self.assertEqual(len(tuner.proposals(self.conn, acc, {})), 1)
        stats = {'worker:S': {(backend, 'shared-name'): {'first_pass_n': 10, 'first_pass_ok': 0}
                              for backend in ('claude', 'codex')}}
        self.assertEqual(len(tuner.proposals(self.conn, {}, stats)), 2)
        self.assertEqual(ops.current_policy(self.conn), before)
        self.assertEqual(self.status(tid), 'proposed')
        self.assertIsNone(ops.get_task(self.conn, tid)['approved_at'])
        self.assertEqual(w.pending(), [])


class BaseQueue(LandingCase):
    def test_abandoned_base_retry_remains_held_until_new_answer(self):
        from drover.ledger.waiting import Waiting, BASE, cause_key
        w = Waiting(self.conn, 'owner')
        tid = self.task('feature', {'feature.txt': 'safe'})
        sha = self.r.git('rev-parse', 'main')
        cause = cause_key('base', 'p', sha)
        w.wait(('merge_queue', 'p'), BASE, cause_key('p', sha), cause=cause,
               data={'repo': 'p', 'sha': sha, 'mode': 'blocked'})
        q = w.ask(cause, 'task', tid, 'Base', options=[{'label': 'Retry landing'}, {'label': 'Keep holding'}], allow_text=False)
        ops.answer_question(self.conn, q, choice='Retry landing')
        self.assertTrue(w.consume_retry(cause))
        run = lambda: landing.run_batch(self.conn, self.r.repo, self.gate, workdir=self.work, repo_name='p', decider=fake_decider())
        with patch('drover.ledger.waiting.PROCESS_SESSION', 'replacement-process'):
            self.assertEqual(run()['outcome'], 'held')
            self.assertEqual(w.state(cause), 'open')
            self.assertEqual(run()['outcome'], 'held')
        self.assertEqual(self.status(tid), 'reviewed')
        self.assertEqual(ops.get_question(self.conn, q)['status'], 'open')

    def test_empty_queue_retains_base_retry_grant(self):
        from drover.ledger.waiting import Waiting, BASE, cause_key
        w = Waiting(self.conn, 'owner')
        sha = self.r.git('rev-parse', 'main')
        cause = cause_key('base', 'p', sha)
        w.wait(('merge_queue', 'p'), BASE, cause_key('p', sha), cause=cause,
               data={'repo': 'p', 'sha': sha, 'mode': 'blocked'})
        epic = ops.create_epic(self.conn, 'p', 'Landing')['id']
        q = w.ask(cause, 'epic', epic, 'Base', options=[{'label': 'Retry landing'}, {'label': 'Keep holding'}], allow_text=False)
        ops.answer_question(self.conn, q, choice='Retry landing')
        run = lambda: landing.run_batch(self.conn, self.r.repo, self.gate, workdir=self.work, repo_name='p', decider=fake_decider())
        self.assertEqual(run()['outcome'], 'empty')
        self.assertEqual(w.state(cause), 'authorized')
        tid = self.task('feature', {'feature.txt': 'safe'})
        self.assertEqual(run()['outcome'], 'landed')
        self.assertEqual(self.status(tid), 'landed')

    def test_red_base_dedupe_and_release(self):
        Waiting = require(self, 'drover.ledger.waiting', 'Waiting')
        key = require(self, 'drover.ledger.waiting', 'cause_key')
        w = Waiting(self.conn, 'owner')
        self.r.commit('poison.txt', 'BAD')
        self.r.git('push', 'origin', 'HEAD:main')
        self.r.git('fetch', 'origin')
        tid = self.task('feature', {'feature.txt': 'safe'})
        run = lambda: landing.run_batch(self.conn, self.r.repo, self.gate, workdir=self.work, repo_name='p', decider=fake_decider())
        first = run()
        self.assertEqual((first['outcome'], first['dropped']), ('base-red', {}))
        q, = ops.open_questions(self.conn)
        sha = self.r.git('rev-parse', 'main')
        base_cache.record_base(self.conn, 'other', sha, 'green')
        self.assertEqual(run()['gates'], 0)
        ops.answer_question(self.conn, q['id'], choice='Keep holding')
        self.assertEqual(run()['gates'], 0)
        ops.answer_question(self.conn, q['id'], choice='Retry landing')
        self.assertEqual(run()['outcome'], 'base-red')
        self.assertEqual([q2['id'] for q2 in ops.open_questions(self.conn)], [q['id']])
        self.assertEqual(base_cache.base_status(self.conn, 'other', sha), 'green')
        self.assertEqual(self.status(tid), 'reviewed')
        self.assertEqual(dispatcher.failures(ops.list_runs(self.conn, tid)), 0)
        base_cache.record_base(self.conn, 'p', sha, 'green')
        self.assertEqual(w.pending(('merge_queue', 'p')), [])
        self.assertFalse(w.consume_retry(key('base', 'p', sha)))
        self.r.commit('poison.txt', 'fixed')
        self.r.git('push', 'origin', 'HEAD:main')
        self.assertEqual(run()['outcome'], 'landed')
        self.assertEqual(base_cache.base_status(self.conn, 'other', sha), 'green')


class DirtyQueue(LandingCase):
    def test_dirty_approval_is_retained_until_origin_allows_probe(self):
        from drover.ledger.waiting import Waiting, cause_key, OWNER_ANSWER
        w = Waiting(self.conn, 'owner')
        questions = {}
        tid = self.task('feature', {'feature.txt': 'safe'})
        for family, label in [('dirty', 'Retry landing'), ('origin', 'Retry origin sync')]:
            cause = cause_key(family, 'p')
            w.wait(('merge_queue', 'p'), OWNER_ANSWER, cause_key(cause), cause=cause, data={'mode': 'blocked'})
            questions[family] = w.ask(cause, 'task', tid, family, options=[{'label': label}], allow_text=False)
        ops.answer_question(self.conn, questions['dirty'], choice='Retry landing')
        with patch.object(origin, 'sync', wraps=origin.sync) as sync:
            landing.sync_main(self.conn, self.r.repo, 'origin', 'main', 'p')
            sync.assert_not_called()
        self.assertEqual(w.state(cause_key('dirty', 'p')), 'authorized')
        ops.answer_question(self.conn, questions['origin'], choice='Retry origin sync')
        self.assertIsNone(landing.sync_main(self.conn, self.r.repo, 'origin', 'main', 'p'))
        self.assertEqual(w.state(cause_key('dirty', 'p')), 'resolved')
        self.assertEqual(w.state(cause_key('origin', 'p')), 'resolved')

    def service(self):
        configs = self.r.root / 'repos'
        configs.mkdir(exist_ok=True)
        (configs / 'p.toml').write_text(f'name="p"\nrepo={json.dumps(str(self.r.repo))}\nworktrees={json.dumps(str(self.r.root / "worktrees"))}\n')
        store = Store(self.r.root / 'ledger.db')
        self.addCleanup(store.close)
        return LandingService(store, Settings(runs_dir=self.r.root / 'receipts'), repos_dir=configs)

    def test_dirty_retry_owner_only(self):
        DirtyCheckout = require(self, 'drover.origin', 'DirtyCheckout')
        Waiting = require(self, 'drover.ledger.waiting', 'Waiting')
        w = Waiting(self.conn, 'owner')
        tid = self.task('feature', {'feature.txt': 'safe'})
        # Keep main in another worktree; a clean topic checkout must not hide owner dirt there.
        self.r.git('checkout', '-b', 'owner-topic')
        main = self.r.root / 'owner-main'
        self.r.git('worktree', 'add', main, 'main')
        (main / 'owner.txt').write_text('owner bytes')
        before = self.r.git('rev-parse', 'main')
        with self.assertRaises(DirtyCheckout):
            origin.fast_forward(self.r.repo, 'main', self.r.git('rev-parse', 'feature'), before)
        run = lambda: landing.run_batch(self.conn, self.r.repo, self.gate, workdir=self.work, repo_name='p', decider=fake_decider())
        self.assertEqual(run()['outcome'], 'refused')
        q, = ops.open_questions(self.conn)
        self.assertTrue(w.pending(('merge_queue', 'p')))
        self.assertEqual(run()['gates'], 0)
        with self.assertRaises(PermissionError):
            ops.answer_question(self.conn, q['id'], choice='Retry landing', actor='dispatcher')
        ops.answer_question(self.conn, q['id'], choice='Retry landing')
        self.assertEqual(run()['outcome'], 'refused')
        self.assertEqual([q2['id'] for q2 in ops.open_questions(self.conn)], [q['id']])
        self.assertEqual(self.r.git('rev-parse', 'main'), before)
        self.assertEqual((main / 'owner.txt').read_text(), 'owner bytes')
        self.assertEqual(self.status(tid), 'reviewed')
        (main / 'owner.txt').unlink()  # Simulate the owner's own cleanup.
        ops.answer_question(self.conn, q['id'], choice='Retry landing')
        self.assertEqual(run()['outcome'], 'landed')
        self.assertEqual(w.pending(('merge_queue', 'p')), [])
        self.assertTrue(issubclass(DirtyCheckout, origin.SyncError))

    def test_interrupted_ci_precedes_wait_and_rebuild(self):
        Waiting = require(self, 'drover.ledger.waiting', 'Waiting')
        key = require(self, 'drover.ledger.waiting', 'cause_key')
        w = Waiting(self.conn, 'owner')
        self.r.commit('.drover/config.toml', 'schema_version=1\n[scripts]\ntest=".drover/scripts/check.ps1"\n[gate]\ndocs_only=false\n')
        count, release = self.r.root / 'count', self.r.root / 'release'
        self.addCleanup(release.touch)
        self.r.commit('.drover/scripts/check.ps1',
                      f"Add-Content '{count}' started\nwhile (-not (Test-Path '{release}')) {{ Start-Sleep -Milliseconds 50 }}\nexit 0\n")
        self.r.git('push', 'origin', 'HEAD:main')
        tid = self.task('feature', {'feature.txt': 'safe'})
        ops.set_setting(self.conn, 'dispatch_enabled', 'true')
        service = self.service()
        def interrupt(*args):
            deadline = time.monotonic() + 20
            while not count.exists() and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue(count.exists())
            raise sqlite3.OperationalError('injected heartbeat interruption')
        with patch.object(slots, 'lease_heartbeat', side_effect=interrupt):
            with self.assertRaises(ci.CIInterrupted):
                service.tick()
        work = self.r.root / 'worktrees/drover-landing-p'
        checkpoint = work.parent / ('.' + work.name + '.merge-group.json')
        self.assertTrue(checkpoint.exists())
        saved = checkpoint.read_bytes()
        cause = key('dirty', 'p')
        w.wait(('merge_queue', 'p'), 'owner-answer', key(cause), cause=cause, data={'mode': 'blocked'})
        w.ask(cause, 'task', tid, 'Clean checkout?', options=[{'label': 'Retry landing'}], allow_text=False)
        self.task('later', {'later.txt': 'safe'}, priority='P0')
        self.r.commit('new-base.txt', 'new base')
        self.r.git('push', 'origin', 'HEAD:main')
        with patch.object(slots, 'lease_heartbeat', side_effect=sqlite3.OperationalError('again')):
            with self.assertRaises(ci.CIInterrupted):
                self.service().tick()
        self.assertEqual(checkpoint.read_bytes(), saved)
        self.assertEqual(count.read_text().splitlines(), ['started'])
        self.assertEqual(self.status(tid), 'reviewed')
        release.touch()
        self.service().tick()
        self.assertEqual(count.read_text().splitlines(), ['started'])
        self.assertEqual(dispatcher.failures(ops.list_runs(self.conn, tid)), 0)


class Failures(ConsumerCase):
    def test_infra_and_external_do_not_escalate(self):
        self.waiting()
        self.d = self.make(allow=lambda backend: backend == 'codex')
        tid = self.task(size='S')
        self.d.tick()
        worker, = self.fl.of('worker')
        self.assertEqual(worker['model'], 'gpt-6-luna')
        self.run_worker(tid, self.worker_commits(tid))
        self.fl.gate_script = [('timeout', 124, 'transport interrupted')] * 2
        self.d.tick()
        for _ in range(2):
            self.d.tick()
            task = self.get(tid)
            self.assertEqual(task['replans_used'], 0)
            self.assertEqual(dispatcher.failures(ops.list_runs(self.conn, tid), task['retry_after_run']), 0)
            self.assertEqual(len(self.fl.of('worker')), 1)
            self.assertEqual(worker['model'], 'gpt-6-luna')
        runs = [
            {'id': 1, 'role': 'worker', 'backend': 'codex', 'model': 'gpt-6-luna', 'outcome': 'gate-fail'},
            {'id': 2, 'role': 'gate', 'backend': 'local', 'model': 'gate', 'outcome': 'failed'},
            {'id': 3, 'role': 'review', 'backend': 'codex', 'model': 'gpt-6.1-sol', 'outcome': 'hold'},
            {'id': 4, 'role': 'planner', 'backend': 'codex', 'model': 'gpt-6.1-sol', 'outcome': 'error'},
        ]
        self.assertEqual([r['id'] for r in dispatcher.fail_runs(runs)], [1])
        self.assertEqual(dispatcher.next_tier(runs, {'backend': 'codex', 'model': 'gpt-6-luna'})['model'], 'gpt-6-luna')
        runs.append({**runs[0], 'id': 5, 'outcome': 'error'})
        self.assertEqual(dispatcher.failures(runs), 2)
        self.assertEqual(dispatcher.next_tier(runs, {'backend': 'codex', 'model': 'gpt-6-luna'})['model'], 'gpt-6.1-sol')

    def test_pin_api_authority(self):
        source = Path(__file__).resolve().parents[1] / 'hub/api.py'
        definition = next(node for node in ast.parse(source.read_text()).body
                          if isinstance(node, ast.ClassDef) and node.name == 'TaskPatch')
        fields = {node.target.id for node in definition.body
                  if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)}
        self.assertIn('model_pin', fields, 'TaskPatch.model_pin interface is absent')
        from hub.api import TaskPatch, patch_task
        self.assertIn('model_pin', TaskPatch.model_fields)
        from fastapi import HTTPException
        from starlette.requests import Request
        store = Store(Path(self.tmp.name) / 'ledger.db')
        self.addCleanup(store.close)
        request = Request({'type': 'http', 'app': SimpleNamespace(state=SimpleNamespace(store=store)), 'headers': []})
        tid = ops.create_task(store.conn, 'Pinned work', 'p')['id']
        ops.approve(store.conn, tid)
        pin = {'backend': 'codex', 'model': 'gpt-6-luna'}
        body = TaskPatch(model_pin=pin)
        self.assertEqual(body.model_dump()['model_pin'], pin)
        changed = patch_task(tid, body, request, who='ui:owner')
        self.assertEqual(changed['model_pin'], pin)
        before = store.get_task(tid)
        with self.assertRaises(HTTPException) as rejected:
            patch_task(tid, TaskPatch(model_pin=None), request, who='token')
        self.assertEqual(rejected.exception.status_code, 403)
        self.assertEqual(store.get_task(tid), before)
        self.assertIsNone(patch_task(tid, TaskPatch(model_pin={}), request, who='ui:owner')['model_pin'])


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
            page = templates.env.get_template('task.html').render(**context, partial=True, view='page')
            for html in (card, page):
                self.assertIn('Waiting on', html)
                self.assertNotIn('<script>', html)
                self.assertNotIn('Blocker:', html)

