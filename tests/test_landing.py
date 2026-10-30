from drover import placement
import json
import sqlite3
import time
import sys
import subprocess
import unittest
from pathlib import Path, PureWindowsPath
from unittest.mock import patch

from drover.port import DirectPort
from drover import ci, landing, landing_batch, landing_control, origin, repos
from drover.ledger import ci as slots
from drover.ledger import landing as landing_storage
from drover.ledger import db as ledger_db
from drover.ledger import ops
from tests.fakeci import InProcessLauncher, resources_go
from tests.gitfix import Repo
from tests.tiers import integration
from tests.landing_fixture import MemoryLandingCase, ProcessFreeCase

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
        self.prog = repos.normalize({'name': 'p', 'repo': str(self.r.repo),
                                     'scripts': {'test': '.drover/scripts/check.ps1'}, 'gate': {'docs_only': False}})
        self.ci = self.make_ci(self.r.root / 'receipts')
        self.gate = lambda checkout, commit, tasks: self.ci.run(self.prog, checkout, commit, base='origin/main')
        self.work = self.r.root / 'batch-wt'

    def make_ci(self, runs):
        """Real-repository contracts use a live script; runner contracts override this."""
        return ci.CI(runs, InProcessLauncher(runs), resource_check=resources_go)

    def task(self, name, files, priority='P2', review='LAND', base='origin/main'):
        self.r.branch(name, files, base=base)
        t = ops.create_task(self.conn, name, 'p', repo_path=str(self.r.repo), priority=priority, scope_files=list(files))
        ops.update_task(self.conn, t['id'], branch=name)
        ops.approve(self.conn, t['id'])
        from tests.ledger_fixture import stage
        sha = self.r.git('rev-parse', name)
        stage(self.conn, t['id'], 'reviewed', sha)
        if review != 'LAND':
            run = ops.start_run(self.conn, t['id'], 'reviewer', 'claude')
            ops.finish_run(self.conn, run, 'ok', review_verdict=review)
            if review is None:
                self.conn.execute("UPDATE runs SET review_verdict=NULL WHERE task_id=?", (t['id'],))
        return t['id']

    def status(self, tid):
        return ops.get_task(self.conn, tid)['status']

    def land(self, **kw):
        kw.setdefault('push', True)
        kw.setdefault('decider', fake_decider())
        return landing.run_batch(self.conn, self.r.repo, self.gate, land_stage=DirectPort(self.conn).land_stage, workdir=self.work, **kw)


class WorktreeErrorTests(ProcessFreeCase):
    def test_landing_does_not_retry_an_unrelated_error_mentioning_relative_paths(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            work = Path(root) / 'batch'
            result = subprocess.CompletedProcess([], 128, '', 'fatal: relative-paths metadata is corrupt')
            with patch.object(landing_batch, 'git', return_value=result) as run:
                with self.assertRaisesRegex(ci.CIError, 'metadata is corrupt'):
                    landing.build_tip(Path(root) / 'repo', work, 'a' * 40, [])
            self.assertEqual(run.call_count, 1)


class PushContractTests(ProcessFreeCase):
    def test_fast_forward_pushes_a_sha_not_a_ref(self):
        sha = 'a' * 40
        previous = 'b' * 40
        with patch.object(origin, 'git', side_effect=[
                subprocess.CompletedProcess([], 0, sha, ''),
                subprocess.CompletedProcess([], 0, previous, ''),
                subprocess.CompletedProcess([], 0, '', ''),
                subprocess.CompletedProcess([], 0, '', '')]) as calls:
            origin.push_main(Path('fixture'))
        push = [call.args[1:] for call in calls.call_args_list if call.args[1] == 'push'][0]
        self.assertRegex(push[2].split(':')[0], r'^[0-9a-f]{40}$')
        self.assertEqual(push, ('push', 'origin', sha + ':refs/heads/main'))





class FormationTests(MemoryLandingCase):
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
        self.task('bare', {'bare.txt': 'b'}, review='LAND')
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


class RunTests(MemoryLandingCase):

    def test_offline_pauses_repo_and_alerts_without_gating(self):
        tid = self.task('feature', {'feature.txt': 'finished'})
        self.r.offline = True
        res = self.land(push=False)
        self.assertEqual((res['outcome'], res['gates']), ('paused', 0))
        self.assertEqual(self.status(tid), 'reviewed')
        self.assertEqual(len(ops.open_questions(self.conn)), 1)
        self.assertEqual(self.land(push=False)['outcome'], 'paused')
        self.assertEqual(len(ops.open_questions(self.conn)), 1)

    def test_diverged_main_sends_reviewed_tasks_to_owner(self):
        tid = self.task('feature', {'feature.txt': 'finished'})
        self.r.owner_commit({'local.txt': 'local'})
        sha = self.r.branch('upstream', {'upstream.txt': 'upstream'})
        self.r.remote = sha
        res = self.land(push=False)
        self.assertEqual((res['outcome'], res['gates']), ('needs-owner', 0))
        self.assertEqual(self.status(tid), 'needs-owner')
        events = ops.events(self.conn, task_id=tid, kind='status')
        self.assertEqual(events[-1]['data']['reason'], 'main diverged from origin')

    def test_divergence_does_not_change_reviewed_tasks_in_another_repo(self):
        tid = self.task('feature', {'feature.txt': 'finished'})
        other = self.task('other', {'other.txt': 'other repo'}, repo_path=self.r.root / 'other-repo')
        self.r.owner_commit({'local.txt': 'local'})
        sha = self.r.branch('upstream', {'upstream.txt': 'upstream'})
        self.r.remote = sha
        self.assertEqual(self.land(push=False)['outcome'], 'needs-owner')
        self.assertEqual(self.status(tid), 'needs-owner')
        self.assertEqual(self.status(other), 'reviewed')

    def test_owner_main_commit_during_gate_is_preserved_and_batch_regated(self):
        tid = self.task('feature', {'feature.txt': 'finished'})
        real_gate = self.gate
        moved = []

        def moving_gate(*args):
            receipt = real_gate(*args)
            if not moved:
                moved.append(self.r.owner_commit({'owner.txt': 'owner commit'}))
            return receipt

        self.gate = moving_gate
        res = self.land(push=False)
        self.assertEqual((res['outcome'], res['gates']), ('landed', 2))
        self.assertEqual(self.status(tid), 'landed')
        self.assertTrue(self.r.ancestor(moved[0], res['tip']))
        self.assertEqual(self.r.nodes[res['tip']][1]['owner.txt'], 'owner commit')

    def test_remote_move_with_conflict_needs_owner(self):
        tid = self.task('feature', {'base.txt': 'task change'})
        real_gate = self.gate
        moved = []

        def moving_gate(*args):
            receipt = real_gate(*args)
            if not moved:
                sha = self.r.branch('upstream', {'base.txt': 'upstream change'})
                self.r.remote = sha
                self.r.conflicts.add(tid)
                moved.append(sha)
            return receipt

        self.gate = moving_gate
        res = self.land(push=False)
        self.assertEqual((res['outcome'], res['gates']), ('conflict', 1))
        self.assertEqual(self.status(tid), 'needs-owner')
        self.assertEqual(res['dropped'][tid], 'rebase conflict')
        self.assertEqual(self.r.heads['main'], moved[0])

    def test_fetch_failure_immediately_before_landing_pauses_without_landing(self):
        tid = self.task('feature', {'feature.txt': 'finished'})
        before = self.r.heads['main']
        real_gate = self.gate

        def offline_gate(*args):
            receipt = real_gate(*args)
            self.r.offline = True
            return receipt

        self.gate = offline_gate
        res = self.land(push=False)
        self.assertEqual((res['outcome'], res['gates']), ('paused', 1))
        self.assertEqual(self.status(tid), 'reviewed')
        self.assertEqual(self.r.heads['main'], before)
        self.assertEqual(len(ops.open_questions(self.conn)), 1)

    def test_default_push_off_supports_consecutive_local_landings(self):
        a = self.task('first', {'first.txt': 'first'})
        before = self.r.remote
        first = landing.run_batch(self.conn, self.r.repo, self.gate, land_stage=DirectPort(self.conn).land_stage, workdir=self.work, required_platforms=['wsl'])
        b = self.task('second', {'second.txt': 'second'})
        second = landing.run_batch(self.conn, self.r.repo, self.gate, land_stage=DirectPort(self.conn).land_stage, workdir=self.work, required_platforms=['wsl'])
        self.assertEqual(first['landed'], [a])
        self.assertEqual(second['landed'], [b])
        self.assertTrue(self.r.ancestor(first['tip'], second['tip']))
        self.assertEqual(self.r.remote, before)
        self.assertEqual(self.r.pushes, [])

    def test_push_failure_alerts_but_task_stays_landed(self):
        tid = self.task('feature', {'feature.txt': 'finished'})
        self.r.push_failed = True
        res = self.land(push=True)
        self.assertEqual(res['outcome'], 'landed')
        self.assertEqual(self.status(tid), 'landed')
        self.assertEqual(self.r.heads['main'], res['tip'])
        self.assertIn('push failed', res['alert'])
        self.assertEqual(len(ops.open_questions(self.conn)), 1)

    def test_owner_edit_during_gate_holds_landing(self):
        tid = self.task('feature', {'base.txt': 'task change\n'})
        real_gate = self.gate

        def owner_edits_during_gate(*args):
            receipt = real_gate(*args)
            self.r.owner_files['base.txt'] = 'owner work\n'
            return receipt

        self.gate = owner_edits_during_gate
        before = self.r.heads['main']
        res = self.land(push=False)
        self.assertEqual(res['outcome'], 'refused')
        self.assertIn('main checkout dirty', res['error'])
        self.assertEqual(self.r.heads['main'], before)
        self.assertEqual(self.status(tid), 'reviewed')
        self.assertEqual(self.r.owner_files['base.txt'], 'owner work\n')

    def test_green_batch_lands_ff_only_and_marks_tasks(self):
        ids = [self.task(n, {f'{n}.txt': n}) for n in 'abc']
        res = self.land()
        self.assertEqual((res['outcome'], res['gates']), ('landed', 1))
        self.assertEqual(sorted(res['landed']), sorted(ids))
        self.assertEqual(self.r.remote, res['tip'])
        self.assertEqual(self.r.owner_files, self.r.nodes[res['tip']][1])
        for t in ids:
            self.assertEqual(self.status(t), 'landed')
            sha = landing.land_sha(self.conn, t)
            self.assertTrue(self.r.ancestor(sha, res['tip']))
        self.assertFalse(self.r.work_active)
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
        self.assertEqual(self.r.remote, self.r.tracking)

    def test_base_moved_during_gate_replays_and_regates(self):
        a = self.task('a', {'a.txt': 'a'})
        moved = {}

        def moving_gate(work, tip, tasks):
            receipt = self.gate(work, tip, tasks)
            if moved:
                return receipt
            moved['sha'] = self.r.branch('upstream', {'moved.txt': 'moved'})
            self.r.remote = moved['sha']
            return receipt
        res = landing.run_batch(self.conn, self.r.repo, moving_gate, land_stage=DirectPort(self.conn).land_stage, workdir=self.work, decider=fake_decider(), required_platforms=['wsl'])
        self.assertEqual((res['outcome'], res['gates']), ('landed', 2))
        self.assertEqual(self.r.remote, moved['sha'])
        self.assertEqual(self.status(a), 'landed')
        self.assertTrue(self.r.ancestor(moved['sha'], res['tip']))

    def test_verdict_for_another_sha_or_moved_branch_is_not_landable(self):
        a = self.task('a', {'a.txt': 'a'})
        run = ops.start_run(self.conn, a, 'reviewer', 'claude')
        ops.finish_run(self.conn, run, 'ok', review_verdict='LAND@' + self.r.tracking)
        self.assertEqual(landing.form_batch(self.conn, self.r.repo, 'origin/main', 5), [])
        self.assertEqual(self.land()['outcome'], 'empty')

    def test_branch_moved_after_formation_refuses_landing(self):
        a = self.task('a', {'a.txt': 'a'})

        def moving_gate(work, tip, tasks):
            receipt = self.gate(work, tip, tasks)
            self.r.heads['a'] = self.r.commit(self.r.heads['a'], {'late.txt': 'late'})
            return receipt
        res = landing.run_batch(self.conn, self.r.repo, moving_gate, land_stage=DirectPort(self.conn).land_stage, workdir=self.work, decider=fake_decider(), required_platforms=['wsl'])
        self.assertEqual(res['outcome'], 'refused')
        self.assertEqual(self.status(a), 'reviewed')
        self.assertEqual(self.r.remote, self.r.tracking)

    def test_base_red_blames_nobody_holds_landing_and_alerts_owner(self):
        self.r.remote = self.r.tracking = self.r.owner_commit({'poison.txt': 'BAD'})
        a, b = self.task('a', {'a.txt': 'a'}), self.task('b', {'b.txt': 'b'})
        res = self.land(size=2)
        self.assertEqual((res['outcome'], res['dropped'], res['landed']), ('base-red', {}, []))
        self.assertEqual((self.status(a), self.status(b)), ('reviewed', 'reviewed'))
        qs = ops.open_questions(self.conn)
        self.assertEqual(len(qs), 1)
        self.assertIn('red', qs[0]['title'])
        self.assertEqual(self.r.remote, self.r.tracking)
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
        sha = self.r.heads['main']
        name = repo_key(None, self.r.repo)
        self.assertEqual(cache.base_status(self.conn, name, sha), 'green')
        cache.record_base(self.conn, 'other', sha, 'red')
        self.assertEqual(cache.base_status(self.conn, name, sha), 'green')
        self.assertEqual(cache.base_status(self.conn, 'other', sha), 'red')

    def test_exception_mid_run_still_records_the_batch(self):
        self.task('a', {'a.txt': 'a'})

        def boom(*a):
            raise RuntimeError('gate exploded')
        with self.assertRaises(RuntimeError):
            landing.run_batch(self.conn, self.r.repo, boom, land_stage=DirectPort(self.conn).land_stage, workdir=self.work, decider=fake_decider(), required_platforms=['wsl'])
        row = self.conn.execute("SELECT outcome FROM landing_batches").fetchone()
        self.assertEqual(row[0], 'error')

    def test_wrong_sha_receipt_cannot_fast_forward(self):
        self.task('a', {'a.txt': 'a'})
        seen = {}

        def lying_gate(work, tip, tasks):
            receipt = dict(self.gate(work, tip, tasks))
            receipt['sha'] = self.r.tracking
            seen['r'] = receipt
            return receipt
        res = landing.run_batch(self.conn, self.r.repo, lying_gate, land_stage=DirectPort(self.conn).land_stage, workdir=self.work, decider=fake_decider(), required_platforms=['wsl'])
        self.assertEqual(res['outcome'], 'refused')
        self.assertIn('CI', res['error'])
        self.assertEqual(self.r.remote, self.r.tracking)

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
        landing.review_landed = landing_batch.review_landed = flaky
        self.addCleanup(setattr, landing, 'review_landed', orig); self.addCleanup(setattr, landing_batch, 'review_landed', orig)
        res = self.land()
        self.assertEqual(res['outcome'], 'refused')
        self.assertEqual(self.status(a), 'reviewed')

    def test_lock_held_refuses(self):
        self.task('a', {'a.txt': 'a'})
        landing_storage.take_lock(self.conn, 'someone')
        res = self.land()
        self.assertEqual(res['outcome'], 'refused')
        self.assertIn('lock held', res['error'])
        self.assertEqual(self.r.remote, self.r.tracking)

    def test_empty_when_nothing_reviewed(self):
        self.assertEqual(self.land()['outcome'], 'empty')





if __name__ == '__main__':
    unittest.main()
