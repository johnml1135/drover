"""Placement policy for WSL workers whose gates can run on Windows."""
import json
from pathlib import Path
from unittest.mock import patch

from drover import pipeline, placement, repos, runner
from drover.ledger import ops
from tests.test_pipeline import PolicyBase


def policy(platform):
    return (f'schema_version = 1\n[scripts]\ntest = ".drover/scripts/drover/test.ps1"\n'
            f'[gate]\nplatform = "{platform}"\n')


class SharedWorktrees(PolicyBase):
    def set_gate(self, platform):
        self.fix.commit('.drover/config.toml', policy(platform))
        self.pipe._repos['p'] = {'name': 'p', 'repo': str(self.fix.repo), 'worktrees': str(Path(self.tmp.name) / 'wts')}

    def test_committed_windows_gates_use_shared_root_and_gate_plan(self):
        for platform in ('windows', 'both'):
            with self.subTest(platform=platform):
                self.set_gate(platform)
                tid = self.task()
                wt = self.pipe.worktree(self.get(tid), 'wsl')
                self.assertEqual(Path(wt).parent, Path(self.tmp.name) / 'wts')
                sha = self.fix.head(cwd=wt)
                run_id = 'native-' + platform
                result = self.pipe.ci.prepare(self.pipe.repo(self.get(tid)), wt, sha, run_id, platform='windows')
                plan = json.loads((runner.run_dir(self.runs, run_id) / 'plan.json').read_text())
                self.assertEqual(result['spec']['placement'], 'windows')
                self.assertEqual(plan['repo'], placement.ci_path('windows', wt))
                self.assertTrue(Path(plan['repo']).is_dir())

    def test_wsl_gate_creates_and_reuses_ext4(self):
        self.set_gate('wsl')
        ext4 = Path(self.tmp.name) / 'ext4'
        with patch.object(placement, 'wsl_worktrees_root', return_value=str(ext4)):
            tid = self.task()
            wt = self.pipe.worktree(self.get(tid), 'wsl')
            with patch.object(placement, 'drive', return_value=None):
                self.assertEqual(self.pipe.worktree(self.get(tid), 'wsl'), wt)
        self.assertEqual(Path(wt).parent, ext4 / 'p.worktrees')

    def test_dirty_config_does_not_override_committed_policy(self):
        self.set_gate('both')
        config = self.fix.repo / '.drover/config.toml'
        config.write_text(policy('wsl'))
        wt = self.pipe.worktree(self.get(self.task()), 'wsl')
        self.assertEqual(Path(wt).parent, Path(self.tmp.name) / 'wts')
        self.assertEqual(config.read_text(), policy('wsl'))

    def test_fallback_reads_source_branch_policy(self):
        self.set_gate('wsl')
        self.fix.branch('feat', {'.drover/config.toml': policy('both')}, base='main')
        foreign = Path(self.tmp.name) / 'foreign'
        self.fix.checkout(foreign, 'feat')
        (foreign / 'scratch').write_text('keep')
        tid = self.task(branch='feat')
        wt = self.pipe.worktree(self.get(tid), 'wsl')
        self.assertEqual(Path(wt).parent, Path(self.tmp.name) / 'wts')
        self.assertEqual(self.fix.head(cwd=wt), self.fix.head('feat'))
        self.assertEqual((foreign / 'scratch').read_text(), 'keep')
        self.assertTrue(ops.events(self.conn, task_id=tid, kind='worktree-fallback'))

    def test_bad_base_config_does_not_block_windows_creation(self):
        self.set_gate('wsl')
        tid = self.task()
        prog = self.pipe.repo(self.get(tid))
        self.fix.branch('bad', {'.drover/config.toml': 'invalid = ['}, base='main')
        with patch.object(self.repository, 'load', side_effect=AssertionError('policy was loaded')):
            wt = self.pipe._create(self.get(tid), prog, str(self.fix.repo), None, source='bad', where='windows')
        self.assertEqual(self.fix.head(cwd=wt), self.fix.head('bad'))

    def test_non_shared_root_refused_before_creation(self):
        self.set_gate('both')
        tid = self.task()
        with patch.object(placement, 'drive', return_value=None):
            with self.assertRaisesRegex(pipeline.WorktreeRefused, 'worktrees root .* is not reachable from Windows'):
                self.pipe.worktree(self.get(tid), 'wsl')
        self.assertIsNone(self.get(tid)['worktree'])

    def test_worktree_created_under_shared_root_is_reused(self):
        self.set_gate('both')
        (Path(self.tmp.name) / 'alias').mkdir()
        self.pipe._repos['p']['worktrees'] = str(Path(self.tmp.name) / 'alias' / '..' / 'wts')
        tid = self.task()
        wt = self.pipe.worktree(self.get(tid), 'windows')
        with patch.object(placement, 'drive', return_value=None):
            self.assertEqual(self.pipe.worktree(self.get(tid), 'wsl'), wt)
        self.assertEqual(self.get(tid)['worktree'], wt)
        self.assertFalse(ops.events(self.conn, task_id=tid, kind='worktree-replaced'))

    def test_shared_root_symlink_resolves_to_created_worktree(self):
        self.set_gate('both')
        root = Path(self.tmp.name)
        target, alias = root / 'actual-wts', root / 'linked-wts'
        target.mkdir()
        try:
            alias.symlink_to(target, target_is_directory=True)
        except OSError as error:
            self.skipTest(f'symlinks unavailable: {error}')
        self.pipe._repos['p']['worktrees'] = str(alias)
        tid = self.task()
        wt = self.pipe.worktree(self.get(tid), 'windows')
        with patch.object(placement, 'drive', return_value=None):
            self.assertEqual(self.pipe.worktree(self.get(tid), 'wsl'), wt)
        self.assertEqual(Path(wt).parent, target)

    def test_clean_existing_ext4_moves_same_branch_and_records_event(self):
        self.set_gate('wsl')
        tid = self.task()
        wt = self.pipe.worktree(self.get(tid), 'wsl')
        self.fix.commit('.drover/config.toml', policy('both'), cwd=wt)
        sha, branch = self.fix.head(cwd=wt), self.get(tid)['branch']
        original_drive = placement.drive
        with patch.object(placement, 'drive', side_effect=lambda p: None if str(p) == wt else original_drive(p)):
            moved = self.pipe.worktree(self.get(tid), 'wsl')
        self.assertFalse(Path(wt).exists())
        self.assertEqual(Path(moved).parent, Path(self.tmp.name) / 'wts')
        self.assertEqual(self.get(tid)['branch'], branch)
        self.assertEqual(self.fix.head(cwd=moved), sha)
        self.assertEqual(ops.events(self.conn, task_id=tid, kind='worktree-replaced')[-1]['data']['path'], wt)

    def test_dirty_existing_ext4_is_never_touched_even_when_owned(self):
        self.set_gate('both')
        tid = self.task()
        self.fix.branch('topic', {'.drover/config.toml': policy('both')}, base='main')
        wt = str(Path(self.tmp.name) / 'ext4' / 'p.worktrees' / 'topic')
        self.fix.checkout(wt, 'topic')
        self.repository.tag(wt, 'p', tid)
        ops.update_task(self.conn, tid, branch='topic', worktree=wt)
        scratch = Path(wt) / 'scratch'
        scratch.write_text('keep')
        with patch.object(placement, 'drive', return_value=None):
            with self.assertRaises(pipeline.WorktreeRefused) as refusal:
                self.pipe.worktree(self.get(tid), 'wsl')
        self.assertEqual(str(refusal.exception), f'worktree {wt} has uncommitted changes and must move to the shared root for the Windows gate')
        self.assertEqual(scratch.read_text(), 'keep')
        self.assertEqual(self.get(tid)['worktree'], wt)
        self.assertFalse(ops.events(self.conn, task_id=tid, kind='worktree-replaced'))

    def test_checkout_outside_drover_root_is_not_removed(self):
        self.set_gate('wsl')
        self.fix.branch('topic', {'.drover/config.toml': policy('both'), 'work': 'committed'}, base='main')
        wt = str(Path(self.tmp.name) / 'wts-other' / 'topic')
        self.fix.checkout(wt, 'topic')
        tid = self.task(branch='topic')
        original_drive = placement.drive
        with patch.object(placement, 'drive', side_effect=lambda p: None if str(p) == wt else original_drive(p)):
            with self.assertRaises(pipeline.WorktreeRefused) as refusal:
                self.pipe.worktree(self.get(tid), 'wsl')
        self.assertEqual(str(refusal.exception), f"branch topic is checked out at {wt}, outside Drover's worktrees; move or remove it")
        self.assertEqual((Path(wt) / 'work').read_text(), 'committed')
        self.assertFalse(ops.events(self.conn, task_id=tid, kind='worktree-replaced'))

    def test_clean_discovered_checkout_under_root_moves(self):
        self.set_gate('wsl')
        self.fix.branch('topic', {'.drover/config.toml': policy('both'), 'work': 'committed'}, base='main')
        wt = str(Path(self.tmp.name) / 'ext4' / 'p.worktrees' / 'topic')
        self.fix.checkout(wt, 'topic')
        tid = self.task(branch='topic')
        original_drive = placement.drive
        with patch.object(placement, 'drive', side_effect=lambda p: None if str(p) == wt else original_drive(p)):
            moved = self.pipe.worktree(self.get(tid), 'wsl')
            self.assertEqual(self.pipe.worktree(self.get(tid), 'wsl'), moved)
        self.assertFalse(Path(wt).exists())
        self.assertEqual((Path(moved) / 'work').read_text(), 'committed')
        self.assertEqual(self.get(tid)['branch'], 'topic')
        self.assertEqual(len(ops.events(self.conn, task_id=tid, kind='worktree-replaced')), 1)

    def test_branch_mismatch_is_refused_without_removing_checkout(self):
        self.set_gate('both')
        self.fix.branch('actual', {'.drover/config.toml': policy('both')}, base='main')
        wt = str(Path(self.tmp.name) / 'ext4' / 'p.worktrees' / 'topic')
        self.fix.checkout(wt, 'actual')
        tid = self.task(branch='expected')
        ops.update_task(self.conn, tid, worktree=wt)
        with patch.object(placement, 'drive', return_value=None):
            with self.assertRaisesRegex(pipeline.WorktreeRefused, 'not the task.s own branch'):
                self.pipe.worktree(self.get(tid), 'wsl')
        self.assertTrue(Path(wt).exists())
        self.assertFalse(ops.events(self.conn, task_id=tid, kind='worktree-replaced'))

    def test_destination_conflict_preserves_old_worktree(self):
        self.set_gate('both')
        self.fix.branch('topic', {'.drover/config.toml': policy('both')}, base='main')
        wt = str(Path(self.tmp.name) / 'ext4' / 'p.worktrees' / 'topic')
        self.fix.checkout(wt, 'topic')
        tid = self.task(branch='topic')
        ops.update_task(self.conn, tid, worktree=wt)
        dest = Path(self.tmp.name) / 'wts' / f'drover-{tid}'
        self.fix.branch('other', {}, base='main')
        self.fix.checkout(dest, 'other')
        (dest / 'owner-work').write_text('keep')
        original_drive = placement.drive
        with patch.object(placement, 'drive', side_effect=lambda p: None if str(p) == wt else original_drive(p)):
            with self.assertRaisesRegex(pipeline.WorktreeRefused, 'exists and is not this task.s worktree'):
                self.pipe.worktree(self.get(tid), 'wsl')
        self.assertTrue(Path(wt).exists())
        self.assertEqual((dest / 'owner-work').read_text(), 'keep')
        self.assertFalse(ops.events(self.conn, task_id=tid, kind='worktree-replaced'))

    def test_sync_failure_preserves_old_worktree(self):
        self.set_gate('both')
        self.fix.branch('topic', {'.drover/config.toml': policy('both')}, base='main')
        wt = str(Path(self.tmp.name) / 'ext4' / 'p.worktrees' / 'topic')
        self.fix.checkout(wt, 'topic')
        tid = self.task(branch='topic')
        ops.update_task(self.conn, tid, worktree=wt)
        self.fix.online = False
        original_drive = placement.drive
        with patch.object(placement, 'drive', side_effect=lambda p: None if str(p) == wt else original_drive(p)):
            self.assertFalse(self.pipe.worktree(self.get(tid), 'wsl'))
        self.assertTrue(Path(wt).exists())
        self.assertEqual(self.get(tid)['worktree'], wt)
        self.assertFalse(ops.events(self.conn, task_id=tid, kind='worktree-replaced'))

    def test_owned_destination_on_same_branch_is_reused(self):
        self.set_gate('both')
        self.fix.branch('topic', {'.drover/config.toml': policy('both')}, base='main')
        wt = str(Path(self.tmp.name) / 'ext4' / 'p.worktrees' / 'topic')
        self.fix.checkout(wt, 'topic')
        tid = self.task(branch='topic')
        ops.update_task(self.conn, tid, worktree=wt)
        dest = str(Path(self.tmp.name) / 'wts' / f'drover-{tid}')
        self.fix.checkout(dest, 'topic')
        self.repository.tag(dest, 'p', tid)
        original_drive = placement.drive
        with patch.object(placement, 'drive', side_effect=lambda p: None if str(p) == wt else original_drive(p)):
            self.assertEqual(self.pipe.worktree(self.get(tid), 'wsl'), dest)
        self.assertFalse(Path(wt).exists())
        self.assertEqual(self.get(tid)['worktree'], dest)
        self.assertEqual(len(ops.events(self.conn, task_id=tid, kind='worktree-replaced')), 1)

    def test_pending_sync_wait_preserves_old_worktree(self):
        self.set_gate('both')
        self.fix.branch('topic', {'.drover/config.toml': policy('both')}, base='main')
        wt = str(Path(self.tmp.name) / 'ext4' / 'p.worktrees' / 'topic')
        self.fix.checkout(wt, 'topic')
        tid = self.task(branch='topic')
        ops.update_task(self.conn, tid, worktree=wt)
        original_drive = placement.drive
        with patch.object(placement, 'drive', side_effect=lambda p: None if str(p) == wt else original_drive(p)), \
                patch.object(self.port.waiting, 'state', return_value='open'), \
                patch.object(self.port.waiting, 'consume_retry', return_value=False), \
                patch.object(self.port.waiting, 'wait') as wait:
            self.assertFalse(self.pipe.worktree(self.get(tid), 'wsl'))
        wait.assert_called_once()
        self.assertTrue(Path(wt).exists())
        self.assertEqual(self.get(tid)['worktree'], wt)
        self.assertFalse(ops.events(self.conn, task_id=tid, kind='worktree-replaced'))

    def test_creation_failure_after_removal_preserves_branch_for_retry(self):
        self.set_gate('both')
        self.fix.branch('topic', {'.drover/config.toml': policy('both')}, base='main')
        wt = str(Path(self.tmp.name) / 'ext4' / 'p.worktrees' / 'topic')
        self.fix.checkout(wt, 'topic')
        tid = self.task(branch='topic')
        ops.update_task(self.conn, tid, worktree=wt)
        original_drive = placement.drive
        with patch.object(placement, 'drive', side_effect=lambda p: None if str(p) == wt else original_drive(p)):
            with patch.object(self.repository, 'add', side_effect=pipeline.WorktreeRefused('add failed')):
                with self.assertRaisesRegex(pipeline.WorktreeRefused, 'branch topic.*add failed'):
                    self.pipe.worktree(self.get(tid), 'wsl')
            self.assertFalse(Path(wt).exists())
            self.assertIsNone(self.get(tid)['worktree'])
            self.assertFalse(ops.events(self.conn, task_id=tid, kind='worktree-replaced'))
            moved = self.pipe.worktree(self.get(tid), 'wsl')
        self.assertEqual(self.get(tid)['branch'], 'topic')
        self.assertEqual(self.repository.branch(moved), 'topic')

    def test_tag_failure_after_removal_sets_needs_owner_with_branch(self):
        self.set_gate('both')
        self.fix.branch('topic', {'.drover/config.toml': policy('both')}, base='main')
        wt = str(Path(self.tmp.name) / 'ext4' / 'p.worktrees' / 'topic')
        self.fix.checkout(wt, 'topic')
        tid = self.task(branch='topic')
        ops.update_task(self.conn, tid, worktree=wt)
        original_drive = placement.drive
        with patch.object(placement, 'drive', side_effect=lambda p: None if str(p) == wt else original_drive(p)), \
                patch.object(self.repository, 'tag', side_effect=OSError('tag failed')):
            self.assertFalse(self.d._worktree(self.get(tid), 'wsl'))
        self.assertEqual(self.status(tid), 'needs-owner')
        self.assertIn('branch topic', self.states(tid)[-1][1])
        self.assertIn('tag failed', self.states(tid)[-1][1])
        self.assertIsNone(self.get(tid)['worktree'])
        self.assertEqual(self.get(tid)['branch'], 'topic')
        self.assertFalse(ops.events(self.conn, task_id=tid, kind='worktree-replaced'))

    def test_non_shared_root_preserves_clean_existing_checkout(self):
        self.set_gate('both')
        tid = self.task()
        self.fix.branch('topic', {'.drover/config.toml': policy('both')}, base='main')
        wt = str(Path(self.tmp.name) / 'ext4' / 'p.worktrees' / 'topic')
        self.fix.checkout(wt, 'topic')
        ops.update_task(self.conn, tid, branch='topic', worktree=wt)
        with patch.object(placement, 'drive', return_value=None):
            with self.assertRaisesRegex(pipeline.WorktreeRefused, 'worktrees root .* is not reachable from Windows'):
                self.pipe.worktree(self.get(tid), 'wsl')
        self.assertTrue(Path(wt).exists())
        self.assertEqual(self.get(tid)['worktree'], wt)
