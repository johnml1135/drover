"""Windows process proof: unreadable creation time of a foreign process must not block every worktree.

Process evidence is a fake CIM/native table at the placement seam; no subprocess runs.
"""
from unittest.mock import patch

from drover import disk_state, placement
from tests.disk_fixture import DiskCase

SYSTEM = r'C:\Windows\System32\svchost.exe'


class ForeignUnreadableBirth(DiskCase):
    def proof(self, row, cwd=None, native=None, birth=None, after=None):
        request = dict(paths={}, state_dir=str(self.state), _inventory_id='foreign')
        disk_state.save_file(self.state, 'processes-foreign.json', dict(rows=[], identities={}))
        pid = str(row['pid'])
        with patch.object(placement, '_windows_processes', side_effect=[{pid: [native or row['name'], birth]}, {pid: [native or row['name'], after]}]) as native, \
                patch('drover.placement_proof._windows_processes', native), \
                patch('drover.placement_proof._windows_cwd', return_value=cwd), \
                patch('drover.placement_process._cim_rows', return_value=[row]) as cim, \
                patch('drover.placement_proof._cim_rows', cim):
            return placement.cleanup_processes(windows=True, request=request)

    def test_foreign_service_with_unreadable_start_time_does_not_block(self):
        proof = self.proof(dict(pid=88, name='svchost.exe', same_user=False, exe=SYSTEM, cmd='', session_id=0))
        self.assertFalse(proof.busy(r'H:\repos\candidate'))
        self.assertEqual(proof.out_of_scope, 1)
        self.assertEqual(proof.unreadable, [])

    def test_session_zero_without_owner_does_not_block(self):
        proof = self.proof(dict(pid=88, name='svchost.exe', same_user=None, exe=None, cmd='', session_id=0))
        self.assertFalse(proof.busy(r'H:\repos\candidate'))

    def test_same_user_unreadable_process_still_blocks(self):
        proof = self.proof(dict(pid=77, name='python.exe', same_user=True, exe=r'C:\tools\python.exe', cmd=''))
        self.assertTrue(proof.busy(r'H:\repos\candidate'))
        self.assertEqual(proof.unreadable[0]['evidence'], 'process_identity_unavailable')

    def test_foreign_process_with_cwd_inside_worktree_still_blocks_it_only(self):
        proof = self.proof(dict(pid=88, name='svchost.exe', same_user=False, exe=SYSTEM, cmd=''),
                           cwd=r'H:\repos\candidate\src')
        self.assertTrue(proof.busy(r'H:\repos\candidate'))
        self.assertFalse(proof.busy(r'H:\repos\other'))

    def test_row_without_owner_information_stays_unknown(self):
        proof = self.proof(dict(pid=77, name='mystery.exe', same_user=None, exe=None, cmd='', session_id=1))
        self.assertTrue(proof.busy(r'H:\repos\candidate'))
        self.assertEqual(proof.reason, 'process visibility unavailable')

    def test_reused_pid_with_different_image_stays_unknown(self):
        proof = self.proof(dict(pid=88, name='svchost.exe', same_user=False, exe=SYSTEM, cmd=''), native='git.exe')
        self.assertTrue(proof.busy(r'H:\repos\candidate'))

    def test_ignored_image_with_verified_exe_is_out_of_scope(self):
        proof = self.proof(dict(pid=9, name='csrss.exe', same_user=None, exe=r'C:\Windows\System32\csrss.exe', cmd=''))
        self.assertFalse(proof.busy(r'H:\repos\candidate'))
        self.assertEqual(proof.out_of_scope, 1)

    def test_ignored_image_without_verified_exe_stays_unknown(self):
        proof = self.proof(dict(pid=9, name='csrss.exe', same_user=None, exe=None, cmd=''))
        self.assertTrue(proof.busy(r'H:\repos\candidate'))

    def test_changed_birth_stays_unknown(self):
        proof = self.proof(dict(pid=88, name='svchost.exe', same_user=False, exe=SYSTEM, cmd=''), birth=20, after=21)
        self.assertTrue(proof.busy(r'H:\repos\candidate'))
