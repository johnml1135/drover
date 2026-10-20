"""Placement's public interface, including composed commands executed by Git/runner."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from drover import gate, placement, task_access, tool_discovery
from drover.launch import RunnerLauncher
from tests.gitfix import Repo


class PlacementTests(unittest.TestCase):
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
        wt = repo.root / 'wt'
        repo.git('worktree', 'add', '-q', '-b', 'topic', wt)
        admin = repo.git('-C', wt, 'rev-parse', '--absolute-git-dir')
        dotgit = wt / '.git'
        dotgit.write_text('gitdir: H:/repo/.git/worktrees/wt\n')
        before = dotgit.read_bytes()
        real = placement.to_wsl_path
        with patch.object(placement, 'to_wsl_path', side_effect=lambda p: admin if p == 'H:/repo/.git/worktrees/wt' else real(p)):
            self.assertEqual(gate.head(wt), repo.git('rev-parse', 'HEAD'))
            self.assertEqual(gate.dirty(wt), '')
            self.assertEqual(dotgit.read_bytes(), before)
            command = [sys.executable, '-c', 'import os,subprocess,tempfile; '
                       'subprocess.run(["git", "rev-parse", "HEAD"], check=True); '
                       'assert "GIT_DIR" not in os.environ; '
                       'd=tempfile.mkdtemp(); subprocess.run(["git", "init", d], check=True)']
            receipt = gate.run_gate(wt, repo.root / 'receipt.json', command, available=False)
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
        self.assertIn('new-source', gate.dirty(repo.repo))
        with self.assertRaisesRegex(gate.GateError, 'uncommitted'):
            gate.run_gate(repo.repo, repo.root / 'receipt.json',
                          [sys.executable, '-c', 'raise AssertionError("must not run")'], available=False)

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
                                    env={'PATH': tmp}, capture_output=True, text=True, check=True)
            found, reasons = tool_discovery.parse_discovery_result(result.stdout)
            self.assertEqual(found, {name: str(Path(tmp) / name) for name in ('claude', 'codex', 'pwsh')})
            self.assertEqual(reasons, {})
