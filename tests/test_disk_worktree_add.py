"""Creation APIs tag real linked checkouts before reporting success."""
import datetime
import io
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import time
from unittest.mock import patch

from tests.disk_fixture import DiskCase
from tests.gitfix import Repo


class Add(DiskCase):
    def test_cli_and_all_creators_tag_with_lease(self):
        from drover import worktree, ownership, pipeline, landing, repos
        from drover.ledger import connect, ops
        from drover.port import DirectPort
        fixture = Repo()
        self.addCleanup(fixture.cleanup)
        repo = fixture.repo
        until = time.time() + 86400
        for owner in ('lane:alpha', 'session literal with spaces'):
            path = fixture.root / ('checkout ' + owner.replace(':', '-'))
            fixture.git('branch', 'topic-' + str(len(owner)))
            value = worktree.add(str(repo), str(path), 'topic-' + str(len(owner)), owner=owner, lease_until=until)
            self.assertEqual(Path(value), path)
            tag = ownership.read(str(path))
            self.assertEqual(tag['v'], 2)
            self.assertEqual(tag['owner'], owner)
            self.assertEqual(tag['lease_until'], until)
            self.assertFalse((path / 'drover-tag.json').exists())
        cli_path = fixture.root / 'cli checkout'
        fixture.git('branch', 'cli-topic')
        iso = datetime.datetime.fromtimestamp(until, datetime.timezone.utc).isoformat()
        with redirect_stdout(io.StringIO()):
            status = worktree.main(['add', str(cli_path), 'cli-topic', '--repo', str(repo),
                                    '--owner', 'lane:cli', '--lease-until', iso])
        self.assertEqual(status, 0)
        self.assertEqual(ownership.read(str(cli_path))['owner'], 'lane:cli')
        conn = connect(self.root / 'creation.db')
        self.addCleanup(conn.close)
        prog = repos.normalize({'name': 'fixture', 'repo': str(repo), 'worktrees': str(fixture.root / 'tasks'),
                                'scripts': {'test': '.drover/scripts/test.ps1'}, 'gate': {'platform': 'wsl'}})
        task = ops.create_task(conn, 'isolated task', 'fixture')
        pipe = pipeline.Pipeline(DirectPort(conn), repos_map={'fixture': prog}, runs_dir=self.root / 'runs')
        before = time.time()
        task_path = pipe.worktree(task)
        tag = ownership.read(task_path)
        self.assertEqual(tag['owner'], 'drover:' + task['id'])
        self.assertGreaterEqual(tag['lease_until'], before + 86400 - 1)
        group = fixture.root / 'detached merge group'
        base = fixture.git('rev-parse', 'main')
        tip, applied, conflicts = landing.build_tip(str(repo), str(group), base, [])
        self.assertEqual(tip, base)
        self.assertEqual((applied, conflicts), ([], []))
        tag = ownership.read(str(group))
        self.assertTrue(tag['owner'])
        self.assertGreaterEqual(tag['lease_until'], before + 86400 - 1)
        self.assertEqual(fixture.git('rev-parse', '--abbrev-ref', 'HEAD', cwd=group), 'HEAD')
        # Resumed acquisition renews a lease rather than inheriting an expired one.
        ownership.tag(str(task_path), 'fixture', 'drover:' + task['id'], lease_until=time.time() - 1)
        pipe.worktree(ops.get_task(conn, task['id']))
        self.assertGreater(ownership.read(task_path)['lease_until'], time.time())

    def test_tag_failure_refuses_success_and_verbatim_paths(self):
        from drover import worktree, ownership
        fixture = Repo()
        self.addCleanup(fixture.cleanup)
        path = fixture.root / ("spaces 'quotes' and literal" if os.name == 'nt' else 'spaces \'quotes" and\nnewline')
        fixture.git('branch', 'exact-topic')
        with patch.object(ownership, 'tag', side_effect=OSError('fixture tag write denied')):
            with self.assertRaises((OSError, RuntimeError)) as caught:
                worktree.add(str(fixture.repo), str(path), 'exact-topic', owner='session:literal',
                             lease_until=time.time() + 86400)
        self.assertIn(str(path), str(caught.exception))
        self.assertTrue((path / '.git').is_file(), 'tag failure leaves a visible quarantinable checkout')
        self.assertEqual(fixture.git('rev-parse', 'HEAD', cwd=path), fixture.git('rev-parse', 'exact-topic'))
        for owner, lease in (('', '2099-01-01T00:00:00Z'), ('valid', 'not-a-time')):
            absent = fixture.root / ('invalid-' + str(len(owner)))
            with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                status = worktree.main(['add', str(absent), 'main', '--repo', str(fixture.repo),
                                        '--owner', owner, '--lease-until', lease])
            self.assertEqual(status, 2)
            self.assertFalse(absent.exists())


from tests.test_landing import LandingCase


class MergeGroupCleanup(LandingCase):
    def test_completed_group_removes_owned_side_locked_checkout(self):
        self.task('completed-topic', {'finished.txt': 'done'})
        self.land()
        self.assertFalse(self.work.exists())
        self.assertNotIn(str(self.work), self.r.git('worktree', 'list', '--porcelain'))


class MarkerRelease(DiskCase):
    def test_windows_path_spelling_releases_only_own_side_marker(self):
        import subprocess
        from drover import placement
        for marker, releases in (('windows', True), ('owner reviewing', False), ('wsl', False)):
            called = []
            def git(repo, *args, **kwargs):
                called.append(args)
                return subprocess.CompletedProcess(args, 0,
                    'worktree C:/Temp/Group\x00HEAD abc\x00locked ' + marker + '\x00\x00', '')
            with patch.object(placement, 'IS_WINDOWS', True) as _placement_patch_1, patch('drover.placement_exec.IS_WINDOWS', _placement_patch_1), patch('drover.placement_git.IS_WINDOWS', _placement_patch_1), patch('drover.placement_host.IS_WINDOWS', _placement_patch_1), patch('drover.placement_paths.IS_WINDOWS', _placement_patch_1), patch('drover.placement_process.IS_WINDOWS', _placement_patch_1), patch('drover.placement_proof.IS_WINDOWS', _placement_patch_1), patch('drover.placement_tree.IS_WINDOWS', _placement_patch_1), patch('drover.placement_worker.IS_WINDOWS', _placement_patch_1), patch.object(placement, 'git', side_effect=git) as _placement_patch_2, patch('drover.placement_git.git', _placement_patch_2):
                placement.unlock_worktree('C:/repo', r'c:\temp\group')
            self.assertEqual(any(args[:2] == ('worktree', 'unlock') for args in called), releases)


class RelativePaths(DiskCase):
    def test_relative_checkout_is_created_and_tagged_at_callers_path(self):
        from contextlib import chdir
        from drover import worktree, ownership
        fixture = Repo()
        self.addCleanup(fixture.cleanup)
        fixture.git('branch', 'relative-topic')
        with chdir(fixture.root):
            path = worktree.add(str(fixture.repo), 'relative checkout', 'relative-topic',
                                owner='session:relative', lease_until=time.time() + 86400)
        expected = fixture.root / 'relative checkout'
        self.assertEqual(Path(path), expected)
        self.assertEqual(ownership.read(str(expected))['owner'], 'session:relative')
        self.assertFalse((fixture.repo / 'relative checkout').exists())
