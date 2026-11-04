import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from drover import brief, launch
from drover import task_access
from tests.fakeci import git_answers
from tests.gitfix import Repo

MAIN, TOPIC = 'a' * 40, 'b' * 40
STATUS = ('status', '--porcelain=v2', '--branch', '--untracked-files=all', '--ignored=matching')


class Clones:
    """An owner's checkout with `main` and, if given, `topic`, answering the git `task_access` runs to clone a read
    checkout and to verify it. Each clone's HEAD lives in its own `.git/HEAD`, so it moves with the rename. Git is setup
    here, not the point: the history tests below clone through real git."""
    def __init__(self, source, topic=None):
        self.source = source
        source.mkdir(parents=True)
        self.refs = {'refs/heads/main': MAIN, **({'refs/heads/topic': TOPIC} if topic else {})}
        self.trees = {MAIN: {'base.txt': 'base'}, TOPIC: {'base.txt': 'base', **(topic or {})}}
        self.asked = []

    def move(self, branch, sha):
        """The owner's branch moves to a new commit with the same tree."""
        self.refs[f'refs/heads/{branch}'] = sha
        self.trees[sha] = dict(self.trees[MAIN if branch == 'main' else TOPIC])

    def __call__(self, repo, *args, timeout):
        if args[0] != '--no-optional-locks':
            raise AssertionError(f'git without --no-optional-locks: {args!r}')
        args = args[1:]
        self.asked.append((repo, args))
        here = Path(repo)

        def done(code=0, out='', err=''):
            return subprocess.CompletedProcess(args, code, out, err)
        if args[:2] == ('rev-parse', '--verify'):
            sha = self.refs.get(args[2].removesuffix('^{commit}'))
            return done(0, sha + '\n') if sha else done(128, err='fatal: Needed a single revision\n')
        if args[:3] == ('clone', '--no-checkout', '--'):
            (Path(args[4]) / '.git').mkdir(parents=True)
            return done()
        if args[:3] == ('fetch', '--no-tags', '--') or args[:2] == ('config', 'core.filemode'):
            return done()
        if args[:4] == ('-c', 'core.autocrlf=false', 'checkout', '--detach'):
            for name, text in self.trees[args[4]].items():
                (here / name).write_text(text)
            (here / '.git' / 'HEAD').write_text(args[4])
            return done()
        if args == STATUS:
            head = (here / '.git' / 'HEAD').read_text()
            tree = self.trees.get(head, {})
            lines = [f'# branch.oid {head}', '# branch.head (detached)']
            exclude = here / '.git' / 'info' / 'exclude'
            ignored = exclude.read_text().split() if exclude.exists() else []
            for path in sorted(p for p in here.iterdir() if p.name != '.git'):
                if path.name not in tree:
                    lines.append(f"{'!' if path.name in ignored else '?'} {path.name}")
                elif path.read_text() != tree[path.name]:
                    lines.append(f'1 .M N... 100644 100644 100644 {head} {head} {path.name}')
            return done(0, '\n'.join(lines) + '\n')
        raise AssertionError(f'unexpected git {args!r}')


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
            fake = Clones(Path(tmp) / 'source', topic={'topic.txt': 'topic'})
            cfg = {'p': {'repo': str(fake.source), 'default_branch': 'main'}}
            with git_answers(fake):
                reads = task_access.prepare(['p@topic'], cfg, Path(tmp) / 'reads')
                dest = Path(reads[0]['path'])
                self.assertNotEqual(dest, fake.source)
                self.assertEqual((dest / 'topic.txt').read_text(), 'topic')
                self.assertEqual({args[0] for repo, args in fake.asked if Path(repo) == fake.source},
                                 {'rev-parse', 'clone'}, "the owner's checkout is only read and cloned, never switched")
                task_access.verify(reads)
                (dest / 'extra').write_text('bad')
                with self.assertRaises(ValueError):
                    task_access.verify(reads)

    def test_verify_detects_moved_head_and_ignored_files_in_one_git_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Clones(Path(tmp) / 'source')
            with git_answers(fake):
                reads = task_access.prepare(['p'], {'p': {'repo': str(fake.source), 'default_branch': 'main'}},
                                            Path(tmp) / 'reads')
                dest = Path(reads[0]['path'])
                fake.asked.clear()
                task_access.verify(reads)
                self.assertEqual(len(fake.asked), 1, fake.asked)
                (dest / '.git' / 'info').mkdir()
                (dest / '.git' / 'info' / 'exclude').write_text('hidden\n')
                (dest / 'hidden').write_text('ignored but present')
                with self.assertRaisesRegex(ValueError, 'changed'):
                    task_access.verify(reads)
                (dest / 'hidden').unlink()
                task_access.verify(reads)
                (dest / '.git' / 'HEAD').write_text('c' * 40)     # a commit made in the read checkout
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

    def test_lines_58_63_still_fall_through_to_the_remote_ref(self):
        """A local ref that will not resolve, by exit or by timeout, falls through to the remote ref, as a ValueError did."""
        import subprocess
        from tests.fakeci import git_answers
        sha = 'a' * 40
        for local in ('exit', 'timeout'):
            with self.subTest(local=local), tempfile.TemporaryDirectory() as tmp:
                asked = []

                def reply(repo, *args, timeout):
                    asked.append(args)
                    if args[-1] == 'refs/heads/main^{commit}':
                        if local == 'timeout':
                            raise subprocess.TimeoutExpired(['git', *args], timeout)
                        return subprocess.CompletedProcess(args, 128, '', 'fatal: needed a single revision\n')
                    if args[-2:] == ('--verify', 'refs/remotes/origin/main^{commit}'):
                        return subprocess.CompletedProcess(args, 0, sha + '\n', '')
                    if args[:2] == ('--no-optional-locks', 'status'):
                        return subprocess.CompletedProcess(args, 0, f'# branch.oid {sha}\n# branch.head (detached)\n', '')
                    return subprocess.CompletedProcess(args, 0, '', '')
                (Path(tmp) / 'src').mkdir()
                with git_answers(reply):
                    reads = task_access.prepare(['p'], {'p': {'repo': str(Path(tmp) / 'src'), 'default_branch': 'main'}},
                                                Path(tmp) / 'reads')
                self.assertEqual(reads[0]['sha'], sha)
                self.assertEqual([a[-1] for a in asked[:2]], ['refs/heads/main^{commit}', 'refs/remotes/origin/main^{commit}'])

    def _fake(self, tmp, topic=False):
        fake = Clones(Path(tmp) / 'source', topic={'topic.txt': 'topic'} if topic else None)
        return fake, {'p': {'repo': str(fake.source), 'default_branch': 'main'}}, Path(tmp) / 'reads'

    @staticmethod
    def _keys(root):
        return {p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith('read-')}

    def test_prune_keeps_the_newest_two_snapshots_of_a_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake, cfg, root = self._fake(tmp)
            with git_answers(fake):
                paths = []
                for sha in ('1' * 40, '2' * 40, '3' * 40):
                    fake.move('main', sha)
                    paths.append(task_access.prepare(['p'], cfg, root)[0]['path'])
            self.assertEqual(self._keys(root), {Path(p).name for p in paths[1:]})

    def test_pinned_snapshot_survives_even_when_oldest(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake, cfg, root = self._fake(tmp)
            with git_answers(fake):
                fake.move('main', '1' * 40)
                old = task_access.prepare(['p'], cfg, root)[0]['path']
                paths = []
                for sha in ('2' * 40, '3' * 40, '4' * 40):
                    fake.move('main', sha)
                    paths.append(task_access.prepare(['p'], cfg, root, pinned=[old])[0]['path'])
            self.assertEqual(self._keys(root), {Path(p).name for p in (old, *paths[1:])})

    def test_each_branch_keeps_its_own_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake, cfg, root = self._fake(tmp, topic=True)
            kept = set()
            with git_answers(fake):
                for i, (one, other) in enumerate((('1', '7'), ('2', '8'), ('3', '9'))):
                    fake.move('main', one * 40)
                    main = task_access.prepare(['p'], cfg, root)[0]['path']
                    fake.move('topic', other * 40)
                    topic = task_access.prepare(['p@topic'], cfg, root)[0]['path']
                    if i:
                        kept |= {Path(main).name, Path(topic).name}
            self.assertEqual(len(kept), 4)
            self.assertEqual(self._keys(root), kept)

    def test_prune_removes_old_format_dirs_and_stale_temp_dirs(self):
        import os
        import time
        with tempfile.TemporaryDirectory() as tmp:
            fake, cfg, root = self._fake(tmp)
            for name in ('objects-0123', 'deadbeef', 'c' * 24, 'read-old', 'read-new'):
                (root / name / '.git').mkdir(parents=True)
                (root / name / 'file.txt').write_text('x')
            two_hours_ago = time.time() - 2 * 3600
            os.utime(root / 'read-old', (two_hours_ago, two_hours_ago))
            with git_answers(fake):
                read = task_access.prepare(['p'], cfg, root)[0]
            names = {p.name for p in root.iterdir() if p.is_dir()}
            self.assertEqual(names, {Path(read['path']).name, 'read-new'})

    def test_read_only_file_does_not_stop_removal(self):
        import os
        import stat
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            fake, cfg, root = self._fake(tmp)
            locked = root / ('d' * 24)
            (locked / 'sub').mkdir(parents=True)
            (locked / 'sub' / 'frozen.txt').write_text('x')
            os.chmod(locked / 'sub' / 'frozen.txt', stat.S_IREAD)
            with git_answers(fake):
                task_access.prepare(['p'], cfg, root)
            self.assertFalse(locked.exists())

    def test_prune_never_follows_a_link_out_of_the_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake, cfg, root = self._fake(tmp)
            outside = Path(tmp) / 'outside'
            outside.mkdir()
            (outside / 'keep.txt').write_text('owner bytes')
            root.mkdir()
            linked = 0
            try:
                (root / ('e' * 24)).symlink_to(outside, target_is_directory=True)
                linked += 1
            except OSError:
                pass
            try:
                import _winapi
                _winapi.CreateJunction(str(outside), str(root / ('f' * 24)))
                linked += 1
            except (ImportError, OSError):
                pass
            if not linked:
                self.skipTest('neither directory symlinks nor junctions available')
            with git_answers(fake):
                task_access.prepare(['p'], cfg, root)
            self.assertEqual((outside / 'keep.txt').read_text(), 'owner bytes')

    def test_reuse_returns_the_newest_snapshot_without_a_new_clone(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake, cfg, root = self._fake(tmp)
            with git_answers(fake):
                first = task_access.prepare(['p'], cfg, root)[0]
                fake.move('main', '5' * 40)
                again = task_access.prepare(['p'], cfg, root, reuse=True)[0]
                self.assertEqual(again, first)
                self.assertEqual(self._keys(root), {Path(first['path']).name})
                fresh = task_access.prepare(['p'], cfg, root)[0]
                self.assertEqual(fresh['sha'], '5' * 40)
                (root / '.index.json').unlink()       # no record: reuse falls back to a fresh prepare
                self.assertEqual(task_access.prepare(['p@main'], cfg, root, reuse=True)[0]['path'], fresh['path'])

    def test_open_run_checkout_still_verifies_after_its_branch_moves_on(self):
        """Finish verifies the manifest an open run recorded; the open runs' paths are what the dispatcher pins."""
        from drover.ledger import connect, ops
        from drover.port import DirectPort
        with tempfile.TemporaryDirectory() as tmp:
            fake, cfg, root = self._fake(tmp)
            conn = connect(Path(tmp) / 'l.db')
            try:
                port = DirectPort(conn)
                tid = ops.create_task(conn, 't', 'p')['id']
                run = port.start_run(tid, 'worker', 'claude')
                with git_answers(fake):
                    fake.move('main', '1' * 40)
                    recorded = task_access.prepare(['p'], cfg, root, pinned=port.open_read_checkouts())
                    port.append_event('dispatch.access', {'run': run, 'read_checkouts': recorded, 'write': True}, tid)
                    self.assertEqual(port.open_read_checkouts(), [recorded[0]['path']])
                    for sha in ('2' * 40, '3' * 40, '4' * 40):
                        fake.move('main', sha)
                        task_access.prepare(['p'], cfg, root, pinned=port.open_read_checkouts())
                    task_access.verify(recorded)
                    port.finish_run(run, 'done')
                    self.assertEqual(port.open_read_checkouts(), [])
                    fake.move('main', '6' * 40)
                    task_access.prepare(['p'], cfg, root, pinned=port.open_read_checkouts())
                    self.assertFalse(Path(recorded[0]['path']).exists())
            finally:
                conn.close()

    def test_replan_inspection_reuses_the_branch_snapshot_after_the_branch_moves(self):
        from types import SimpleNamespace
        from drover import config
        from drover.replan import Replanning
        repo = Repo()
        self.addCleanup(repo.cleanup)
        with tempfile.TemporaryDirectory() as tmp:
            original = config.get()
            config.use(config.validate({'paths': {'state_dir': str(Path(tmp) / 'state')}}))
            self.addCleanup(config.use, original)
            prog = {'name': 'p', 'repo': str(repo.repo), 'default_branch': 'main'}
            planning = Replanning(SimpleNamespace(port=SimpleNamespace(open_read_checkouts=list), repo=lambda task: prog, all_repos=lambda: {'p': prog}))
            task = {'id': 'T-1', 'worktree': None, 'branch': None, 'read_access': None}
            root = Path(tmp) / 'state' / 'read-checkouts'
            first = planning._inputs(task)
            before = self._keys(root)
            repo.commit('moved.txt', 'moved')
            for _ in range(2):
                self.assertEqual(planning._inputs(task)['head'], first['head'])
                self.assertEqual(self._keys(root), before)
            fresh = planning._inputs(task, fresh=True)
            self.assertNotEqual(fresh['head'], first['head'])
            self.assertEqual(len(self._keys(root)), len(before) + 1)

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

    def test_admission_pins_the_read_checkouts_of_open_runs(self):
        self.task()
        self.d.tick()
        recorded = self.fl.of('worker')[-1]['read_dirs']
        self.assertTrue(recorded)
        self.task()
        self.pinned_reads = None
        self.d.tick()
        self.assertEqual(len(self.fl.of('worker')), 2, 'the second task was admitted')
        self.assertTrue(set(recorded) <= set(self.pinned_reads or ()), self.pinned_reads)
