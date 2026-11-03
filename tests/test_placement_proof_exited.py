"""A Windows cleanup proof re-checks the processes it could not read: only one proven gone stops counting."""
import unittest
from unittest import mock

from drover import placement_proof

WORK = r'C:\cargo-targets\stale'
NODE = dict(pid=101, ppid=100, name='node.exe', same_user=True, cwd=r'C:\elsewhere', cmd='node', exe=None,
            session_id=1, command_unreadable=False)


def unread(pid, name, **extra):
    """A CIM row whose owner and cwd could not be read, as for a protected or already exited process."""
    return dict(dict(pid=pid, ppid=1, name=name, same_user=None, cwd=None, cmd='', exe=None, session_id=1,
                     command_unreadable=True), **extra)


def proof(current, rows, refreshed, afterwards):
    """The stage's real route: a native list, the cached CIM answer, the refresh query, then `afterwards`, the
    native lists that follow (a list item that is an exception is raised)."""
    answer = dict(rows=rows, identities={pid: list(identity) for pid, identity in current.items()})
    with mock.patch.object(placement_proof, '_windows_processes', side_effect=[current, *afterwards]), \
            mock.patch.object(placement_proof, '_cleanup_cim', return_value=answer), \
            mock.patch.object(placement_proof, '_cim_rows', return_value=refreshed), \
            mock.patch.object(placement_proof, '_windows_cwd', return_value=None):
        return placement_proof.cleanup_processes(paths={'process_ignore': []}, windows=True)


class Rechecked(unittest.TestCase):
    def assert_clear(self, result):
        self.assertFalse(result.unknown)
        self.assertIsNone(result.reason)
        self.assertEqual(result.unreadable, [])
        self.assertFalse(result.busy(WORK))

    def assert_unknown(self, result, pids):
        self.assertTrue(result.unknown)
        self.assertEqual(result.reason, 'process visibility unavailable')
        self.assertEqual([row['pid'] for row in result.unreadable], pids)
        self.assertTrue(result.busy(WORK))

    def test_a_listed_process_gone_before_its_identity_was_read_is_not_unknown(self):
        # git.exe was in the native list, exited before CIM, and is absent from every later list.
        current = {'101': ['node.exe', 1], '7001': ['git.exe', 5]}
        self.assert_clear(proof(current, [NODE], [], [{'101': ['node.exe', 1]}, {'101': ['node.exe', 1]}]))

    def test_a_process_gone_before_its_cwd_was_read_is_not_unknown(self):
        # dotnet.exe's row came back, but its cwd could not be read because it had exited.
        current = {'101': ['node.exe', 1], '7002': ['dotnet.exe', 6]}
        rows = [NODE, unread(7002, 'dotnet.exe', same_user=True, command_unreadable=False, cmd='dotnet test')]
        self.assert_clear(proof(current, rows, [], [{'101': ['node.exe', 1]}]))

    def test_a_reused_pid_is_not_unknown(self):
        # pid 7001 now belongs to a process born later: the listed git.exe is gone.
        current = {'101': ['node.exe', 1], '7001': ['git.exe', 5]}
        later = {'101': ['node.exe', 1], '7001': ['conhost.exe', 9]}
        self.assert_clear(proof(current, [NODE], [], [later, later]))

    def test_a_process_still_alive_but_unreadable_stays_unknown(self):
        # csrss.exe in the user's session: access denied to its birth, owner and image, and still there.
        current = {'101': ['node.exe', 1], '900': ['csrss.exe', None]}
        rows = [NODE, unread(900, 'csrss.exe')]
        self.assert_unknown(proof(current, rows, [unread(900, 'csrss.exe')], [current, current]), [900])

    def test_a_still_listed_process_with_the_same_birth_stays_unknown(self):
        current = {'101': ['node.exe', 1], '7002': ['dotnet.exe', 6]}
        rows = [NODE, unread(7002, 'dotnet.exe', same_user=True, command_unreadable=False, cmd='dotnet test')]
        self.assert_unknown(proof(current, rows, [], [current]), [7002])

    def test_a_recheck_that_fails_stays_unknown(self):
        current = {'101': ['node.exe', 1], '7001': ['git.exe', 5]}
        for error in (OSError('no snapshot'), ValueError('bad')):
            with self.subTest(error):
                self.assert_unknown(proof(current, [NODE], [], [{'101': ['node.exe', 1]}, error]), [7001])


if __name__ == '__main__':
    unittest.main()
