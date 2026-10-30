"""Placement's public interface, including composed commands executed by Git/runner."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from drover import ci, placement, repos, task_access, tool_discovery
from drover.launch import RunnerLauncher
from tests.fakeci import resources_go
from tests.gitfix import Repo


class PlacementTests(unittest.TestCase):
    def setUp(self):
        # These tests exercise placement/CI, independently of the host's free space.
        readings = patch('drover.disk.read', return_value=[])
        readings.start()
        self.addCleanup(readings.stop)

    def test_config_paths_preserve_native_linux_worktrees_with_a_windows_repository(self):
        linux = '/home/dev/work/acme.worktrees/drover-T1'
        self.assertEqual(placement.config_path(linux, r'H:\repos\acme'), linux)
        self.assertEqual(placement.config_path('/mnt/h/repos/acme.worktrees/T1', r'H:\repos\acme'),
                         r'H:\repos\acme.worktrees\T1')

    def test_windows_gate_maps_a_shared_wsl_mount_to_its_native_checkout_path(self):
        shared = '/mnt/h/repos/acme.worktrees/drover-T1'
        with patch('drover.placement_paths.IS_WINDOWS', True):
            native = placement.ci_path('windows', shared)
        self.assertEqual(native, r'H:\repos\acme.worktrees\drover-T1')
        self.assertEqual(placement.to_wsl_path(native), shared)

    def test_ledger_distro_changes_reach_git_and_runner_together(self):
        settings = {'wsl_distro': 'UI-choice'}
        previous = placement.bind_settings(lambda: settings)
        self.addCleanup(placement.bind_settings, previous)
        for name in ('UI-choice', 'Changed'):
            settings['wsl_distro'] = name
            self.assertEqual(placement.git_argv('/home/repo', ['status'], nt=True)[2], name)
            self.assertEqual(RunnerLauncher('unused', distro='config-choice').runner_argv(['find'])[2], name)

    @unittest.skipIf(os.name == 'nt', 'POSIX executable bridge')
    def test_composed_wsl_command_preserves_worker_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            # A real executable bridge exercises the entire composed argv on WSL.
            bridge = Path(tmp) / 'wsl.exe'
            bridge.write_text('#!' + sys.executable + '\nimport os,sys\na=sys.argv[1:]; '
                              'i=a.index("--exec"); os.execvp(a[i+1], a[i+1:])\n')
            bridge.chmod(0o755)
            payload = [r'H:\literal', '$HOME', 'a b', '"quotes"', '--opt=C:\\x']
            argv = placement.WSL('test').command([sys.executable, '-c', 'import json,sys; print(json.dumps(sys.argv[1:]))', *payload])
            with patch.dict(os.environ, {'PATH': tmp + os.pathsep + os.environ.get('PATH', '')}):
                result = subprocess.run(argv, capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout), payload)

    @unittest.skipIf(os.name == 'nt', 'WSL gitdir spelling')
    def test_windows_gitdir_is_read_and_checks_make_it_portable(self):
        repo = Repo()
        self.addCleanup(repo.cleanup)
        code = ('import os,subprocess,tempfile; '
                'subprocess.run(["git", "rev-parse", "HEAD"], check=True); '
                'assert "GIT_DIR" not in os.environ; '
                'd=tempfile.mkdtemp(); subprocess.run(["git", "init", d], check=True)')
        exe = sys.executable.replace("'", "''")
        repo.commit('scripts/check.ps1', f"& '{exe}' -c '{code}'\nexit $LASTEXITCODE\n")
        wt = repo.root / 'wt'
        repo.git('worktree', 'add', '-q', '-b', 'topic', wt)
        admin = repo.git('-C', wt, 'rev-parse', '--absolute-git-dir')
        dotgit = wt / '.git'
        dotgit.write_text('gitdir: H:/repo/.git/worktrees/wt\n')
        before = dotgit.read_bytes()
        real = placement.to_wsl_path
        with patch.object(placement, 'to_wsl_path', side_effect=lambda p: admin if p == 'H:/repo/.git/worktrees/wt' else real(p)) as _placement_patch_1, patch('drover.placement_exec.to_wsl_path', _placement_patch_1), patch('drover.placement_git.to_wsl_path', _placement_patch_1), patch('drover.placement_host.to_wsl_path', _placement_patch_1), patch('drover.placement_paths.to_wsl_path', _placement_patch_1):
            self.assertEqual(ci.head(wt), repo.git('rev-parse', 'HEAD'))
            self.assertEqual(ci.dirty(wt), '')
            self.assertEqual(dotgit.read_bytes(), before)
            platform = 'wsl'
            prog = repos.normalize({'name': 'p', 'repo': str(wt), 'scripts': {'test': 'scripts/check.ps1'},
                                    'gate': {'platform': platform, 'docs_only': False}})
            receipt = ci.CI(repo.root / 'runs', resource_check=resources_go).run(prog, wt, ci.head(wt))
            self.assertEqual(receipt['status'], 'passed')
        pointer = dotgit.read_text().strip().removeprefix("gitdir: ")
        self.assertFalse(Path(pointer).is_absolute())
        self.assertFalse(placement.WIN_RE.match(pointer))


    def test_cross_drive_gitdir_stays_absolute(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            marker = Path(tmp) / '.git'
            marker.write_text('gitdir: C:/repo/.git/worktrees/topic\n')
            original = marker.read_text()
            # The worktree is on H:, the admin dir on C:. Both can exist on WSL.
            with patch('os.path.isdir', return_value=True):
                self.assertFalse(placement.relativize_gitdir(tmp))
            self.assertEqual(marker.read_text(), original)

    def test_malformed_git_metadata_reports_the_pointer(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / '.git').write_text('broken metadata')
            with self.assertRaisesRegex(ValueError, 'invalid git metadata pointer'):
                placement.git(tmp, 'status')

    @unittest.skipIf(os.name == 'nt', 'other-host adapter check')
    def test_windows_spawn_requires_windows_process_ownership(self):
        with self.assertRaisesRegex(OSError, 'requires Windows'):
            placement.Windows().spawn([sys.executable, '-c', 'pass'])

    def test_untracked_source_rejects_a_commit_before_gate(self):
        repo = Repo()
        self.addCleanup(repo.cleanup)
        (repo.repo / 'new-source').write_text('never added')
        self.assertIn('new-source', ci.dirty(repo.repo))
        platform = 'windows' if os.name == 'nt' else 'wsl'
        prog = repos.normalize({'name': 'p', 'repo': str(repo.repo), 'scripts': {'test': 'scripts/check.ps1'},
                                'gate': {'platform': platform, 'docs_only': False}})
        result = ci.CI(repo.root / 'runs', resource_check=resources_go).run(prog, repo.repo, ci.head(repo.repo))
        self.assertEqual(result['outcome'], 'deferred')
        self.assertIn('uncommitted', result['reason'])

    def test_read_snapshots_preserve_history_and_independent_exact_commits(self):
        repo = Repo()
        self.addCleanup(repo.cleanup)
        original = repo.git('rev-parse', 'HEAD')
        repo.commit('later.txt', 'later')
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {'p': {'repo': str(repo.repo), 'default_branch': 'main'}}
            first = task_access.prepare(['p'], cfg, tmp)[0]
            repo.commit('newest.txt', 'newest')
            second = task_access.prepare(['p'], cfg, tmp)[0]
            dest = Path(second['path'])
            self.assertNotEqual(first['path'], second['path'])
            self.assertIn(original, repo.git('log', '--format=%H', cwd=dest))
            self.assertIn('base', repo.git('blame', 'base.txt', cwd=dest))
            self.assertFalse((dest / '.git/objects/info/alternates').exists())
            self.assertFalse(any(Path(tmp).glob('objects-*')))
            repo.git('reset', '--hard', original)
            self.assertEqual(repo.git('rev-parse', 'HEAD', cwd=dest), second['sha'])
            self.assertEqual(repo.git('rev-parse', 'HEAD', cwd=first['path']), first['sha'])
            task_access.verify([first, second])

    @unittest.skipIf(os.name == 'nt', 'POSIX executable discovery')
    def test_machine_tool_discovery_uses_path_executables(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ('claude', 'codex', 'pwsh'):
                executable = Path(tmp) / name
                executable.write_text('#!/bin/sh\nexit 0\n')
                executable.chmod(0o755)
            result = subprocess.run(['/bin/bash', '-c', tool_discovery.DISCOVERY_SCRIPT],
                                    env={'PATH': tmp, 'DROVER_WORKER_PATH': '/usr/bin:/bin'}, capture_output=True, text=True, check=True)
            found, reasons = tool_discovery.parse_discovery_result(result.stdout)
            self.assertEqual(found, {name: str(Path(tmp) / name) for name in ('claude', 'codex', 'pwsh')})
            self.assertEqual(reasons, {})


class CleanupIdentityCache(unittest.TestCase):
    def test_own_identity_is_read_once_per_process(self):
        with patch.object(placement, 'IS_WINDOWS', True) as _placement_patch_2, patch('drover.placement_exec.IS_WINDOWS', _placement_patch_2), patch('drover.placement_git.IS_WINDOWS', _placement_patch_2), patch('drover.placement_host.IS_WINDOWS', _placement_patch_2), patch('drover.placement_paths.IS_WINDOWS', _placement_patch_2), patch('drover.placement_process.IS_WINDOWS', _placement_patch_2), patch('drover.placement_proof.IS_WINDOWS', _placement_patch_2), patch('drover.placement_tree.IS_WINDOWS', _placement_patch_2), patch('drover.placement_worker.IS_WINDOWS', _placement_patch_2), \
                patch.object(placement, '_read_windows_birth', return_value=1234) as birth, patch('drover.placement_process._read_windows_birth', birth), \
                patch('drover.placement_process._SELF_IDENTITY', None):
            first = placement.cleanup_identity()
            second = placement.cleanup_identity()
            placement.cleanup_identity(os.getpid() + 1)
        self.assertEqual(first, second)
        self.assertEqual(first['birth'], '1234')
        self.assertEqual(birth.call_args_list, [unittest.mock.call(os.getpid()), unittest.mock.call(os.getpid() + 1)])

    def test_a_cached_identity_from_another_pid_is_not_reused(self):
        with patch.object(placement, 'IS_WINDOWS', True) as _placement_patch_5, patch('drover.placement_exec.IS_WINDOWS', _placement_patch_5), patch('drover.placement_git.IS_WINDOWS', _placement_patch_5), patch('drover.placement_host.IS_WINDOWS', _placement_patch_5), patch('drover.placement_paths.IS_WINDOWS', _placement_patch_5), patch('drover.placement_process.IS_WINDOWS', _placement_patch_5), patch('drover.placement_proof.IS_WINDOWS', _placement_patch_5), patch('drover.placement_tree.IS_WINDOWS', _placement_patch_5), patch('drover.placement_worker.IS_WINDOWS', _placement_patch_5), \
                patch.object(placement, '_read_windows_birth', return_value=5678) as birth, patch('drover.placement_process._read_windows_birth', birth), \
                patch('drover.placement_process._SELF_IDENTITY', dict(pid=os.getpid() + 1, birth='1234', side='windows')):
            current = placement.cleanup_identity()
        self.assertEqual(current['birth'], '5678')
        birth.assert_called_once_with(os.getpid())


class NativeCleanupIdentity(unittest.TestCase):
    def identity(self, *, handle=123, error=0, times=True):
        import ctypes
        pid = os.getpid() + 1  # always exercise another process, even inside a PID namespace
        kernel = unittest.mock.Mock()
        kernel.OpenProcess.return_value = handle
        def read(handle, created, exited, system, user):
            created._obj.dwHighDateTime, created._obj.dwLowDateTime = 42, 17
            return times
        kernel.GetProcessTimes.side_effect = read
        with patch.object(placement, 'IS_WINDOWS', True) as _placement_patch_8, patch('drover.placement_exec.IS_WINDOWS', _placement_patch_8), patch('drover.placement_git.IS_WINDOWS', _placement_patch_8), patch('drover.placement_host.IS_WINDOWS', _placement_patch_8), patch('drover.placement_paths.IS_WINDOWS', _placement_patch_8), patch('drover.placement_process.IS_WINDOWS', _placement_patch_8), patch('drover.placement_proof.IS_WINDOWS', _placement_patch_8), patch('drover.placement_tree.IS_WINDOWS', _placement_patch_8), patch('drover.placement_worker.IS_WINDOWS', _placement_patch_8), \
                patch.object(ctypes, 'WinDLL', return_value=kernel, create=True), \
                patch.object(ctypes, 'get_last_error', return_value=error, create=True), \
                patch.object(placement.subprocess, 'run', side_effect=AssertionError('identity spawned a process')):
            value = placement.cleanup_identity(pid)
            alive = placement.cleanup_claim_alive(dict(pid=pid, side='windows', birth=str((42 << 32) | 17)))
        if handle:
            self.assertEqual(kernel.CloseHandle.call_count, 2)
        else:
            kernel.CloseHandle.assert_not_called()
        return value, alive

    def test_native_filetime_matches_existing_windows_records_without_a_process(self):
        identity, alive = self.identity()
        self.assertEqual(identity, dict(pid=os.getpid() + 1, side='windows', birth=str((42 << 32) | 17)))
        self.assertTrue(alive)

    def test_only_proven_absence_marks_a_claim_dead(self):
        identity, alive = self.identity(handle=0, error=87)
        self.assertEqual(identity['birth'], 'absent')
        self.assertFalse(alive)

    def test_access_denied_and_failed_creation_reads_leave_a_claim_unknown(self):
        for kwargs in (dict(handle=0, error=5), dict(handle=0, error=0), dict(times=False, error=5)):
            with self.subTest(kwargs=kwargs):
                identity, alive = self.identity(**kwargs)
                self.assertIsNone(identity['birth'])
                self.assertIsNone(alive)
