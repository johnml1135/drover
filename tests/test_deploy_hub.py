"""scripts/deploy_hub.py: pinning the live hub to a deliberately updated worktree. Real temp git repos; process and
service operations are injected so no test stops anything."""
import importlib.util
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest import mock

from tests.gitfix import clean_parent, sh

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'deploy_hub.py'
spec = importlib.util.spec_from_file_location('deploy_hub', SCRIPT)
deploy_hub = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy_hub)


class Fake:
    """Records the commands the script would run and answers the listener query."""
    def __init__(self, pids=(), cmd=None, task='Ready', stop_rc=0, linger=0):
        self.calls, self.pids, self.task, self.stop_rc, self.linger = [], list(pids), task, stop_rc, linger
        self.cmd = cmd if cmd is not None else 'C:\\src\\hub\\.venv\\python.exe|python.exe -m hub'
        self.src = None
        self.stopped = False

    def __call__(self, argv, **kw):
        self.calls.append([str(a) for a in argv])
        out, rc = '', 0
        if argv[0] == 'LISTENERS':
            out = ' '.join(self.pids)
            if self.linger and self.stopped:
                self.linger -= 1
                out = '999'
        elif argv[0] == 'INSPECT':
            out = self.cmd.replace('C:\\src', str(self.src))
        elif argv[0] == 'TASK':
            out = self.task
        elif argv[0] == 'STOP':
            rc = self.stop_rc
            self.stopped = True
            if not rc:
                self.pids = []
        return subprocess.CompletedProcess(argv, rc, out, 'boom' if rc else '')


class CrossSideAdd(unittest.TestCase):
    """From WSL a deploy worktree on a Windows drive must hold pointers Windows git can read."""
    def versions(self, local, windows):
        out = {'git': local, 'git.exe': windows}

        def tool(argv):
            v = out[argv[0]]
            return None if v is None else f'git version {v}.windows.1\n'
        return tool

    def posix_paths(self):
        return mock.patch.object(deploy_hub, "Path",
                                 side_effect=lambda value: SimpleNamespace(absolute=lambda: PurePosixPath(value)))

    def add(self, local, windows):
        calls, exe = [], []
        with self.posix_paths(), mock.patch.object(deploy_hub, 'IS_WINDOWS', False), \
                mock.patch.object(deploy_hub, 'git', lambda repo, *a, **k: calls.append((str(repo), a))), \
                mock.patch.object(deploy_hub.subprocess, 'run',
                                  lambda argv, **k: exe.append(argv) or subprocess.CompletedProcess(argv, 0, '', '')):
            deploy_hub.add_deploy_worktree('/mnt/h/repos/x', '/mnt/h/repos/x.deploy', 'abc', self.versions(local, windows))
        return calls, exe

    def test_new_git_on_both_sides_uses_relative_paths(self):
        calls, exe = self.add('2.53.0', '2.52.0')
        self.assertEqual(calls, [('/mnt/h/repos/x', ('worktree', 'add', '--lock', '--reason', 'wsl', '--relative-paths', '--detach',
                                                     '/mnt/h/repos/x.deploy', 'abc'))])
        self.assertEqual(exe, [])

    def test_old_git_on_either_side_runs_the_add_through_windows_git(self):
        for local, windows in (('2.47.1', '2.52.0'), ('2.53.0', '2.45.0')):
            calls, exe = self.add(local, windows)
            self.assertEqual(calls, [])
            self.assertEqual(exe, [['git.exe', '-C', 'H:\\repos\\x', 'worktree', 'add', '--lock', '--reason', 'windows', '--detach',
                                    'H:\\repos\\x.deploy', 'abc']])

    def test_no_windows_git_and_old_local_git_is_refused(self):
        with self.posix_paths(), mock.patch.object(deploy_hub, 'git'):
            with self.assertRaises(deploy_hub.DeployError):
                deploy_hub.add_deploy_worktree('/mnt/h/x', '/mnt/h/x.d', 'abc', self.versions('2.53.0', None))

    def test_native_windows_source_uses_only_local_git(self):
        from pathlib import PureWindowsPath
        with mock.patch.object(deploy_hub, 'IS_WINDOWS', True), mock.patch.object(deploy_hub, 'Path',
                               side_effect=lambda value: SimpleNamespace(absolute=lambda: PureWindowsPath(value))), \
                mock.patch.object(deploy_hub, 'git') as git, \
                mock.patch.object(deploy_hub.subprocess, 'run') as run:
            versions = mock.Mock(side_effect=AssertionError('native add needs no cross-side version query'))
            deploy_hub.add_deploy_worktree('H:\\repos\\x', 'H:\\repos\\x.deploy', 'abc', versions)
        git.assert_called_once_with('H:\\repos\\x', 'worktree', 'add', '--lock', '--reason', 'windows',
                                '--detach', 'H:\\repos\\x.deploy', 'abc')
        run.assert_not_called()
        versions.assert_not_called()

    def test_relative_paths_really_write_relative_pointers(self):
        with tempfile.TemporaryDirectory(dir=clean_parent()) as d:
            repo = Path(d) / 'src'
            repo.mkdir()
            sh('git', 'init', '-b', 'main', cwd=repo)
            sh('git', 'config', 'user.email', 't@t', cwd=repo)
            sh('git', 'config', 'user.name', 't', cwd=repo)
            (repo / 'a').write_text('a')
            sh('git', 'add', 'a', cwd=repo)
            sh('git', 'commit', '-m', 'a', cwd=repo)
            dep = Path(d) / 'dep'
            sh('git', 'worktree', 'add', '--relative-paths', '--detach', str(dep), 'HEAD', cwd=repo)
            self.assertFalse((dep / '.git').read_text().removeprefix('gitdir: ').startswith('/'))


class DeployTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = tempfile.TemporaryDirectory(dir=clean_parent())
        cls.addClassCleanup(cls.template.cleanup)
        src = Path(cls.template.name) / 'src'
        src.mkdir()
        sh('git', 'init', '-b', 'main', cwd=src)
        sh('git', 'config', 'user.email', 't@t', cwd=src)
        sh('git', 'config', 'user.name', 't', cwd=src)
        for name in ('a', 'b', 'c'):
            if name == 'c':
                sh('git', 'branch', 'side', cls.c1, cwd=src)
                sh('git', 'checkout', '-q', '-b', 'topic', cwd=src)
            (src / name).write_text(name)
            sh('git', 'add', name, cwd=src)
            sh('git', 'commit', '-m', name, cwd=src)
            setattr(cls, {'a': 'c1', 'b': 'c2', 'c': 'off'}[name], sh('git', 'rev-parse', 'HEAD', cwd=src).stdout.strip())
        sh('git', 'checkout', '-q', 'main', cwd=src)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=clean_parent())
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.src, self.dep = root / 'src', root / 'src.deploy'
        shutil.copytree(Path(self.template.name) / 'src', self.src)
        self.receipt = root / 'receipt.json'
    def write_receipt(self, sha, passed=True):
        self.receipt.write_text(json.dumps({'version': 1, 'sha': sha, 'passed': passed, 'kind': 'full',
                                           'status': 'passed' if passed else 'failed',
                                           'outcome': 'passed' if passed else 'code-failed',
                                           'exit_code': 0 if passed else 1, 'tree_unchanged': True,
                                           'platform': 'windows'}))
    def head(self):
        return sh('git', 'rev-parse', 'HEAD', cwd=self.dep).stdout.strip()
    def run_main(self, *argv, fake=None):
        fake = fake or Fake()
        fake.src = self.src
        out = io.StringIO()
        with redirect_stdout(out):
            code = deploy_hub.main(['--source', str(self.src), '--deploy', str(self.dep), *argv], run=fake, sleep=lambda s: None)
        return code, out.getvalue(), fake

    def test_creates_detached_worktree_at_main_tip_with_receipt(self):
        self.write_receipt(self.c2)
        code, out, _ = self.run_main('--receipt', str(self.receipt))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.head(), self.c2)
        self.assertEqual(sh('git', 'branch', '--show-current', cwd=self.dep).stdout.strip(), '')
        self.assertIn('Install-DroverHub.ps1', out)      # the restart steps are printed, nothing restarted
        self.assertEqual(sh('git', 'rev-parse', 'main', cwd=self.src).stdout.strip(), self.c2)

    def test_moves_existing_deploy_to_older_main_commit(self):
        self.write_receipt(self.c2)
        self.run_main('--receipt', str(self.receipt))
        self.write_receipt(self.c1)
        code, out, _ = self.run_main('--commit', self.c1, '--receipt', str(self.receipt))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.head(), self.c1)

    def test_refuses_without_receipt_or_force(self):
        code, out, _ = self.run_main()
        self.assertNotEqual(code, 0)
        self.assertFalse(self.dep.exists())
        self.assertIn('--force', out)

    def test_refuses_failed_or_other_sha_receipt(self):
        self.write_receipt(self.c2, passed=False)
        self.assertNotEqual(self.run_main('--receipt', str(self.receipt))[0], 0)
        self.write_receipt(self.c1)
        self.assertNotEqual(self.run_main('--receipt', str(self.receipt))[0], 0)
        self.assertFalse(self.dep.exists())

    def test_refuses_commit_not_on_main_even_with_receipt_but_force_allows(self):
        self.write_receipt(self.off)
        code, out, _ = self.run_main('--commit', self.off, '--receipt', str(self.receipt))
        self.assertNotEqual(code, 0)
        self.assertIn('not on main', out)
        code, out, _ = self.run_main('--commit', self.off, '--force')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.head(), self.off)

    def test_refuses_dirty_deploy_checkout(self):
        self.run_main('--force')
        (self.dep / 'a').write_text('changed')
        code, out, _ = self.run_main('--commit', self.c1, '--force')
        self.assertNotEqual(code, 0)
        self.assertIn('uncommitted', out)
        self.assertEqual(self.head(), self.c2)

    def test_default_run_never_touches_processes(self):
        _, _, fake = self.run_main('--force')
        self.assertEqual(fake.calls, [])

    def test_restart_stops_only_listener_pids_without_tree_kill(self):
        _, out, fake = self.run_main('--force', '--restart', '--port', '8765', '--uv', 'UV', fake=Fake(pids=['111', '222']))
        stops = [c for c in fake.calls if c[0] == 'STOP']
        self.assertEqual(stops, [['STOP', '111'], ['STOP', '222']])
        flat = ' '.join(' '.join(c) for c in fake.calls).lower()
        self.assertNotIn('/t', flat.split())
        self.assertNotIn('-recurse', flat)
        self.assertTrue(any(c[:2] == ['UV', 'sync'] and str(self.dep / 'hub') in c for c in fake.calls))
        self.assertTrue(any(c[0] == 'START' for c in fake.calls))
        order = [c[0] for c in fake.calls]
        self.assertLess(order.index('UV'), order.index('STOP'))   # deps synced before the old hub is stopped

    def test_restart_refuses_listener_that_is_not_the_hub(self):
        for cmd in ('C:\\other\\python.exe|python.exe -m hub', 'C:\\src\\x.exe|python.exe -m http.server'):
            code, out, fake = self.run_main('--force', '--restart', fake=Fake(pids=['5'], cmd=cmd))
            self.assertNotEqual(code, 0)
            self.assertIn('not the hub', out)
            self.assertFalse([c for c in fake.calls if c[0] in ('STOP', 'START')])

    def test_restart_without_task_explains_migration(self):
        code, out, fake = self.run_main('--force', '--restart', fake=Fake(pids=['5'], task=''))
        self.assertNotEqual(code, 0)
        self.assertIn('Install-DroverHub.ps1', out)
        self.assertFalse([c for c in fake.calls if c[0] in ('STOP', 'START')])

    def test_failed_stop_aborts_start(self):
        code, out, fake = self.run_main('--force', '--restart', fake=Fake(pids=['5'], stop_rc=1))
        self.assertNotEqual(code, 0)
        self.assertFalse([c for c in fake.calls if c[0] == 'START'])

    def test_waits_for_port_then_starts_or_gives_up(self):
        code, _, fake = self.run_main('--force', '--restart', fake=Fake(pids=['5'], linger=3))
        self.assertEqual(code, 0)
        self.assertTrue([c for c in fake.calls if c[0] == 'START'])
        code, out, fake = self.run_main('--force', '--restart', '--wait', '2', fake=Fake(pids=['5'], linger=50))
        self.assertNotEqual(code, 0)
        self.assertFalse([c for c in fake.calls if c[0] == 'START'])

    def test_untracked_file_makes_deploy_dirty(self):
        self.run_main('--force')
        (self.dep / 'stray').write_text('x')
        code, out, _ = self.run_main('--commit', self.c1, '--force')
        self.assertNotEqual(code, 0)
        self.assertIn('uncommitted', out)

    def test_restart_with_no_listener_just_starts(self):
        _, _, fake = self.run_main('--force', '--restart', fake=Fake())
        self.assertFalse([c for c in fake.calls if c[0] == 'STOP'])
        self.assertTrue([c for c in fake.calls if c[0] == 'START'])


if __name__ == '__main__':
    unittest.main()
