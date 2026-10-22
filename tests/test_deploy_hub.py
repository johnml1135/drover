"""scripts/deploy_hub.py: pinning the live hub to a deliberately updated worktree. Real temp git repos; process and
service operations are injected so no test stops anything."""
import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

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


class DeployTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=clean_parent())
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.src, self.dep = root / 'src', root / 'src.deploy'
        self.src.mkdir()
        sh('git', 'init', '-b', 'main', cwd=self.src)
        sh('git', 'config', 'user.email', 't@t', cwd=self.src)
        sh('git', 'config', 'user.name', 't', cwd=self.src)
        self.c1 = self.commit('a')
        self.c2 = self.commit('b')
        sh('git', 'branch', 'side', self.c1, cwd=self.src)
        sh('git', 'checkout', '-q', '-b', 'topic', cwd=self.src)
        self.off = self.commit('c')          # on topic only, not on main
        sh('git', 'checkout', '-q', 'main', cwd=self.src)
        self.receipt = root / 'receipt.json'

    def commit(self, name):
        (self.src / name).write_text(name)
        sh('git', 'add', name, cwd=self.src)
        sh('git', 'commit', '-m', name, cwd=self.src)
        return sh('git', 'rev-parse', 'HEAD', cwd=self.src).stdout.strip()

    def write_receipt(self, sha, passed=True):
        self.receipt.write_text(json.dumps({'sha': sha, 'passed': passed}))

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
