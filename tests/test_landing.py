from drover import placement
import json
import sqlite3
import time
import sys
import subprocess
import unittest
from pathlib import Path, PureWindowsPath
from unittest.mock import patch

from drover import ci, landing, origin, repos
from drover.ledger import ci as slots
from drover.ledger import landing as landing_storage
from drover.ledger import db as ledger_db
from drover.ledger import ops
from tests.fakeci import InProcessLauncher
from tests.gitfix import Repo
from tests.tiers import integration

SCRIPT = """\
import pathlib, sys
files = {p.name: p.read_text() for p in pathlib.Path('.').glob('*.txt')}
bad = [n for n, t in files.items() if 'BAD' in t]
for n in bad:
    print(f'bad content in {n}')
if 'x.txt' in files and 'y.txt' in files:
    print('x and y clash')
    bad.append('shared')
sys.exit(1 if bad else 0)
"""


def fake_decider(answer=None, acted=False):
    calls = []

    def decide(name, state, rule_answer=None, dynamic_criteria=None, recorder=None):
        calls.append((name, dynamic_criteria))
        r = {'name': name, 'answer': answer if acted else None, 'shadow_answer': answer, 'acted': acted,
             'confidence': 0.9 if acted else 0.4, 'criteria_version': 1, 'model': 'fake', 'state_hash': 'h',
             'rule_answer': rule_answer, 'fallback': None if acted else 'low_confidence'}
        if recorder:
            recorder(r)
        return r
    decide.calls = calls
    return decide


class LandingCase(unittest.TestCase):
    def setUp(self):
        self.r = Repo()
        self.addCleanup(self.r.cleanup)
        from drover import config, disk
        import copy
        import os
        environment = patch.dict(os.environ, {'DROVER_HOME': str(self.r.root / 'state')})
        environment.start()
        self.addCleanup(environment.stop)
        original_config = config.get()
        self.addCleanup(config.use, original_config)
        cfg = copy.deepcopy(original_config)
        cfg['paths']['state_dir'] = str(self.r.root / 'state')
        cfg['repos_dir'] = str(self.r.root / 'state' / 'repos')
        config.use(cfg)
        for context in (patch.object(disk, 'read', return_value=[dict(name='fixture', path=str(self.r.root), free_gb=1000., level='ok')]),):
            context.start()
            self.addCleanup(context.stop)
        self.conn = ledger_db.connect(self.r.root / 'ledger.db')
        self.addCleanup(self.conn.close)
        self.script = self.r.root / 'gate.py'
        self.script.write_text(SCRIPT)
        exe = sys.executable.replace("'", "''")
        script = str(self.script).replace("'", "''")
        self.r.commit('.drover/scripts/check.ps1', f"& '{exe}' '{script}'\nexit $LASTEXITCODE\n")
        self.r.git('push', '-q', 'origin', 'HEAD:main')
        self.r.git('fetch', '-q', 'origin')
        self.prog = repos.normalize({'name': 'p', 'repo': str(self.r.repo),
                                     'scripts': {'test': '.drover/scripts/check.ps1'}, 'gate': {'docs_only': False}})
        self.ci = self.make_ci(self.r.root / 'receipts')
        self.gate = lambda checkout, commit, tasks: self.ci.run(self.prog, checkout, commit, base='origin/main')
        self.work = self.r.root / 'batch-wt'

    def make_ci(self, runs):
        """The gate runs in-process (tests/fakeci.py); RealRunnerLandingTests overrides this to use the real runner."""
        return ci.CI(runs, InProcessLauncher(runs))

    def task(self, name, files, priority='P2', review='LAND'):
        self.r.branch(name, files)
        t = ops.create_task(self.conn, name, 'p', repo_path=str(self.r.repo), priority=priority)
        ops.update_task(self.conn, t['id'], branch=name)
        ops.approve(self.conn, t['id'])
        for s in ('claimed', 'running', 'gated', 'reviewed'):
            ops.set_status(self.conn, t['id'], s, actor='system')
        if review:
            run = ops.start_run(self.conn, t['id'], 'reviewer', 'claude')
            verdict = f'LAND@{self.r.git("rev-parse", name)}' if review == 'LAND' else review
            ops.finish_run(self.conn, run, 'ok', review_verdict=verdict)
        return t['id']

    def status(self, tid):
        return ops.get_task(self.conn, tid)['status']

    def land(self, **kw):
        kw.setdefault('push', True)
        kw.setdefault('decider', fake_decider())
        return landing.run_batch(self.conn, self.r.repo, self.gate, workdir=self.work, **kw)


class WorktreeTests(LandingCase):
    def test_legacy_git_falls_back_only_for_native_landing_worktrees(self):
        real = landing.git
        base = self.r.git('rev-parse', 'main')
        self.r.git('config', 'core.autocrlf', 'true')
        calls = []

        def git(repo, *args, **kw):
            if 'add' in args:
                calls.append(args)
                if '--relative-paths' in args:
                    return subprocess.CompletedProcess(args, 129, '', "error: unknown option `relative-paths'")
            return real(repo, *args, **kw)

        with patch.object(landing, 'git', git), patch.object(placement, 'is_linux_native', return_value=False), patch.object(placement, 'routes_through_wsl', return_value=False):
            with self.assertRaisesRegex(ci.CIError, 'both Windows and WSL: update git'):
                landing.build_tip(self.r.repo, self.work, base, [])
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.work.exists())
        calls.clear()
        with patch.object(landing, 'git', git), patch.object(placement, 'is_linux_native', return_value=True), patch.object(placement, 'routes_through_wsl', return_value=False):
            landing.build_tip(self.r.repo, self.work, base, [])
        self.assertEqual(len(calls), 2)
        for args in calls:
            self.assertEqual(args[:4], ('-c', 'core.autocrlf=false', 'worktree', 'add'))
        self.assertEqual((self.work / 'base.txt').read_bytes(), b'base\n')

    def test_landing_does_not_retry_an_unrelated_error_mentioning_relative_paths(self):
        def git(repo, *args, **kw):
            return subprocess.CompletedProcess(args, 128, '', 'fatal: relative-paths metadata is corrupt')

        with patch.object(landing, 'git', side_effect=git) as run:
            with self.assertRaisesRegex(ci.CIError, 'metadata is corrupt'):
                landing.build_tip(self.r.repo, self.work, 'a' * 40, [])
        self.assertEqual(run.call_count, 1)

    def test_reused_absolute_gitdir_is_repaired_before_git_opens_it(self):
        base = self.r.git('rev-parse', 'main')
        self.r.git('worktree', 'add', '--no-relative-paths', '-q', '--detach', str(self.work), base)
        pointer = (self.work / '.git').read_text().strip().removeprefix('gitdir: ')
        self.assertTrue(Path(pointer).is_absolute() or PureWindowsPath(pointer).is_absolute())
        real = landing.git

        def git(repo, *args, **kw):
            if Path(repo) == self.work:
                pointer = (self.work / '.git').read_text().strip().removeprefix('gitdir: ')
                self.assertFalse(Path(pointer).is_absolute() or PureWindowsPath(pointer).is_absolute())
            return real(repo, *args, **kw)

        with patch.object(landing, 'git', git):
            tip, applied, conflicts = landing.build_tip(self.r.repo, self.work, base, [])
        self.assertEqual((tip, applied, conflicts), (base, [], []))
        self.assertEqual(self.r.git('rev-parse', 'HEAD', cwd=self.work), base)

    def test_landing_worktree_uses_a_relative_gitdir_and_disables_autocrlf(self):
        self.r.git('config', 'core.autocrlf', 'true')
        base = self.r.git('rev-parse', 'main')
        tip, applied, conflicts = landing.build_tip(self.r.repo, self.work, base, [])
        pointer = (self.work / '.git').read_text().strip().removeprefix('gitdir: ')
        self.assertFalse(Path(pointer).is_absolute())
        self.assertFalse(PureWindowsPath(pointer).is_absolute())
        self.assertEqual((tip, applied, conflicts), (base, [], []))
        self.assertEqual((self.work / 'base.txt').read_bytes(), b'base\n')


class FormationTests(LandingCase):
    def test_disjoint_batch_up_to_policy_size(self):
        ops.add_policy_version(self.conn, 'v1', {'landing_batch_size': 2})
        a = self.task('a', {'a.txt': 'a'}, 'P2')
        b = self.task('b', {'b.txt': 'b'}, 'P1')
        self.task('c', {'c.txt': 'c'}, 'P3')
        batch = landing.form_batch(self.conn, self.r.repo, 'origin/main')
        self.assertEqual([t['id'] for t in batch], [b, a])   # priority first, capped at 2

    def test_overlapping_and_unreviewed_tasks_excluded(self):
        a = self.task('a', {'a.txt': 'a'})
        self.task('a2', {'a.txt': 'other'})
        self.task('nr', {'n.txt': 'n'}, review=None)
        self.task('hold', {'h.txt': 'h'}, review='HOLD')
        self.task('bare', {'bare.txt': 'b'}, review='LAND-bare-nosha'.replace('LAND-bare-nosha', 'LAND'))
        bare = ops.list_tasks(self.conn)[-1]['id']
        run = ops.start_run(self.conn, bare, 'reviewer', 'claude')
        ops.finish_run(self.conn, run, 'ok', review_verdict='LAND')   # newest verdict names no SHA
        self.assertEqual([t['id'] for t in landing.form_batch(self.conn, self.r.repo, 'origin/main', 5)], [a])

    def test_size_clamped_and_batching_turns_off_when_gates_fast(self):
        ops.add_policy_version(self.conn, 'v1', {'landing_batch_size': 99})
        self.assertEqual(landing.policy_batch_size(self.conn), 5)
        self.assertEqual(landing.effective_batch_size(self.conn), 5)
        for _ in range(5):
            self.conn.execute("INSERT INTO landing_batches(at,tasks,outcome,gates,gate_s,red,bisect_gates,decisions,dropped)"
                              " VALUES('t','[]','landed',1,'[30]',0,0,0,'{}')")
        self.assertEqual(landing.effective_batch_size(self.conn), 1)


class RunTests(LandingCase):
    def test_landing_advances_checked_out_main_and_keeps_checkout_clean(self):
        tid = self.task('feature', {'base.txt': 'landed content'})
        res = self.land(push=False)
        self.assertEqual(res['landed'], [tid])
        self.assertEqual(self.r.git('rev-parse', 'HEAD'), res['tip'])
        self.assertEqual(self.r.git('status', '--porcelain'), '')
        self.assertEqual((self.r.repo / 'base.txt').read_text(), 'landed content')

    def test_landing_advances_main_in_another_worktree(self):
        tid = self.task('feature', {'base.txt': 'landed content'})
        self.r.git('checkout', '-q', '--detach')
        checkout = self.r.root / 'main checkout with spaces'
        self.r.git('worktree', 'add', '-q', str(checkout), 'main')
        res = self.land(push=False)
        self.assertEqual(res['landed'], [tid])
        self.assertEqual(self.r.git('rev-parse', 'HEAD', cwd=checkout), res['tip'])
        self.assertEqual(self.r.git('status', '--porcelain', cwd=checkout), '')
        self.assertEqual((checkout / 'base.txt').read_text(), 'landed content')
        self.assertEqual((self.r.repo / 'base.txt').read_text(), 'base\n')

    def test_unchecked_out_main_lands_with_update_ref(self):
        tid = self.task('feature', {'base.txt': 'landed content'})
        self.r.git('checkout', '-q', '--detach')
        old = self.r.git('rev-parse', 'main')
        with patch.object(landing, 'git', wraps=landing.git) as run:
            res = self.land(push=False)
        self.assertEqual(res['landed'], [tid])
        self.assertTrue(any(c.args[1:] == ('update-ref', 'refs/heads/main', res['tip'], old)
                            for c in run.call_args_list))
        self.assertEqual(self.r.git('rev-parse', 'main'), res['tip'])
        self.assertEqual(self.r.git('rev-parse', 'HEAD'), old)
        self.assertEqual(self.r.git('status', '--porcelain'), '')
        self.assertEqual((self.r.repo / 'base.txt').read_text(), 'base\n')

    def test_moved_main_fails_landing_cas_and_releases_lock(self):
        self.r.commit('local.txt')
        self.task('feature', {'feature.txt': 'finished'})
        tasks = landing.form_batch(self.conn, self.r.repo, 'main')
        old = self.r.git('rev-parse', 'main')
        tip, tasks, _ = landing.build_tip(self.r.repo, self.work, old, tasks)
        receipt = self.gate(self.work, tip, tasks)
        real = landing.git
        moved = self.r.git('rev-parse', 'main^')

        def run(repo, *args, **kw):
            if args[:2] == ('worktree', 'list'):
                self.r.git('reset', '--hard', moved)
            return real(repo, *args, **kw)

        with patch.object(landing, 'git', side_effect=run):
            with self.assertRaisesRegex(ci.CIError, 'local main moved; rebase and regate'):
                landing.local_fast_forward(self.conn, self.r.repo, self.work, 'main', tip, receipt)
        self.assertEqual(self.r.git('rev-parse', 'main'), moved)
        self.assertEqual(self.r.git('status', '--porcelain'), '')
        self.assertIsNone(self.conn.execute('SELECT * FROM landing_lock').fetchone())

    def test_offline_pauses_repo_and_alerts_without_gating(self):
        tid = self.task('feature', {'feature.txt': 'finished'})
        self.r.git('remote', 'set-url', 'origin', str(self.r.root / 'offline.git'))
        res = self.land(push=False)
        self.assertEqual((res['outcome'], res['gates']), ('paused', 0))
        self.assertEqual(self.status(tid), 'reviewed')
        self.assertEqual(len(ops.open_questions(self.conn)), 1)
        self.assertEqual(self.land(push=False)['outcome'], 'paused')
        self.assertEqual(len(ops.open_questions(self.conn)), 1)

    def test_diverged_main_sends_reviewed_tasks_to_owner(self):
        tid = self.task('feature', {'feature.txt': 'finished'})
        self.r.commit('local.txt')
        sha = self.r.branch('upstream', {'upstream.txt': 'upstream'})
        self.r.git('push', '-q', 'origin', f'{sha}:main')
        res = self.land(push=False)
        self.assertEqual((res['outcome'], res['gates']), ('needs-owner', 0))
        self.assertEqual(self.status(tid), 'needs-owner')
        events = ops.events(self.conn, task_id=tid, kind='status')
        self.assertEqual(events[-1]['data']['reason'], 'main diverged from origin')

    def test_divergence_does_not_change_reviewed_tasks_in_another_repo(self):
        tid = self.task('feature', {'feature.txt': 'finished'})
        other = Repo()
        self.addCleanup(other.cleanup)
        other.branch('feature', {'feature.txt': 'other repo'})
        task = ops.create_task(self.conn, 'other repo', 'acme-app', repo_path=str(other.repo))
        ops.update_task(self.conn, task['id'], branch='feature')
        ops.approve(self.conn, task['id'])
        for status in ('claimed', 'running', 'gated', 'reviewed'):
            ops.set_status(self.conn, task['id'], status, actor='system')
        self.r.commit('local.txt')
        sha = self.r.branch('upstream', {'upstream.txt': 'upstream'})
        self.r.git('push', '-q', 'origin', f'{sha}:main')
        self.assertEqual(self.land(push=False)['outcome'], 'needs-owner')
        self.assertEqual(self.status(tid), 'needs-owner')
        self.assertEqual(self.status(task['id']), 'reviewed')

    def test_owner_main_commit_during_gate_is_preserved_and_batch_regated(self):
        tid = self.task('feature', {'feature.txt': 'finished'})
        real_gate = self.gate
        moved = []

        def moving_gate(*args):
            receipt = real_gate(*args)
            if not moved:
                moved.append(self.r.commit('owner.txt', 'owner commit'))
            return receipt

        self.gate = moving_gate
        res = self.land(push=False)
        self.assertEqual((res['outcome'], res['gates']), ('landed', 2))
        self.assertEqual(self.status(tid), 'landed')
        self.assertEqual(self.r.git('merge-base', moved[0], res['tip']), moved[0])
        self.assertEqual((self.r.repo / 'owner.txt').read_text(), 'owner commit')

    def test_remote_move_with_conflict_needs_owner(self):
        tid = self.task('feature', {'base.txt': 'task change'})
        real_gate = self.gate
        moved = []

        def moving_gate(*args):
            receipt = real_gate(*args)
            if not moved:
                sha = self.r.branch('upstream', {'base.txt': 'upstream change'})
                self.r.git('push', '-q', 'origin', f'{sha}:main')
                moved.append(sha)
            return receipt

        self.gate = moving_gate
        res = self.land(push=False)
        self.assertEqual((res['outcome'], res['gates']), ('conflict', 1))
        self.assertEqual(self.status(tid), 'needs-owner')
        self.assertEqual(res['dropped'][tid], 'rebase conflict')
        self.assertEqual(self.r.git('rev-parse', 'main'), moved[0])

    def test_fetch_failure_immediately_before_landing_pauses_without_landing(self):
        tid = self.task('feature', {'feature.txt': 'finished'})
        before = self.r.git('rev-parse', 'main')
        real_gate = self.gate

        def offline_gate(*args):
            receipt = real_gate(*args)
            self.r.git('remote', 'set-url', 'origin', str(self.r.root / 'offline.git'))
            return receipt

        self.gate = offline_gate
        res = self.land(push=False)
        self.assertEqual((res['outcome'], res['gates']), ('paused', 1))
        self.assertEqual(self.status(tid), 'reviewed')
        self.assertEqual(self.r.git('rev-parse', 'main'), before)
        self.assertEqual(len(ops.open_questions(self.conn)), 1)

    def test_default_push_off_supports_consecutive_local_landings(self):
        a = self.task('first', {'first.txt': 'first'})
        before = self.r.remote_head()
        real_git = origin.git
        with patch.object(origin, 'git', wraps=real_git) as calls:
            first = landing.run_batch(self.conn, self.r.repo, self.gate, workdir=self.work)
            b = self.task('second', {'second.txt': 'second'})
            second = landing.run_batch(self.conn, self.r.repo, self.gate, workdir=self.work)
        self.assertEqual(first['landed'], [a])
        self.assertEqual(second['landed'], [b])
        self.assertEqual(self.r.git('merge-base', first['tip'], second['tip']), first['tip'])
        self.assertEqual(self.r.remote_head(), before)
        self.assertFalse(any(call.args[1] == 'push' for call in calls.call_args_list))

    def test_push_failure_alerts_but_task_stays_landed(self):
        tid = self.task('feature', {'feature.txt': 'finished'})
        self.r.git('config', 'remote.origin.pushurl', str(self.r.root / 'offline.git'))
        res = self.land(push=True)
        self.assertEqual(res['outcome'], 'landed')
        self.assertEqual(self.status(tid), 'landed')
        self.assertEqual(self.r.git('rev-parse', 'main'), res['tip'])
        self.assertIn('push failed', res['alert'])
        self.assertEqual(len(ops.open_questions(self.conn)), 1)

    def test_dirty_main_holds_landing_and_preserves_owner_files_and_index(self):
        tid = self.task('feature', {'feature.txt': 'finished'})
        path = self.r.repo / 'base.txt'
        path.write_text('owner work\n')
        before = self.r.git('rev-parse', 'main')
        remote_before = self.r.remote_head()
        index = (self.r.repo / '.git' / 'index').read_bytes()
        res = self.land(push=False)
        self.assertEqual(res['outcome'], 'refused')
        self.assertIn('main checkout dirty', res['error'])
        self.assertEqual(self.r.git('rev-parse', 'main'), before)
        self.assertEqual(self.r.remote_head(), remote_before)
        self.assertEqual(path.read_text(), 'owner work\n')
        self.assertEqual((self.r.repo / '.git' / 'index').read_bytes(), index)
        self.assertEqual(self.status(tid), 'reviewed')

    def test_owner_edit_during_gate_holds_landing(self):
        tid = self.task('feature', {'feature.txt': 'finished'})
        self.gate = lambda checkout, commit, tasks: self.ci.run(self.prog, checkout, commit, base='main')
        real_gate = self.gate

        def owner_edits_during_gate(*args):
            receipt = real_gate(*args)
            (self.r.repo / 'base.txt').write_text('owner work\n')
            return receipt

        self.gate = owner_edits_during_gate
        before = self.r.git('rev-parse', 'main')
        res = self.land(push=False)
        self.assertEqual(res['outcome'], 'refused')
        self.assertIn('main checkout dirty', res['error'])
        self.assertEqual(self.r.git('rev-parse', 'main'), before)
        self.assertEqual(self.status(tid), 'reviewed')
        self.assertEqual((self.r.repo / 'base.txt').read_text(), 'owner work\n')

    def test_green_batch_lands_ff_only_and_marks_tasks(self):
        ids = [self.task(n, {f'{n}.txt': n}) for n in 'abc']
        res = self.land()
        self.assertEqual((res['outcome'], res['gates']), ('landed', 1))
        self.assertEqual(sorted(res['landed']), sorted(ids))
        self.assertEqual(self.r.remote_head(), res['tip'])
        for t in ids:
            self.assertEqual(self.status(t), 'landed')
            sha = landing.land_sha(self.conn, t)
            self.assertEqual(self.r.git('merge-base', '--is-ancestor', sha, res['tip']) , '')
        self.assertFalse(self.work.exists())
        v = landing.velocity(self.conn)
        self.assertEqual((v['tasks_landed'], v['gates'], v['red_rate']), (3, 1, 0.0))

    def test_unstructured_failure_bisects_without_guessing_file_ownership(self):
        a, b = self.task('a', {'a.txt': 'ok'}), self.task('b', {'b.txt': 'BAD'})
        d = fake_decider()
        res = self.land(decider=d)
        self.assertEqual((res['outcome'], res['landed'], res['dropped'], res['gates']), ('landed', [a], {b: 'bisect'}, 4))
        self.assertEqual([call[0] for call in d.calls], ['batch_culprit'])
        self.assertEqual(self.status(b), 'ready')
        self.assertIn('bad content', ops.get_task(self.conn, b)['handoff'])
        self.assertEqual(self.status(a), 'landed')

    def test_decide_above_tau_drops_named_culprit(self):
        x, y = self.task('x', {'x.txt': 'x'}), self.task('y', {'y.txt': 'y'})
        d = fake_decider(answer=y, acted=True)
        res = self.land(decider=d, size=2)
        self.assertEqual((res['outcome'], res['landed'], res['dropped']), ('landed', [x], {y: 'decide'}))
        self.assertEqual(len(d.calls), 1)
        self.assertEqual(d.calls[0][0], 'batch_culprit')
        self.assertEqual(set(d.calls[0][1]), {x, y})
        self.assertEqual(res['bisect_gates'], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 1)

    def test_below_tau_bisects_to_the_culprit(self):
        x, y, z = (self.task(n, {f'{n}.txt': n}) for n in 'xyz')
        res = self.land(decider=fake_decider(answer=x, acted=False), size=3)
        self.assertEqual((res['outcome'], sorted(res['landed']), res['dropped']), ('landed', sorted([x, z]), {y: 'bisect'}))
        self.assertGreaterEqual(res['bisect_gates'], 1)
        self.assertEqual(self.status(y), 'ready')
        self.assertEqual(landing.velocity(self.conn)['bisect_gates'], res['bisect_gates'])

    def test_decider_that_raises_falls_back_to_bisect(self):
        x, y = self.task('x', {'x.txt': 'x'}), self.task('y', {'y.txt': 'y'})

        def boom(*a, **k):
            raise RuntimeError('ollama down')
        res = self.land(decider=boom, size=2)
        self.assertEqual(res['dropped'], {y: 'bisect'})

    def test_whole_batch_red_requeues_all(self):
        a = self.task('a', {'a.txt': 'BAD'})
        res = self.land()
        self.assertEqual((res['outcome'], res['dropped']), ('red', {a: 'single'}))
        self.assertEqual(self.status(a), 'ready')
        self.assertEqual(self.r.remote_head(), self.r.git('rev-parse', 'origin/main'))

    def test_base_moved_during_gate_replays_and_regates(self):
        a = self.task('a', {'a.txt': 'a'})
        moved = {}

        def moving_gate(work, tip, tasks):
            receipt = self.gate(work, tip, tasks)
            if moved:
                return receipt
            other = self.r.root / 'other'
            self.r.git('clone', '-q', str(self.r.remote), str(other), cwd=self.r.root)
            self.r.git('config', 'user.name', 't', cwd=other)
            self.r.git('config', 'user.email', 't@x.invalid', cwd=other)
            self.r.commit('moved.txt', cwd=other)
            self.r.git('push', '-q', 'origin', 'HEAD:main', cwd=other)
            moved['sha'] = self.r.git('rev-parse', 'HEAD', cwd=other)
            return receipt
        res = landing.run_batch(self.conn, self.r.repo, moving_gate, workdir=self.work, decider=fake_decider())
        self.assertEqual((res['outcome'], res['gates']), ('landed', 2))
        self.assertEqual(self.r.remote_head(), moved['sha'])
        self.assertEqual(self.status(a), 'landed')
        self.assertEqual(self.r.git('merge-base', moved['sha'], res['tip']), moved['sha'])

    def test_verdict_for_another_sha_or_moved_branch_is_not_landable(self):
        a = self.task('a', {'a.txt': 'a'})
        run = ops.start_run(self.conn, a, 'reviewer', 'claude')
        ops.finish_run(self.conn, run, 'ok', review_verdict='LAND@' + self.r.git('rev-parse', 'origin/main'))
        self.assertEqual(landing.form_batch(self.conn, self.r.repo, 'origin/main', 5), [])
        self.assertEqual(self.land()['outcome'], 'empty')

    def test_branch_moved_after_formation_refuses_landing(self):
        a = self.task('a', {'a.txt': 'a'})

        def moving_gate(work, tip, tasks):
            receipt = self.gate(work, tip, tasks)
            wt = self.r.root / 'mv'
            self.r.git('worktree', 'add', '-q', wt, 'a')
            self.r.commit('late.txt', cwd=wt)
            self.r.git('worktree', 'remove', '--force', wt)
            return receipt
        res = landing.run_batch(self.conn, self.r.repo, moving_gate, workdir=self.work, decider=fake_decider())
        self.assertEqual(res['outcome'], 'refused')
        self.assertEqual(self.status(a), 'reviewed')
        self.assertEqual(self.r.remote_head(), self.r.git('rev-parse', 'origin/main'))

    def test_base_red_blames_nobody_holds_landing_and_alerts_owner(self):
        self.r.commit('poison.txt', 'BAD')
        self.r.git('push', '-q', 'origin', 'HEAD:main')
        self.r.git('fetch', '-q', 'origin')
        a, b = self.task('a', {'a.txt': 'a'}), self.task('b', {'b.txt': 'b'})
        res = self.land(size=2)
        self.assertEqual((res['outcome'], res['dropped'], res['landed']), ('base-red', {}, []))
        self.assertEqual((self.status(a), self.status(b)), ('reviewed', 'reviewed'))
        qs = ops.open_questions(self.conn)
        self.assertEqual(len(qs), 1)
        self.assertIn('red', qs[0]['title'])
        self.assertEqual(self.r.remote_head(), self.r.git('rev-parse', 'origin/main'))
        again = self.land(size=2)                                   # held: no more gating at all
        self.assertEqual((again['outcome'], again['gates']), ('held', 0))
        ops.answer_question(self.conn, qs[0]['id'], choice=landing.RETRY)
        self.assertEqual(self.land(size=2)['outcome'], 'base-red')   # base still red: gated once more, held again
        self.assertEqual(len(ops.open_questions(self.conn)), 1)

    def test_base_status_is_cached_per_sha(self):
        a, b = self.task('a', {'a.txt': 'BAD'}), self.task('b', {'b.txt': 'BAD'})
        res = self.land(size=2)
        self.assertEqual(res['outcome'], 'red')
        from drover.ledger import landing as cache
        from drover.ledger.waiting import repo_key
        sha = self.r.git('rev-parse', 'main')
        name = repo_key(None, self.r.repo)
        self.assertEqual(cache.base_status(self.conn, name, sha), 'green')
        cache.record_base(self.conn, 'other', sha, 'red')
        self.assertEqual(cache.base_status(self.conn, name, sha), 'green')
        self.assertEqual(cache.base_status(self.conn, 'other', sha), 'red')

    @integration    # the hub's own CI service starts a real detached runner and adopts it after an interruption
    def test_hub_retry_adopts_interrupted_ci_and_runs_script_once(self):
        from hub.dispatch import LandingService
        from hub.settings import Settings
        from hub.store import Store

        configs = self.r.root / 'repos'
        configs.mkdir()
        trees = self.r.root / 'worktrees'
        (configs / 'p.toml').write_text(
            f'name="p"\nrepo={json.dumps(str(self.r.repo))}\n'
            f'worktrees={json.dumps(str(trees))}\n')
        self.r.commit('.drover/config.toml', 'schema_version=1\n[scripts]\ntest=".drover/scripts/check.ps1"\n[gate]\ndocs_only=false\n')
        store = Store(self.r.root / 'ledger.db')
        self.addCleanup(store.close)
        settings = Settings(runs_dir=self.r.root / 'receipts')
        service = LandingService(store, settings, repos_dir=configs)
        ops.set_setting(self.conn, 'dispatch_enabled', 'true')
        work = trees / 'drover-landing-p'
        count, release = self.r.root / 'count', self.r.root / 'release'
        self.addCleanup(release.touch)
        # The script stays live until the test lets the adopted runner finish.
        self.r.commit('.drover/scripts/check.ps1',
                      f"Add-Content '{count}' started\n"
                      f"while (-not (Test-Path '{release}')) {{ Start-Sleep -Milliseconds 50 }}\nexit 0\n")
        self.r.git('push', '-q', 'origin', 'HEAD:main')
        tid = self.task('a', {'a.txt': 'a'})
        def fail_heartbeat(*args):
            deadline = time.monotonic() + 20
            while not count.exists() and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue(count.exists())
            raise sqlite3.OperationalError('database is locked')
        with patch.object(slots, 'lease_heartbeat', side_effect=fail_heartbeat):
            with self.assertRaises(ci.CIInterrupted):
                service.tick()
        self.assertTrue(work.is_dir())
        self.assertTrue((work.parent / ('.' + work.name + '.merge-group.json')).is_file())
        self.assertEqual(len(slots.lease_queue(self.conn)), 1)
        self.assertEqual(self.status(tid), 'reviewed')
        release.touch()
        # A fresh hub service must adopt saved execution before rebuilding the checkout.
        service = LandingService(store, settings, repos_dir=configs)
        result = service.tick()['p']
        self.assertEqual(result['outcome'], 'landed')
        self.assertEqual(count.read_text().splitlines(), ['started'])
        self.assertFalse(work.exists())
        self.assertEqual(slots.lease_queue(self.conn), [])
        self.assertEqual(len(list(settings.runs_dir.glob('ci-*/status.json'))), 1)

    def test_exception_mid_run_still_records_the_batch(self):
        self.task('a', {'a.txt': 'a'})

        def boom(*a):
            raise RuntimeError('gate exploded')
        with self.assertRaises(RuntimeError):
            landing.run_batch(self.conn, self.r.repo, boom, workdir=self.work, decider=fake_decider())
        row = self.conn.execute("SELECT outcome FROM landing_batches").fetchone()
        self.assertEqual(row[0], 'error')

    def test_wrong_sha_receipt_cannot_fast_forward(self):
        self.task('a', {'a.txt': 'a'})
        seen = {}

        def lying_gate(work, tip, tasks):
            receipt = dict(self.gate(work, tip, tasks))
            receipt['sha'] = self.r.git('rev-parse', 'origin/main')
            seen['r'] = receipt
            return receipt
        res = landing.run_batch(self.conn, self.r.repo, lying_gate, workdir=self.work, decider=fake_decider())
        self.assertEqual(res['outcome'], 'refused')
        self.assertIn('receipt is for', res['error'])
        self.assertEqual(self.r.remote_head(), self.r.git('rev-parse', 'origin/main'))

    def test_fast_forward_pushes_a_sha_not_a_ref(self):
        import re
        a = self.task('a', {'a.txt': 'a'})
        calls = []
        orig = origin.git

        def spy(repo, *args, **kw):
            calls.append(args)
            return orig(repo, *args, **kw)
        origin.git = spy
        self.addCleanup(setattr, origin, 'git', orig)
        self.land()
        push = [c for c in calls if c[0] == 'push'][0]
        self.assertRegex(push[2].split(':')[0], r'^[0-9a-f]{40}$')

    def test_build_tip_replays_the_recorded_head_not_the_live_branch(self):
        a = self.task('a', {'a.txt': 'a'})
        (t,) = landing.form_batch(self.conn, self.r.repo, 'origin/main', 1)
        wt = self.r.root / 'mv'
        self.r.git('worktree', 'add', '-q', wt, 'a')
        self.r.commit('late.txt', cwd=wt)
        self.r.git('worktree', 'remove', '--force', wt)
        tip, applied, _ = landing.build_tip(self.r.repo, self.work, self.r.git('rev-parse', 'origin/main'), [t])
        self.addCleanup(self.r.git, 'worktree', 'remove', '--force', str(self.work))
        files = self.r.git('ls-tree', '-r', '--name-only', tip).split()
        self.assertIn('a.txt', files)
        self.assertNotIn('late.txt', files)

    def test_merge_group_replay_keeps_the_commit_being_checked_after_restart(self):
        self.task('feature', {'feature.txt': 'change'})
        tasks = landing.form_batch(self.conn, self.r.repo, 'main', 1)
        base = self.r.git('rev-parse', 'main')
        first = landing.build_tip(self.r.repo, self.work, base, tasks)
        self.addCleanup(self.r.git, 'worktree', 'remove', '--force', str(self.work))
        with patch.dict('os.environ', {'GIT_COMMITTER_DATE': '2001-01-01T00:00:00Z'}):
            resumed = landing.build_tip(self.r.repo, self.work, base, tasks)
        self.assertEqual(resumed, first)
        self.assertEqual(self.r.git('rev-parse', 'HEAD', cwd=self.work), first[0])

    def test_lock_release_only_by_owner(self):
        landing_storage.take_lock(self.conn, 'mine')
        landing_storage.drop_lock(self.conn, 'other')
        with self.assertRaises(landing_storage.LockHeld):
            landing_storage.take_lock(self.conn, 'third')
        landing_storage.drop_lock(self.conn, 'mine')
        landing_storage.take_lock(self.conn, 'third')

    def test_owner_matching_is_path_component_aware(self):
        tasks = [{'id': 'A', 'files': ['util.py']}, {'id': 'B', 'files': ['lib/util.py']}]
        self.assertEqual(landing.owners_of([{'file': 'lib/util.py'}], tasks), ['B'])
        self.assertEqual(landing.owners_of([{'file': '/w/repo/lib/util.py'}], tasks), ['B'])
        self.assertEqual(landing.owners_of([{'file': '/w/repo/xlib/util.py'}], tasks), ['A'])
        self.assertEqual(landing.owners_of([{'file': 'xlib/util.py'}], tasks), [])

    def test_missing_review_at_land_time_refuses(self):
        a = self.task('a', {'a.txt': 'a'})
        orig = landing.review_landed
        calls = {'n': 0}

        def flaky(conn, tid, head):
            calls['n'] += 1
            return calls['n'] == 1       # LAND at formation, gone by landing
        landing.review_landed = flaky
        self.addCleanup(setattr, landing, 'review_landed', orig)
        res = self.land()
        self.assertEqual(res['outcome'], 'refused')
        self.assertEqual(self.status(a), 'reviewed')

    def test_lock_held_refuses(self):
        self.task('a', {'a.txt': 'a'})
        landing_storage.take_lock(self.conn, 'someone')
        res = self.land()
        self.assertEqual(res['outcome'], 'refused')
        self.assertIn('lock held', res['error'])
        self.assertEqual(self.r.remote_head(), self.r.git('rev-parse', 'origin/main'))

    def test_empty_when_nothing_reviewed(self):
        self.assertEqual(self.land()['outcome'], 'empty')


if __name__ == '__main__':
    unittest.main()


@integration
class RealRunnerLandingTests(LandingCase):
    """The same landing scenarios as RunTests, but each gate goes through the real drover.runner and its polling.
    One green, one red and one bisecting path keep the runner/landing wiring covered; RunTests covers the rest."""

    def make_ci(self, runs):
        return ci.CI(runs, poll_s=0.05)

    test_green_batch_lands_ff_only_and_marks_tasks = RunTests.test_green_batch_lands_ff_only_and_marks_tasks
    test_whole_batch_red_requeues_all = RunTests.test_whole_batch_red_requeues_all
    test_below_tau_bisects_to_the_culprit = RunTests.test_below_tau_bisects_to_the_culprit
    test_base_red_blames_nobody_holds_landing_and_alerts_owner = RunTests.test_base_red_blames_nobody_holds_landing_and_alerts_owner
