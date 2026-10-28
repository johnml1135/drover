"""Removal, revalidation and usable restoration records at the public boundary."""
import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest import mock

from drover import placement
from tests.gitfix import sh
from tests.test_disk_worktrees import WorktreeCase
from tests.disk_fixture import MemoryCleanupCase
from tests.tiers import integration


class Removal(MemoryCleanupCase):
    def test_missing_folder_entry_by_path_other_side_survives(self):
        self.evidence.add('native', checks=dict(missing=True))
        self.evidence.add('foreign', checks=dict(other_side=True, missing=True))['row']['side_lock'] = 'foreign'
        self.cleanup()
        self.assertEqual(self.evidence.operations, [])
        self.cleanup(apply=True)
        self.assertFalse(self.kept('native'))
        self.assertTrue(self.kept('foreign'))
        self.assertEqual(self.evidence.operations, [('remove', 'native')])
        self.assertEqual(self.evidence.paths['foreign']['row']['side_lock'], 'foreign')

    def test_ignored_content_outside_declared_outputs_keeps_worktree(self):
        for name, files, removable in (
            ('environment', ['.venv/keep'], False), ('undeclared', ['data/keep'], False),
            ('mixed', ['generated/disposable', 'data/keep'], False),
            ('safe', ['generated/disposable'], True), ('nested-env', ['generated/.venv/keep'], False)):
            with self.subTest(case=name):
                observation = self.evidence.add(name)
                path = Path(name).absolute()
                observation['row']['ignored'] = [str(path / p) for p in files]
                from drover.disk_worktrees import ignored_outside
                observation['excluded'] = ignored_outside(
                    path, observation['row']['ignored'], ['generated'])
                result = self.cleanup(apply=True)
                self.assertEqual(self.kept(name), not removable, result)
                if not removable:
                    self.assertEqual(next(r for r in result['items'] if r['path'] == name)['reason_code'], 'ignored_content')
                    self.assertEqual(observation['row']['ignored'], [str(path / p) for p in files])
        approved = self.evidence.add('approved')
        approved_path = Path('approved').absolute()
        approved['row']['ignored'] = [str(approved_path / '.venv/keep')]
        approved['excluded'] = ignored_outside(
            approved_path, approved['row']['ignored'], ['generated'])
        approved['settlement']['merged'] = False
        result = self.cleanup(apply=True, approved_path='approved')
        self.assertTrue(self.kept('approved'), result)
        self.assertIn('approved', [r['path'] for r in result['items']])

    def test_dry_run_then_apply_selected_path_never_prunes(self):
        self.evidence.add('selected')
        self.evidence.add('dirty')['row']['dirty'] = ['dirty/scratch.txt']
        self.evidence.add('unmerged')['settlement']['merged'] = False
        self.evidence.add('ghost', checks=dict(missing=True, other_side=True))
        before = self.evidence.observe(dict(now=self.now, tasks=self.tasks, apply=False))
        self.cleanup()
        self.assertEqual(self.evidence.observe(dict(now=self.now, tasks=self.tasks, apply=False)), before)
        self.assertEqual(self.store.state, {})
        self.cleanup(apply=True)
        self.assertFalse(self.kept('selected'))
        for path in ('dirty', 'unmerged', 'ghost'):
            self.assertTrue(self.kept(path))
        self.assertEqual(self.evidence.operations, [('remove', 'selected')])
        self.assertEqual(self.evidence.paths['dirty']['row']['dirty'], ['dirty/scratch.txt'])

    def test_missing_registration_of_live_process_is_kept(self):
        self.evidence.add('busy', checks=dict(missing=True, busy=True))
        self.cleanup()
        result = self.cleanup(apply=True)
        self.assertTrue(self.kept('busy'))
        self.assertEqual(result['items'][0]['reason_code'], 'busy')
        self.assertEqual(self.evidence.operations, [])

    def test_nested_registered_worktree_survives_outer_settlement(self):
        from drover import disk_worktrees
        outer_path, inner_path = 'outer', 'outer/generated/foreign-inner'
        outer = self.evidence.add(outer_path)
        inner = self.evidence.add(inner_path, checks=dict(other_side=True))
        inner['row']['side_lock'] = 'foreign'
        inner['payload'] = {'keep': 'foreign owner data'}
        request = dict(repos=[dict(repo='repo')])
        def registrations(repo):
            return [dict(worktree=o['row']['path'], locked=o['row']['side_lock'])
                    for o in self.evidence.observe(dict(now=self.now, tasks=self.tasks))]
        before = registrations('repo')
        with mock.patch.object(disk_worktrees, 'entries', side_effect=registrations):
            # The real detector sees the nested registration even without a gitfile.
            outer['nested'] = disk_worktrees.overlaps_worktrees(request, outer_path, outer_path)
            self.assertTrue(outer['nested'])
            self.assertFalse(disk_worktrees.overlaps_worktrees(request, 'unrelated', 'unrelated'))
            self.cleanup()
            self.cleanup(apply=True)
        self.assertTrue(self.kept(outer_path))
        self.assertTrue(self.kept(inner_path))
        self.assertEqual(inner['payload']['keep'], 'foreign owner data')
        self.assertEqual(registrations('repo'), before)
        self.assertIn(dict(worktree=inner_path, locked='foreign'), registrations('repo'))
        self.assertEqual(self.evidence.operations, [])

    def test_owner_approval_selects_only_named_path_and_keeps_safety_checks(self):
        for path in ('approved', 'unselected', 'dirty'):
            self.evidence.add(path)['settlement']['merged'] = False
        self.evidence.paths['dirty']['row']['dirty'] = ['precious']
        self.cleanup(apply=True, approved_path='dirty')
        self.assertTrue(self.kept('dirty'))
        self.cleanup(apply=True, approved_path='approved')
        self.assertFalse(self.kept('approved'))
        self.assertTrue(self.kept('unselected'))

    def test_decide_is_pure_and_journal_failure_retains_candidate(self):
        import copy
        from drover.disk_cleanup import decide
        self.evidence.add('orphan')['row']['tag'] = None
        request = dict(apply=True, now=self.now, tasks=self.tasks)
        observations = self.evidence.observe(request)
        state = {'quarantine': {}}
        before = copy.deepcopy((request, observations, state))
        with mock.patch.object(placement, 'cleanup_parts', side_effect=AssertionError('decision probed paths')) as _placement_patch_1, mock.patch('drover.placement_paths.cleanup_parts', _placement_patch_1):
            answer = decide(request, observations, state, self.now)
        self.assertEqual((request, observations, state), before)
        self.assertEqual(answer['events'][0]['kind'], 'quarantine_started')
        self.evidence.add('settled')
        self.store.fail_journal = True
        self.cleanup(apply=True)
        self.assertTrue(self.kept('settled'))
        self.assertEqual(self.evidence.operations, [])

    def test_reports_preserve_grace_and_activity_starts_a_full_new_grace(self):
        observation = self.evidence.add('orphan')
        observation['row']['tag'] = None
        self.cleanup(apply=True)
        since = self.store.state['quarantine']['orphan']['since']
        self.now += 2 * 86400
        self.cleanup()
        self.assertEqual(self.store.state['quarantine']['orphan']['since'], since)
        observation['identity'] = 'new activity'
        self.cleanup(apply=True)
        self.now += 86400
        self.cleanup(apply=True)
        self.assertTrue(self.kept('orphan'))
        self.now += 2 * 86400
        self.cleanup(apply=True)
        self.assertFalse(self.kept('orphan'))


@integration
class RemovalContracts(WorktreeCase):
    def cleanup(self, *, apply=False, mode='on-demand'):
        # Exercise the owning-side adapter, not twelve unrelated coordinator stages.
        from types import SimpleNamespace
        from drover.disk_side import serve
        request = dict(stage=4, mode=mode, apply=apply, now=self.now,
                       drive=dict(name='test-drive', path=str(self.root), level='queue', warn_gb=150),
                       repos=[dict(name='alpha', repo=str(self.r.repo), worktrees=str(self.trees),
                                   remote='origin', default_branch='main')],
                       paths=self.cfg['disk_cleanup'], state_dir=str(self.state), tasks=self.tasks)
        with mock.patch.object(placement, 'cleanup_processes', return_value=SimpleNamespace(busy=self.busy, reason=getattr(self, 'process_reason', None))), \
             mock.patch.object(placement, 'physical_disk_path', return_value=str(self.root)):
            return serve(request)

    def test_missing_folder_entry_by_path_other_side_survives(self):
        native = self.worktree('missing native')
        foreign = self.worktree('missing other', lock=self.other_side)
        shutil.rmtree(native)
        shutil.rmtree(foreign)
        branches = self.branches()
        self.cleanup()
        calls = []
        real_git = placement.git
        def git(repo, *args, **kw):
            calls.append(args)
            return real_git(repo, *args, **kw)
        with mock.patch.object(placement, 'git', side_effect=git) as _placement_patch_2, mock.patch('drover.placement_git.git', _placement_patch_2):
            self.cleanup(apply=True)
        listed = self.registrations()
        self.assertNotIn(str(native), listed)
        self.assertIn(str(foreign), listed)
        self.assertIn('locked ' + self.other_side, listed)
        self.assertEqual(self.branches(), branches)
        self.assertFalse(any('prune' in call for call in calls), calls)
        self.assertTrue(any('remove' in call and str(native) in tuple(map(str, call))
                            for call in calls), calls)
        self.check_git_syntax_and_uncertain_repository_trees_survive()

    def test_revalidate_dirty_head_lock_and_manifest(self):
        # Mutate after scanning on the final status query, before removal.
        for change in ('dirty', 'head', 'lock', 'status-error', 'ignored'):
            with self.subTest(change=change):
                wt = self.worktree('race-' + change)
                self.r.commit('done-' + change + '.txt', cwd=wt)
                self.merge(wt)
                self.cleanup()
                real_git = placement.git
                status_calls = []
                def git(repo, *args, **kw):
                    if str(repo) == str(wt) and 'status' in args:
                        status_calls.append(args)
                        if len(status_calls) == 2:
                            if change == 'dirty':
                                (wt / 'new data.txt').write_text('keep')
                            elif change == 'ignored':
                                (wt / 'data').mkdir(exist_ok=True)
                                (wt / 'data' / 'new').write_text('keep')
                            elif change == 'head':
                                self.r.commit('later.txt', cwd=wt)
                            elif change == 'lock':
                                self.r.git('worktree', 'unlock', wt)
                                self.r.git('worktree', 'lock', '--reason', self.other_side, wt)
                            elif change == 'status-error':
                                return subprocess.CompletedProcess(['git'], 128, '', 'unavailable')
                    return real_git(repo, *args, **kw)
                with mock.patch.object(placement, 'git', side_effect=git) as _placement_patch_4, mock.patch('drover.placement_git.git', _placement_patch_4):
                    result = self.cleanup(apply=True)
                self.assertGreaterEqual(len(status_calls), 2, 'must revalidate full status')
                self.assertTrue(wt.exists(), change)
                self.assertIn(str(wt), self.registrations())
                self.reported(result, wt)
                journal = self.state / 'disk-cleanup-manifest.jsonl'
                if journal.exists():
                    self.assertFalse(any(r['phase'] == 'completed' and r['path'] == str(wt)
                                         for r in self.journal()))

        wt = self.worktree('fresh unknown process')
        checks = []
        def unknown_after_selection(path):
            if path == str(wt):
                checks.append(path)
                return len(checks) >= 2
            return False
        self.busy = unknown_after_selection
        self.process_reason = 'process_visibility_unavailable'
        result = self.cleanup(apply=True)
        self.assertGreaterEqual(len(checks), 2, 'fresh process visibility must be required')
        self.assertTrue(wt.exists(), result)
        self.busy = lambda path: False
        self.process_reason = None
        self.check_owner_appearing_during_removal_keeps_unregistered_directory()

    def test_manifest_restores_branch_and_detached_head(self):
        from drover import ownership
        for detached in (False, True):
            with self.subTest(detached=detached):
                name = "restore 'quote' " if os.name == 'nt' else 'restore "quote"\n'
                wt = self.worktree(name + str(detached), detached=detached)
                sha = self.r.commit('payload.txt', 'precious\n', cwd=wt)
                # Detached unreferenced commits need terminal task proof, then a rescue ref.
                task = 'restore-' + str(detached)
                from drover.ledger import states
                ownership.tag(str(wt), 'alpha', 'drover:' + task, self.now + 86400, now=self.now)
                self.tasks = {'at': self.now, 'tasks': [{'id': task, 'state': states.LANDED, 'terminal': True, 'live': False,
                              'worktree': str(wt), 'owner': 'drover:' + task}], 'active_worktrees': []}
                branches = self.branches()
                self.cleanup()
                self.cleanup(apply=True)
                self.assertFalse(wt.exists())
                entries = [r for r in self.journal() if r['path'] == str(wt)]
                self.assertEqual([r['phase'] for r in entries], ['planned', 'completed'])
                record = entries[-1]
                self.assertEqual(record['head'], sha)
                self.assertEqual(record['side_lock'], self.side)
                self.assertIsInstance(record['restore_argv'], list)
                self.assertIn(str(wt), record['restore_argv'])
                if detached:
                    self.assertTrue(record['rescue_ref'])
                    self.assertEqual(self.r.git('rev-parse', record['rescue_ref']), sha)
                self.assertEqual(self.branches(), branches)
                sh(*record['restore_argv'])
                if record['lock_argv']:
                    sh(*record['lock_argv'])
                self.assertEqual(self.r.git('rev-parse', 'HEAD', cwd=wt), sha)
                self.assertEqual((wt / 'payload.txt').read_text(), 'precious\n')
                self.assertIn('locked ' + self.side, self.registrations())

    def test_journal_failure_prevents_unlock_and_delete(self):
        from drover import disk_state
        wt = self.worktree('journal-failure')
        from drover import disk_cleanup
        denied = disk_cleanup.remove_approved(self.cfg, str(self.r.repo), str(wt), runtime=self.runtime(real=True))
        self.assertFalse(denied['ok'])
        self.assertTrue(wt.exists(), 'owner deletion requires a matching report scope')
        self.cleanup()
        calls = []
        real_git = placement.git
        def git(repo, *args, **kwargs):
            calls.append(args)
            return real_git(repo, *args, **kwargs)
        with mock.patch.object(disk_state, 'append_manifest', side_effect=OSError('cannot sync journal')), \
             mock.patch.object(placement, 'git', side_effect=git) as _placement_patch_3, mock.patch('drover.placement_git.git', _placement_patch_3):
            result = self.cleanup(apply=True)
        self.assertTrue(wt.exists(), result)
        self.assertIn('locked ' + self.side, self.registrations())
        self.assertFalse(any('unlock' in c or 'remove' in c for c in calls), calls)

    def check_git_syntax_and_uncertain_repository_trees_survive(self):
        from tests import test_disk_fix2 as fix2
        configs = ('[core] bare=true\n', '\ufeff[core]\n bare = true\n',
                   '[CoRe] BaRe = "true" ; comment\n', '[core\n bare=true\n',
                   '[core]\n bare = "unterminated\n')
        for stage in (2, 3):
            for index, config in enumerate(configs):
                with self.subTest(stage=stage, config=config):
                    root = self.temp_root / f'git-{stage}-{index}'
                    self.file(root / 'config', config)
                    valuable = self.file(root / 'unpublished')
                    self.age(root, 40 * 86400)
                    self.cfg['disk_cleanup']['caches'] = [dict(path=str(root), placement=self.side)]
                    result = fix2.PathSafety.execute(self, fix2.PathSafety.request(self, stage))
                    self.assertTrue(valuable.exists(), result)
            for marker in ('HEAD', 'objects', 'refs'):
                with self.subTest(stage=stage, marker=marker):
                    root = self.temp_root / f'partial-{stage}-{marker}'
                    self.file(root / marker, 'incomplete repository')
                    valuable = self.file(root / 'unpublished')
                    self.age(root, 40 * 86400)
                    self.cfg['disk_cleanup']['caches'] = [dict(path=str(root), placement=self.side)]
                    result = fix2.PathSafety.execute(self, fix2.PathSafety.request(self, stage))
                    self.assertTrue(valuable.exists(), result)

    def check_owner_appearing_during_removal_keeps_unregistered_directory(self):
        import shutil
        from types import SimpleNamespace
        from drover import placement
        path = self.trees / 'late owner'
        self.trees.mkdir(exist_ok=True)
        shutil.copytree(self.r.repo, path, ignore=shutil.ignore_patterns('.git'))
        self.cleanup()
        self.cleanup(apply=True)
        self.now += 3 * 86400
        self.tasks = dict(at=self.now, active_worktrees=[], tasks=[])
        checks = []
        live = []
        metadata = placement.worktree_metadata
        def read_owner(worktree, name, *args):
            if str(worktree) == str(path) and live:
                import json
                return json.dumps(dict(v=2, repo='alpha', owner='session:late',
                    created=self.now, lease_until=self.now + 86400))
            return metadata(worktree, name, *args)
        def busy(candidate):
            if candidate == str(path):
                checks.append(candidate)
                if len(checks) == 2:
                    live.append(True)
            return False
        # run_cleanup installs the process adapter; busy is its declared test hook.
        self.busy = busy
        with mock.patch.object(placement, 'worktree_metadata', side_effect=read_owner):
            self.cleanup(apply=True)
            self.assertGreaterEqual(len(checks), 2)
            self.assertTrue(path.exists(), 'live ownership must be checked again before removal')
            live.clear()
            self.busy = lambda candidate: False
            self.cleanup(apply=True)
            self.assertTrue(path.exists(), 'a newly observed owner cancels elapsed quarantine')
            self.now += 3 * 86400
            self.cleanup(apply=True)
            self.assertFalse(path.exists(), 'released owner gets a fresh full grace period')
