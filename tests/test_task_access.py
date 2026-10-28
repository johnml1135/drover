import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from drover import brief, launch
from drover import task_access
from tests.gitfix import Repo


class AccessTests(unittest.TestCase):
    def test_codex_read_only_and_extra_dirs(self):
        spec = dict(backend='codex', model='codex', cwd='H:\\work', placement='wsl',
                    system_append='', user_prompt='', write=False, read_dirs=['H:\\read'])
        argv, _ = launch.build_argv(spec)
        self.assertEqual(argv[argv.index('-s') + 1], 'read-only')
        self.assertNotIn('--add-dir', argv)

    def test_codex_read_inputs_never_become_writable_roots(self):
        for placement in ('windows', 'wsl'):
            with self.subTest(placement=placement):
                spec = dict(backend='codex', model='codex', cwd='H:\\work', placement=placement,
                            system_append='', user_prompt='', write=True, read_dirs=['H:\\reads\\p'])
                argv, _ = launch.build_argv(spec)
                self.assertEqual(argv[argv.index('-s') + 1], 'workspace-write')
                self.assertNotIn('--add-dir', argv)
                roots = []
                self.assertIn(f'sandbox_workspace_write.writable_roots={json.dumps(roots)}', argv)
                self.assertIn('sandbox_workspace_write.exclude_slash_tmp=true', argv)
                self.assertIn('sandbox_workspace_write.exclude_tmpdir_env_var=true', argv)
                self.assertIn('approval_policy="never"', argv)

    def test_codex_refuses_read_snapshot_inside_writable_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = dict(backend='codex', model='codex', cwd=tmp, placement='wsl',
                        system_append='', user_prompt='', write=True, read_dirs=[str(Path(tmp) / 'read')])
            with self.assertRaisesRegex(ValueError, 'inside.*writable'):
                launch.build_argv(spec)
            # A read-only worker may use its first snapshot as cwd.
            launch.build_argv(dict(spec, write=False, read_dirs=[tmp]))

    def test_read_root_checks_windows_case_and_symlinks(self):
        with self.assertRaisesRegex(ValueError, 'writable'):
            task_access.check_read_roots('H:\\WORK', ['h:\\work\\read'])
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / 'workspace'
            workspace.mkdir()
            alias = Path(tmp) / 'alias'
            try:
                alias.symlink_to(workspace, target_is_directory=True)
            except OSError:
                self.skipTest('directory symlinks unavailable')
            with self.assertRaisesRegex(ValueError, 'writable'):
                task_access.check_read_roots(workspace, [alias])

    def test_scratch_cannot_overlap_a_read_snapshot(self):
        spec = dict(backend='codex', model='codex', cwd='H:\\work', placement='wsl',
                    system_append='', user_prompt='', scratch_dir='H:\\reads\\p\\temp',
                    read_dirs=['H:\\reads\\p'])
        with self.assertRaisesRegex(ValueError, 'writable'):
            launch.build_argv(spec)

    def test_wsl_native_scratch_cannot_overlap_a_read_snapshot(self):
        scratch = '/tmp/drover-scratch/7-0123456789abcdef'
        for read in (scratch, '/tmp/drover-scratch', scratch + '/read'):
            with self.subTest(read=read):
                spec = dict(backend='codex', model='codex', cwd='H:\\work', placement='wsl',
                            system_append='', user_prompt='', scratch_dir=scratch, read_dirs=[read])
                with self.assertRaisesRegex(ValueError, 'writable'):
                    launch.build_argv(spec)

    def test_isolated_branch_read_and_change_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo()
            self.addCleanup(repo.cleanup)
            repo.branch('topic', {'topic.txt': 'topic'})
            cfg = {'p': {'repo': str(repo.repo), 'default_branch': 'main'}}
            reads = task_access.prepare(['p@topic'], cfg, Path(tmp) / 'reads')
            dest = Path(reads[0]['path'])
            self.assertNotEqual(dest, repo.repo)
            self.assertEqual((dest / 'topic.txt').read_text(), 'topic')
            self.assertEqual(repo.git('branch', '--show-current'), 'main')
            task_access.verify(reads)
            (dest / 'extra').write_text('bad')
            with self.assertRaises(ValueError):
                task_access.verify(reads)

    def test_verify_detects_moved_head_and_ignored_files_in_one_git_call(self):
        repo = Repo()
        self.addCleanup(repo.cleanup)
        with tempfile.TemporaryDirectory() as tmp:
            reads = task_access.prepare(['p'], {'p': {'repo': str(repo.repo), 'default_branch': 'main'}}, tmp)
            dest = Path(reads[0]['path'])
            real, calls = task_access.placement.git, []
            def counting(path, *args, **kw):
                calls.append(args)
                return real(path, *args, **kw)
            with patch.object(task_access.placement, 'git', counting):
                task_access.verify(reads)
            self.assertEqual(len(calls), 1, calls)
            (dest / '.git' / 'info' / 'exclude').write_text('hidden\n')
            (dest / 'hidden').write_text('ignored but present')
            with self.assertRaisesRegex(ValueError, 'changed'):
                task_access.verify(reads)
            (dest / 'hidden').unlink()
            task_access.verify(reads)
            repo.git('commit', '--allow-empty', '-m', 'later', cwd=dest)
            with self.assertRaisesRegex(ValueError, 'changed'):
                task_access.verify(reads)

    def test_remote_tracking_only_read_keeps_history(self):
        repo = Repo()
        self.addCleanup(repo.cleanup)
        repo.git('checkout', '-b', 'remote-only')
        previous = repo.commit('history.txt', 'previous')
        sha = repo.commit('history.txt', 'current')
        repo.git('update-ref', 'refs/remotes/origin/remote-only', sha)
        repo.git('checkout', 'main')
        repo.git('branch', '-D', 'remote-only')
        with tempfile.TemporaryDirectory() as tmp:
            reads = task_access.prepare(['p@remote-only'],
                                        {'p': {'repo': str(repo.repo), 'default_branch': 'main'}}, tmp)
            repo.repo.rename(repo.repo.with_name('offline'))
            self.assertEqual(reads[0]['sha'], sha)
            self.assertEqual(repo.git('show', previous + ':history.txt', cwd=reads[0]['path']), 'previous')
            task_access.verify(reads)

    def test_history_is_available_without_fetching_into_the_read_snapshot(self):
        repo = Repo()
        self.addCleanup(repo.cleanup)
        previous = repo.commit('versioned.txt', 'old contents\n')
        repo.commit('versioned.txt', 'current contents\n')
        with tempfile.TemporaryDirectory() as tmp:
            reads = task_access.prepare(['p'], {'p': {'repo': str(repo.repo), 'default_branch': 'main'}}, tmp)
            # Read-only workers cannot fetch missing blobs into this checkout.
            # Taking the fixture source offline also detects an attempted lazy fetch on every host.
            repo.repo.rename(repo.repo.with_name('offline'))
            self.assertEqual(repo.git('show', previous + ':versioned.txt', cwd=reads[0]['path']), 'old contents')
            self.assertIn('current contents', repo.git('blame', 'versioned.txt', cwd=reads[0]['path']))
            task_access.verify(reads)

    def test_brief_states_access_and_kind(self):
        b = brief.assemble(dict(id='T-a', title='Read', kind='research', write=False,
                               read_access=['p@main'], read_dirs=['/reads/p']))['user_prompt']
        self.assertIn('Type: research', b)
        self.assertIn('read-only', b)
        self.assertIn('p@main', b)
        self.assertIn('/reads/p', b)
        self.assertNotIn('leave changes in the worktree', b)
        self.assertIn('read checkout HEAD', b)


from tests.rcw_fixture import PipelineBase
from tests.test_pipeline import result, done


class WorkerAccessTests(PipelineBase):
    def test_read_task_has_no_task_worktree_and_does_not_commit_or_gate(self):
        tid = self.task(kind='research')
        with patch.object(self.pipe, 'worktree', side_effect=AssertionError('must not create worktree')):
            self.assertIn(tid, self.d.tick()['launched'])
        spec = self.fl.of('worker')[-1]
        self.assertFalse(spec['write'])
        self.assertEqual(spec['sandbox'], 'read-only')
        self.assertIsNone(self.get(tid)['worktree'])
        self.assertIn('Read directories:', spec['user_prompt'])
        self.assertTrue(spec['read_dirs'])
        self.fl.handles[spec['run_id']].result = result('done', done('a' * 40))
        with patch.object(self.pipe, 'commit_ready', side_effect=AssertionError('no commit')):
            self.d.reap()
        self.assertEqual(self.get(tid)['status'], 'needs-owner')
        self.assertEqual(self.port.list_runs(tid)[-1]['outcome'], 'read-only-done')
        self.assertEqual(self.fl.of('gate'), [])

    def test_read_diff_blocks_write_task_before_commit(self):
        tid = self.task()
        self.d.tick()
        spec = self.fl.of('worker')[-1]
        path = Path(spec['read_dirs'][0])
        (path / 'extra').write_text('changed read input')
        self.fl.handles[spec['run_id']].result = result('done', done('a' * 40))
        with patch.object(self.pipe, 'commit_ready', side_effect=AssertionError('no commit')):
            self.d.reap()
        self.assertEqual(self.get(tid)['status'], 'needs-owner')
        self.assertEqual(self.port.list_runs(tid)[-1]['outcome'], 'blocked')
        self.assertIn('read-only checkout changed', self.port.list_runs(tid)[-1]['note'])

    def test_committer_refuses_read_only_task(self):
        from drover.pipeline import CommitRefused
        with self.assertRaisesRegex(CommitRefused, 'read-only'):
            self.pipe.commit_ready(dict(write=False), 1, 'bad')

    def test_restart_restores_read_manifest_and_still_refuses_changes(self):
        tid = self.task()
        self.d.tick()
        spec = self.fl.of('worker')[-1]
        rid = spec['run_id']
        restarted = self.make()
        with patch.object(self.fl, 'adopt', create=True, return_value=self.fl.handles[rid]):
            restarted._adopt(self.get(tid), self.port.list_runs(tid)[-1], 'windows', self.port.get_policy())
        adopted = restarted.live[rid][2]
        self.assertEqual(adopted['read_checkouts'], spec['read_checkouts'])
        path = Path(spec['read_dirs'][0])
        (path / 'base.txt').write_text('bad after restart')
        self.fl.handles[rid].result = result('done', done('a' * 40))
        restarted.reap()
        self.assertEqual(self.port.list_runs(tid)[-1]['outcome'], 'blocked')
        self.assertEqual(self.get(tid)['status'], 'needs-owner')
