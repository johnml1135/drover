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
        import os
        print('acquired ' + str(os.getpid()), flush=True)
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
            ready, holder_pid = first.stdout.readline().strip().split()
            self.assertEqual(ready, 'acquired')
            lock = self.state / 'disk-cleanup.lock'
            self.assertTrue(lock.is_dir())
            diagnostic = ''.join(path.read_text() for path in lock.iterdir() if path.is_file())
            self.assertEqual(json.loads(diagnostic)['pid'], int(holder_pid))
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


class StaleLock(DiskCase):
    """A dead same-side holder is reclaimed; anything unprovable stays locked."""

    def plant(self, **over):
        lock = self.state / 'disk-cleanup.lock'
        lock.mkdir()
        owner = dict(token='dead-token', pid=424242, host=__import__('socket').gethostname(),
                     created=1.0, side=self.side, birth='boot:100')
        owner.update(over)
        (lock / 'owner.json').write_text(json.dumps(owner), encoding='utf-8')
        return lock

    def identity(self, birth):
        return lambda pid=None: dict(pid=pid, birth=birth, side=self.side)

    def test_acquire_records_side_and_birth(self):
        from drover.disk_state import CleanupLock
        with patch('drover.placement.cleanup_identity', self.identity('boot:7')):
            with CleanupLock(self.state) as lock:
                owner = json.loads((lock.path / 'owner.json').read_text())
        self.assertEqual((owner['side'], owner['birth']), (self.side, 'boot:7'))

    def test_dead_holder_is_reclaimed(self):
        from drover.disk_state import CleanupLock
        lock = self.plant()
        with patch('drover.placement.cleanup_identity', self.identity('absent')):
            with CleanupLock(self.state) as mine:
                self.assertNotEqual(json.loads((lock / 'owner.json').read_text())['token'], 'dead-token')
                self.assertTrue(mine.acquired)
        self.assertFalse(lock.exists())
        self.assertEqual([p.name for p in self.state.iterdir() if 'stale' in p.name], [])

    def test_live_holder_is_kept(self):
        from drover.disk_state import CleanupLock
        lock = self.plant()
        with patch('drover.placement.cleanup_identity', self.identity('boot:100')):
            with self.assertRaises(BlockingIOError):
                CleanupLock(self.state).__enter__()
        self.assertEqual(json.loads((lock / 'owner.json').read_text())['token'], 'dead-token')

    def test_reused_pid_with_different_birth_is_reclaimed(self):
        from drover.disk_state import CleanupLock
        self.plant()
        with patch('drover.placement.cleanup_identity', self.identity('boot:999')):
            with CleanupLock(self.state) as mine:
                self.assertTrue(mine.acquired)

    def test_other_side_other_host_and_unreadable_holders_are_kept(self):
        from drover.disk_state import CleanupLock
        for over in (dict(side=self.other_side), dict(host='another-host'), dict(birth=None), dict(side=None)):
            with self.subTest(over=over):
                lock = self.plant(**over)
                with patch('drover.placement.cleanup_identity', self.identity('absent')):
                    with self.assertRaises(BlockingIOError):
                        CleanupLock(self.state).__enter__()
                (lock / 'owner.json').unlink()
                lock.rmdir()
        lock = self.state / 'disk-cleanup.lock'
        lock.mkdir()
        (lock / 'owner.json').write_text('{not json')
        with patch('drover.placement.cleanup_identity', self.identity('absent')):
            with self.assertRaises(BlockingIOError):
                CleanupLock(self.state).__enter__()

    def holder(self):
        return json.loads((self.state / 'disk-cleanup.lock' / 'owner.json').read_text())['token']

    def hooked(self, hook):
        from drover import disk_state
        return patch.object(disk_state, '_HOOK', hook)

    def enter(self):
        from drover.disk_state import CleanupLock
        lock = CleanupLock(self.state)
        try:
            lock.__enter__()
        except BlockingIOError:
            return lock, False
        return lock, True

    def test_fresh_acquirer_between_rename_and_mkdir_keeps_the_only_lock(self):
        self.plant()
        c = []
        def hook(name):
            if name == 'after_rename':
                c.append(self.enter())  # C: plain mkdir wins the gap
        with patch('drover.placement.cleanup_identity', self.identity('absent')), self.hooked(hook):
            a, a_won = self.enter()
        self.assertFalse(a_won)
        self.assertTrue(c[0][1])
        self.assertEqual(self.holder(), c[0][0].token)
        self.assertEqual([p.name for p in self.state.iterdir() if p.name.endswith('.reclaim')], [])

    def test_stale_view_never_displaces_a_completed_reclaim(self):
        self.plant()
        a = []
        def hook(name):
            if name == 'before_guard' and not a:
                a.append(None)
                a[0] = self.enter()  # A runs a whole reclaim after B read the stale owner
        with patch('drover.placement.cleanup_identity', self.identity('absent')), self.hooked(hook):
            b, b_won = self.enter()
        self.assertFalse(b_won)
        self.assertTrue(a[0][1])
        self.assertEqual(self.holder(), a[0][0].token)

    def test_existing_guard_fails_closed_for_everyone(self):
        self.plant()
        seen = []
        def hook(name):
            if name == 'after_guard' and not seen:
                seen.append(self.enter())  # B and C arrive while A holds the guard
                seen.append(self.enter())
        with patch('drover.placement.cleanup_identity', self.identity('absent')), self.hooked(hook):
            a, a_won = self.enter()
        self.assertTrue(a_won)
        self.assertEqual([won for _, won in seen], [False, False])
        self.assertEqual(self.holder(), a.token)

    def test_stuck_guard_is_never_reclaimed_automatically(self):
        self.plant()
        (self.state / 'disk-cleanup.lock.reclaim').mkdir()
        with patch('drover.placement.cleanup_identity', self.identity('absent')):
            lock, won = self.enter()
        self.assertFalse(won)
        self.assertEqual(self.holder(), 'dead-token')
        from drover import disk_state
        self.assertIn('reclaim_guard', disk_state.lock_blocker(self.state))
