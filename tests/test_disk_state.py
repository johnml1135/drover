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
