"""Removal, revalidation and usable restoration records at the public boundary."""
import json
import shutil
import subprocess
from pathlib import Path
from unittest import mock

from drover import placement
from tests.gitfix import sh
from tests.test_disk_worktrees import WorktreeCase


class Removal(WorktreeCase):
    def test_missing_folder_entry_by_path_other_side_survives(self):
        native = self.worktree('missing native')
        foreign = self.worktree('missing other', lock='windows')
        shutil.rmtree(native)
        shutil.rmtree(foreign)
        branches = self.branches()
        self.cleanup()
        calls = []
        real_git = placement.git
        def git(repo, *args, **kw):
            calls.append(args)
            return real_git(repo, *args, **kw)
        with mock.patch.object(placement, 'git', side_effect=git):
            self.cleanup(apply=True)
        listed = self.registrations()
        self.assertNotIn(str(native), listed)
        self.assertIn(str(foreign), listed)
        self.assertIn('locked windows', listed)
        self.assertEqual(self.branches(), branches)
        self.assertFalse(any('prune' in call for call in calls), calls)
        self.assertTrue(any('remove' in call and str(native) in tuple(map(str, call))
                            for call in calls), calls)

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
                                self.r.git('worktree', 'lock', '--reason', 'windows', wt)
                            elif change == 'status-error':
                                return subprocess.CompletedProcess(['git'], 128, '', 'unavailable')
                    return real_git(repo, *args, **kw)
                with mock.patch.object(placement, 'git', side_effect=git):
                    result = self.cleanup(apply=True)
                self.assertGreaterEqual(len(status_calls), 2, 'must revalidate full status')
                self.assertTrue(wt.exists(), change)
                self.assertIn(str(wt), self.registrations())
                self.reported(result, wt)
                journal = self.state / 'disk-cleanup-manifest.jsonl'
                if journal.exists():
                    self.assertFalse(any(r['phase'] == 'completed' and r['path'] == str(wt)
                                         for r in self.journal()))

    def test_manifest_restores_branch_and_detached_head(self):
        from drover import ownership
        for detached in (False, True):
            with self.subTest(detached=detached):
                wt = self.worktree('restore "quote"\n' + str(detached), detached=detached)
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
                self.assertEqual(record['side_lock'], 'wsl')
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
                self.assertIn('locked wsl', self.registrations())

    def test_ignored_content_outside_declared_outputs_keeps_worktree(self):
        for name, files, removable in (
            ('environment', ['.venv/keep'], False),
            ('undeclared', ['data/keep'], False),
            ('mixed', ['generated/disposable', 'data/keep'], False),
            ('safe', ['generated/disposable'], True),
            ('nested-env', ['generated/.venv/keep'], False),
        ):
            with self.subTest(case=name):
                wt = self.worktree(name)
                self.r.commit('done-' + name, cwd=wt)
                self.merge(wt)
                for rel in files:
                    p = wt / rel
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text('sentinel')
                self.cleanup()
                result = self.cleanup(apply=True)
                self.assertEqual(wt.exists(), not removable)
                if not removable:
                    for rel in files:
                        if not rel.startswith('generated/') or '.venv' in rel:
                            self.assertEqual((wt / rel).read_text(), 'sentinel')
                    self.reported(result, wt / files[-1])
        late = self.worktree('late ignored file')
        self.r.commit('late.txt', cwd=late)
        self.merge(late)
        self.cleanup()
        real_git = placement.git
        checked = []
        def add_after_selection(repo, *args, **kwargs):
            if str(repo) == str(late) and 'status' in args:
                checked.append(args)
                if len(checked) == 2:
                    (late / 'data').mkdir()
                    (late / 'data' / 'arrived').write_text('keep late ignored content')
            return real_git(repo, *args, **kwargs)
        with mock.patch.object(placement, 'git', side_effect=add_after_selection):
            result = self.cleanup(apply=True)
        self.assertGreaterEqual(len(checked), 2)
        self.assertEqual((late / 'data' / 'arrived').read_text(), 'keep late ignored content')
        self.reported(result, late / 'data' / 'arrived')
        # Explicit owner approval still obeys the ignored-content rule.
        from drover import disk_cleanup
        approved = self.worktree('approved')
        (approved / '.venv').mkdir()
        (approved / '.venv' / 'keep').write_text('owner data')
        self.cleanup()
        with mock.patch.object(placement, 'cleanup_processes',
                               return_value=type('Idle', (), {'busy': lambda _, p: False})()), \
             mock.patch.object(placement, 'physical_disk_path', return_value=str(self.root)), \
             mock.patch('tempfile.gettempdir', return_value=str(self.temp_root)):
            result = disk_cleanup.remove_approved(self.cfg, str(self.r.repo), str(approved),
                                                  runtime=self.runtime(real=True))
        self.assertTrue(approved.exists())
        self.assertIn(str(approved), json.dumps(result))

    def test_dry_run_then_apply_selected_path_never_prunes(self):
        selected = self.worktree('selected')
        self.r.commit('selected.txt', cwd=selected)
        self.merge(selected)
        dirty = self.worktree('dirty unrelated')
        (dirty / 'scratch.txt').write_text('owner work')
        unmerged = self.worktree('unmerged')
        self.r.commit('unmerged.txt', cwd=unmerged)
        ghost = self.worktree('other-side ghost', lock='windows')
        shutil.rmtree(ghost)
        before = self.registrations()
        branches = self.branches()
        calls = []
        real_git = placement.git
        def git(repo, *args, **kw):
            calls.append(args)
            return real_git(repo, *args, **kw)
        with mock.patch.object(placement, 'git', side_effect=git):
            self.cleanup()
            self.assertEqual(self.registrations(), before)
            self.assertTrue(selected.exists())
            self.cleanup(apply=True)
        self.assertFalse(selected.exists())
        self.assertTrue(dirty.exists())
        self.assertEqual((dirty / 'scratch.txt').read_text(), 'owner work')
        self.assertTrue(unmerged.exists())
        listed = self.registrations()
        for path in (dirty, unmerged, ghost):
            self.assertIn(str(path), listed)
        self.assertIn('locked windows', listed)
        self.assertEqual(self.branches(), branches)
        self.assertFalse(any('prune' in call for call in calls), calls)

    def test_missing_registration_of_live_process_is_kept(self):
        wt = self.worktree('missing busy')
        shutil.rmtree(wt)
        self.busy_paths.add(str(wt))
        self.cleanup()
        before = self.registrations()
        self.cleanup(apply=True)
        self.assertEqual(self.registrations(), before)
        self.assertIn(str(wt), self.registrations())

    def test_nested_registered_worktree_survives_outer_settlement(self):
        wt = self.worktree('outer settled')
        self.r.commit('merged.txt', cwd=wt)
        self.merge(wt)
        inner = wt / 'generated' / 'foreign-inner'
        self.r.git('worktree', 'add', '-q', '-b', 'foreign-inner', inner, 'origin/main')
        self.r.git('worktree', 'lock', '--reason', 'windows', inner)
        (inner / '.git').unlink()
        keep = inner / 'keep'
        keep.write_text('foreign owner data')
        self.cleanup()
        self.cleanup(apply=True)
        self.assertEqual(keep.read_text(), 'foreign owner data')
        self.assertIn(str(inner), self.registrations())
        self.assertIn('locked windows', self.registrations())
