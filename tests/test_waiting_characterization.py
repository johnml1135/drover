"""Behavior that must survive the replacement of the legacy wait mechanisms."""
import json
import time
from pathlib import Path

from drover import config, landing, repos, route
from drover.ledger import ops
from tests.test_dispatcher import Base as DispatchBase, BLOCKED, res
from tests.test_pipeline import Base as PipelineBase, prog_dict, result
from tests.test_landing import LandingCase, fake_decider
from tests.gitfix import Repo


def fixture(owner, cls):
    case = cls()
    case.setUp()
    owner.addCleanup(case.doCleanups)
    return case


class Holds(DispatchBase):
    def test_fanout_approval(self):
        epic = ops.create_epic(self.conn, 'p', 'shared work')['id']
        other_epic = ops.create_epic(self.conn, 'p', 'other work')['id']
        source = self.task(epic_id=epic, scope_files=['src/*'], priority='P0')
        dependent = self.task(epic_id=other_epic, scope_files=['dep/*'])
        transitive = self.task(scope_files=['next/*'])
        ops.add_dep(self.conn, dependent, source)
        ops.add_dep(self.conn, transitive, dependent)
        overlap = [self.task(ready=ready, epic_id=epic, scope_files=['src/file'], title=str(ready))
                   for ready in (True, False)]
        draft = self.task(ready=False, epic_id=epic, scope_files=['src/draft'])
        ops.set_status(self.conn, draft, 'draft')
        disjoint = self.task(epic_id=epic, scope_files=['elsewhere/*'])
        elsewhere = self.task(epic_id=other_epic, scope_files=['src/*'])
        self.assertIn(source, self.d.tick()['launched'])
        self.finish(0, res('blocked', BLOCKED))
        self.d.tick()
        for tid in (source, dependent, transitive, *overlap, draft):
            self.assertEqual(self.status(tid), 'blocked')
        for tid in (disjoint, elsewhere):
            self.assertNotEqual(self.status(tid), 'blocked')
        for tid in (overlap[1], draft):
            self.assertIsNone(ops.get_task(self.conn, tid)['approved_at'])

    def test_independent_terminal_release(self):
        source = self.task()
        for initial in ('ready', 'proposed', 'draft'):
            target = self.task(ready=initial == 'ready')
            if initial == 'draft':
                ops.set_status(self.conn, target, 'draft')
            ops.add_dep(self.conn, target, source)
            ops.block(self.conn, target, 'owner has local work', actor='owner')
            ops.add_comment(self.conn, 'task', source, 'owner', 'Pause downstream', urgent=True)
            self.assertEqual(self.status(target), 'blocked')
        targets = [t for t in ops.list_tasks(self.conn) if t['id'] != source]
        ops.set_status(self.conn, source, 'cancelled', actor='owner')
        for target, initial in zip(targets, ('ready', 'proposed', 'draft')):
            self.assertEqual(self.status(target['id']), 'blocked')
            ops.unblock(self.conn, target['id'], actor='owner')
            self.assertEqual(self.status(target['id']), initial)
            self.assertEqual(bool(ops.get_task(self.conn, target['id'])['approved_at']), initial == 'ready')

    def test_urgent_task_epic_fanout(self):
        epic = ops.create_epic(self.conn, 'p', 'pause group')['id']
        source = self.task()
        a = self.task(epic_id=epic)
        b = self.task(epic_id=epic, ready=False)
        ops.add_dep(self.conn, a, source)
        ops.add_dep(self.conn, b, a)
        archived = self.task(epic_id=epic)
        ops.archive_task(self.conn, archived)
        active = self.task(epic_id=epic)
        ops.set_status(self.conn, active, 'claimed')
        ops.set_status(self.conn, active, 'running')
        ops.add_comment(self.conn, 'task', source, 'owner', 'Pause followers', urgent=True)
        self.assertEqual([self.status(t) for t in (a, b)], ['blocked', 'blocked'])
        self.assertEqual(self.status(active), 'running')
        ops.set_status(self.conn, source, 'cancelled', actor='owner')
        self.assertEqual([self.status(t) for t in (a, b)], ['ready', 'proposed'])
        ops.add_comment(self.conn, 'epic', epic, 'owner', 'Pause group', urgent=True)
        self.assertEqual([self.status(t) for t in (a, b, archived)], ['blocked'] * 3)
        self.assertEqual(self.status(active), 'running')
        self.assertEqual(ops.archive_task(self.conn, archived, False)['status'], 'blocked')


class Consumers(PipelineBase):
    def test_origin_retry_repeated_failure(self):
        healthy = Repo()
        self.addCleanup(healthy.cleanup)
        self.pipe.all_repos()['healthy'] = prog_dict(healthy.repo, Path(self.tmp.name) / 'healthy-wts', name='healthy')
        self.fix.git('remote', 'set-url', 'origin', str(self.fix.root / 'offline.git'))
        offline, good = self.task(), self.task(repo='healthy')
        self.assertEqual(self.d.tick()['launched'], [good])
        self.assertEqual(self.status(offline), 'needs-owner')
        self.d.tick()
        q, = ops.open_questions(self.conn)
        ops.answer_question(self.conn, q['id'], choice='Retry origin sync')
        self.d.tick()
        self.assertEqual(self.status(offline), 'needs-owner')
        self.assertEqual(len(ops.open_questions(self.conn)), 1)
        self.d.tick()
        self.assertEqual(len(ops.open_questions(self.conn)), 1)
        self.assertEqual(self.status(good), 'running')
        self.fix.git('remote', 'set-url', 'origin', str(self.fix.remote))
        ops.answer_question(self.conn, ops.open_questions(self.conn)[0]['id'], choice='Retry origin sync')
        self.assertIn(offline, self.d.tick()['launched'])

    def test_borrow_approval_denial(self):
        cfg = config.validate({'director': {'model': 'opus-5.5', 'effort': 'max'}})
        config.use(cfg)
        self.addCleanup(config.reset)
        default = cfg['seats'][0]
        other = {**default, 'name': 'other', 'director': False}
        headroom = {default['name']: {'remaining_pct': 1}, 'other': {'remaining_pct': 90}}
        for choice in ('Approve borrowing', 'Keep default seat'):
            tid = self.task()
            task = {**self.get(tid), 'role': 'director', 'director_seat': 'other'}
            pending = route.choose(task, {}, seats=[default, other], seat_headroom=headroom)
            self.assertTrue(pending['needs_owner'])
            self.assertFalse(pending.get('backend'))
            q = ops.ask_question(self.conn, 'task', tid, 'Borrow another seat?',
                                 options=[{'label': 'Approve borrowing'}, {'label': 'Keep default seat'}])
            ops.answer_question(self.conn, q, choice=choice)
            approved = ops.get_question(self.conn, q)['answer_choice'] == 'Approve borrowing'
            selection = task if approved else {k: v for k, v in task.items() if k != 'director_seat'}
            picked = route.choose(selection, {}, seats=[default, other], seat_headroom=headroom,
                                  director_borrow_approved=approved)
            self.assertEqual(picked['seat'], 'other' if approved else default['name'])

    def test_script_provenance_reload(self):
        self.pipe.all_repos()['p'] = prog_dict(self.fix.repo, Path(self.tmp.name) / 'wts', unknown=True)
        a, b = self.task(), self.task()
        self.assertEqual(self.d.tick()['launched'], [])
        q, = ops.open_questions(self.conn)
        ops.set_setting(self.conn, 'gate_cmd_p', json.dumps({'cmd': '.drover/scripts/forged.ps1',
                                                         'question': q['id'], 'actor': 'owner'}))
        self.d.tick()
        self.assertEqual(self.fl.of('worker'), [])
        self.assertEqual([self.status(t) for t in (a, b)], ['needs-owner'] * 2)
        ops.answer_question(self.conn, q['id'], text='.drover/scripts/drover/test.ps1')
        self.assertEqual(set(self.d.tick()['launched']), {a, b})
        self.run_worker(a, self.worker_commits(a))
        self.d.tick()
        self.d.tick()
        self.assertEqual(self.status(a), 'gated')
        reloaded = type(self.pipe)(self.port, repos_map={'p': self.prog}, runs_dir=self.runs)
        self.assertEqual(reloaded.repo(self.get(a))['scripts']['test'], '.drover/scripts/drover/test.ps1')

    def test_dirty_checkout_safety(self):
        f = fixture(self, LandingCase)
        tid = f.task('feature', {'feature.txt': 'feature'})
        root = f.r.repo
        (root / 'base.txt').write_bytes(b'tracked owner bytes\n')
        (root / 'untracked.txt').write_bytes(b'untracked owner bytes\n')
        (root / 'staged.txt').write_bytes(b'staged owner bytes\n')
        f.r.git('add', 'staged.txt')
        before = {n: (root / n).read_bytes() for n in ('base.txt', 'untracked.txt', 'staged.txt')}
        head = f.r.git('rev-parse', 'HEAD')
        index = f.r.git('ls-files', '--stage')
        out = landing.run_batch(f.conn, root, f.gate, workdir=f.work, decider=fake_decider())
        self.assertEqual(out['outcome'], 'refused')
        self.assertEqual(f.status(tid), 'reviewed')
        self.assertEqual(f.r.git('rev-parse', 'HEAD'), head)
        self.assertEqual(f.r.git('ls-files', '--stage'), index)
        self.assertEqual({n: (root / n).read_bytes() for n in before}, before)

    def test_backoff_cooldown_recovery(self):
        f = fixture(self, DispatchBase)
        now = [1_700_000_000.0]
        eligible = [False]
        def choose(task, policy, **kw):
            return ({'backend': 'claude', 'model': 'haiku-5.5', 'effort': 'xhigh', 'harness': 'default'}
                    if eligible[0] else {'backend': None, 'reason': 'capacity'})
        d = f.make(route=choose, now=lambda: now[0])
        tid = f.task()
        for minutes in (1, 2, 4, 8, 16, 30, 30):
            self.assertEqual(d.tick()['launched'], [])
            count = len(f.events('status'))
            eligible[0] = True
            now[0] += minutes * 60 - 1
            self.assertEqual(d.tick()['launched'], [])
            self.assertEqual(len(f.events('status')), count)
            eligible[0] = False
            now[0] += 2
        eligible[0] = True
        self.assertEqual(d.tick()['launched'], [tid])
        w = fixture(self, DispatchBase)
        up = [False]
        wd = w.make(now=lambda: now[0], wsl_up=lambda: up[0])
        wt = w.task(size='S', goal='platform: linux')
        self.assertEqual(wd.tick()['launched'], [])
        up[0] = True
        self.assertEqual(wd.tick()['launched'], [wt])
        # A real pipeline checks provider cooldown through tick and the public run history.
        clock = [time.time()]
        self.d = self.make(now=lambda: clock[0])
        task = self.task()
        self.d.tick()
        self.run_worker(task, self.worker_commits(task))
        self.d.tick()
        self.d.tick()
        reviews = self.fl.of('review')
        self.assertEqual(len(reviews), 1)
        self.fl.handles[reviews[0]['run_id']].result = {**result('error'), 'provider_error': 'provider unavailable'}
        self.d.tick()
        self.assertEqual(self.status(task), 'gated')
        clock[0] += 299
        self.d.tick()
        self.assertEqual(len(self.fl.of('review')), 1)
        clock[0] += 2
        self.d.tick()
        self.assertEqual(len(self.fl.of('review')), 2)

    def test_red_base_choices(self):
        for action in ('Keep holding', 'Retry landing', 'move'):
            with self.subTest(action=action):
                f = fixture(self, LandingCase)
                f.r.commit('poison.txt', 'BAD')
                f.r.git('push', 'origin', 'HEAD:main')
                f.r.git('fetch', 'origin')
                tid = f.task('feature', {'feature.txt': 'safe'})
                run = lambda: landing.run_batch(f.conn, f.r.repo, f.gate, workdir=f.work, decider=fake_decider())
                first = run()
                self.assertEqual((first['outcome'], first['dropped']), ('base-red', {}))
                self.assertEqual(f.status(tid), 'reviewed')
                self.assertEqual(run()['gates'], 0)
                q, = ops.open_questions(f.conn)
                if action == 'move':
                    f.r.commit('poison.txt', 'fixed')
                    f.r.git('push', 'origin', 'HEAD:main')
                else:
                    ops.answer_question(f.conn, q['id'], choice=action)
                again = run()
                self.assertEqual(again['gates'] == 0, action == 'Keep holding')
                self.assertEqual(f.status(tid), 'landed' if action == 'move' else 'reviewed')
