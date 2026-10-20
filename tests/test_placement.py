"""Placement's public interface, including composed commands executed by Git/runner."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from drover import gate, placement, task_access
from tests.gitfix import Repo


class PlacementTests(unittest.TestCase):
    def test_ledger_distro_changes_reach_git_and_runner_together(self):
        settings = {'wsl_distro': 'UI-choice'}
        previous = placement.bind_settings(lambda: settings)
        self.addCleanup(placement.bind_settings, previous)
        for name in ('UI-choice', 'Changed'):
            settings['wsl_distro'] = name
            self.assertEqual(placement.git_argv('/home/repo', ['status'], nt=True)[2], name)
            self.assertEqual(placement.wsl_argv('config-choice', ['find'])[2], name)

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

    @unittest.skipIf(os.name == 'nt', 'DrvFs gate policy')
    def test_drvfs_status_skips_untracked_scan_but_detects_tracked_edits(self):
        repo = Repo()
        self.addCleanup(repo.cleanup)
        real = placement.MNT_RE
        class DriveMount:
            def match(self, path):
                return real.match(path) or str(path) == str(repo.repo)
        with patch.object(placement, 'MNT_RE', DriveMount()):
            (repo.repo / 'untracked').mkdir()
            (repo.repo / 'untracked' / 'build.bin').write_text('output')
            self.assertEqual(gate.dirty(repo.repo), '')
            (repo.repo / 'base.txt').write_text('tracked edit')
            self.assertIn('base.txt', gate.dirty(repo.repo))

    def test_read_snapshots_have_history_and_share_objects_safely(self):
        repo = Repo()
        self.addCleanup(repo.cleanup)
        original = repo.git('rev-parse', 'HEAD')
        repo.commit('later.txt', 'later')
        with tempfile.TemporaryDirectory() as tmp:
            self.addCleanup(task_access.make_writable, tmp)
            cfg = {'p': {'repo': str(repo.repo), 'default_branch': 'main'}}
            first = Path(task_access.prepare(['p'], cfg, tmp)[0]['path'])
            repo.commit('newest.txt', 'newest')
            read = task_access.prepare(['p'], cfg, tmp)[0]
            dest = Path(read['path'])
            self.assertIn(original, repo.git('log', '--format=%H', cwd=dest))
            self.assertIn('base', repo.git('blame', 'base.txt', cwd=dest))
            obj = Path('.git/objects') / original[:2] / original[2:]
            if os.name != 'nt' and (first / obj).stat().st_dev == (dest / obj).stat().st_dev:
                self.assertEqual((first / obj).stat().st_ino, (dest / obj).stat().st_ino)
            self.assertFalse((dest / '.git/objects/info/alternates').exists())
            # Source refs can move without changing the snapshot's history.
            repo.git('reset', '--hard', original)
            self.assertEqual(repo.git('rev-parse', 'HEAD', cwd=dest), read['sha'])
            task_access.make_writable(tmp)
