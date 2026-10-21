import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from tests.disk_fixture import DiskCase


class State(DiskCase):
    def test_cross_process_lock_contention_and_release(self):
        from drover.disk_state import CleanupLock
        helper = self.root / 'lock contender.py'
        helper.write_text('''import sys
from pathlib import Path
from drover.disk_state import CleanupLock
state = Path(sys.argv[1])
try:
    with CleanupLock(state):
        print('acquired', flush=True)
        sys.stdin.readline()
except BlockingIOError:
    print('busy', flush=True)
    sys.exit(23)
''')
        env = {**os.environ, 'PYTHONPATH': os.pathsep.join(
            [str(Path(__file__).resolve().parents[1]), os.environ.get('PYTHONPATH', '')])}
        first = subprocess.Popen([sys.executable, str(helper), str(self.state)], env=env,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: first.poll() is None and first.kill())
        try:
            self.assertEqual(first.stdout.readline().strip(), 'acquired')
            lock = self.state / 'disk-cleanup.lock'
            self.assertTrue(lock.is_dir())
            diagnostic = ''.join(path.read_text() for path in lock.iterdir() if path.is_file())
            self.assertIn(str(first.pid), diagnostic)
            second = subprocess.run([sys.executable, str(helper), str(self.state)], env=env,
                                    input='', capture_output=True, text=True, timeout=10)
            self.assertEqual(second.returncode, 23, second.stderr)
            self.assertEqual(second.stdout.strip(), 'busy')
            self.assertTrue(lock.is_dir())
            first.communicate('\n', timeout=10)
            self.assertEqual(first.returncode, 0)
            self.assertFalse(lock.exists())
            with self.assertRaisesRegex(RuntimeError, 'body failure'):
                with CleanupLock(self.state):
                    raise RuntimeError('body failure')
            self.assertFalse(lock.exists())
            with CleanupLock(self.state):
                self.assertTrue(lock.is_dir())
        finally:
            if first.poll() is None:
                first.kill()
                first.communicate(timeout=10)

    def test_manifest_failure_prevents_removal(self):
        # Build a real settled checkout; fsync failure must block its removal.
        from tests.test_disk_worktrees import WorktreeCase
        case = WorktreeCase(methodName='runTest')
        case.setUp()
        self.addCleanup(case.doCleanups)
        wt = case.worktree('journal-failure')
        case.r.commit('landed.txt', 'landed', cwd=wt)
        case.merge(wt)
        case.cleanup()
        registrations, branches = case.registrations(), case.branches()
        with patch('os.fsync', side_effect=OSError('injected journal fsync failure')):
            result = case.cleanup(apply=True)
        self.assertTrue(wt.is_dir())
        self.assertEqual(case.registrations(), registrations)
        self.assertEqual(case.branches(), branches)
        self.assertTrue(result['errors'])
        journal = case.state / 'disk-cleanup-manifest.jsonl'
        if journal.exists():
            self.assertFalse(any(json.loads(line)['phase'] == 'completed'
                                 for line in journal.read_text().splitlines()))

    def test_state_transactions_versions_and_durable_journal(self):
        from drover import disk_state
        self.assertEqual(disk_state.read(self.state), {'v': 1})
        disk_state.write(self.state, {'v': 1, 'readings': {'drive': 90}})
        changed = disk_state.read(self.state)
        changed['readings']['drive'] = 80
        disk_state.write(self.state, changed)
        self.assertEqual(disk_state.read(self.state)['readings'], {'drive': 80})
        journal = self.state / 'disk-cleanup-manifest.jsonl'
        with patch('os.fsync', side_effect=OSError('fsync refused')):
            with self.assertRaises(OSError):
                disk_state.append_manifest(self.state, {'v': 1, 'phase': 'planned'})
        disk_state.append_manifest(self.state, {'v': 1, 'phase': 'failed'})
        self.assertEqual(json.loads(journal.read_text().splitlines()[-1])['phase'], 'failed')
        path = self.state / 'disk' / 'state.json'
        for payload in ('{bad', '{"v":2}', '{"v":true}'):
            path.write_text(payload)
            with self.assertRaises(ValueError):
                disk_state.read(self.state)
            with self.assertRaises(ValueError):
                disk_state.write(self.state, {'v': 1})
            self.assertEqual(path.read_text(), payload)

    def test_partial_holder_diagnostic_failure_releases_lock(self):
        from drover import disk_state
        original = Path.write_text

        def fail_after_creation(path, *args, **kwargs):
            original(path, '', encoding='utf-8')
            raise OSError('disk full')

        with patch.object(Path, 'write_text', fail_after_creation):
            with self.assertRaisesRegex(OSError, 'disk full'):
                with disk_state.CleanupLock(self.state):
                    self.fail('unacquired lock must not enter body')
        self.assertFalse((self.state / 'disk-cleanup.lock').exists())
        with disk_state.CleanupLock(self.state):
            pass
