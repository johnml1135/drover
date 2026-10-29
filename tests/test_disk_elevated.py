"""Elevated helpers need a proven executable location before cleanup can ignore them."""
import ctypes
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from drover import config, disk_paths, disk_worktrees, placement, placement_process, placement_proof


class ElevatedProcesses(unittest.TestCase):
    def proof(self, name, exe, *, same_user=True, policy=None, cwd=None, cmd=None):
        data = dict(sid='owner', processes=[dict(
            ProcessId=123456789, Name=name, ExecutablePath=None, CommandLine=cmd,
            SessionId=1, OwnerSid='owner' if same_user else None)])
        request = dict(paths={} if policy is None else dict(process_ignore=policy))
        with patch.object(placement_process.subprocess, 'run', return_value=SimpleNamespace(
                returncode=0, stdout=json.dumps(data))), \
                patch.object(placement_process, '_windows_image_path', return_value=exe) as query:
            proof = placement_proof.cleanup_processes(windows=True, request=request,
                cim=lambda: [dict(row, cwd=cwd) for row in placement_process._cim_rows()])
        return proof, request, query

    def test_elevated_system_helpers_allow_unrelated_worktree_evaluation(self):
        for name in ('taskhostw.exe', 'dllhost.exe', 'CTFMON.EXE'):
            for owner in (True, None):
                with self.subTest(name=name, owner=owner):
                    exe = 'C:\\Windows\\System32\\' + name
                    proof, request, query = self.proof(name, exe, same_user=owner)
                    self.assertFalse(proof.busy(r'H:\worktrees\candidate'))
                    self.assertIsNone(proof.reason)
                    self.assertEqual(proof.out_of_scope, 1)
                    self.assertEqual(request['_process_diagnostics'], {})
                    query.assert_called_once_with(123456789)

    def test_unknown_image_and_unproven_helper_paths_fail_closed_with_pid(self):
        for name, exe in (
                ('unknown.exe', None), ('unknown.exe', r'C:\Windows\System32\unknown.exe'),
                ('taskhostw.exe', None), ('dllhost.exe', ''), ('ctfmon.exe', None),
                ('taskhostw.exe', r'C:\Windows\System32\unknown.exe'),
                ('dllhost.exe', r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'),
                ('taskhostw.exe', r'H:\worktrees\candidate\taskhostw.exe'),
                ('taskhostw.exe', r'C:\Windows-lookalike\taskhostw.exe')):
            with self.subTest(name=name, exe=exe):
                proof, request, _ = self.proof(name, exe)
                self.assertTrue(proof.busy(r'H:\unrelated'))
                self.assertEqual(proof.reason, 'process visibility unavailable')
                row = request['_process_diagnostics'][(123456789, name)]
                self.assertEqual(row['owner_action'],
                    f'close {name} (pid 123456789) or add it to [disk_cleanup] process_ignore')

    def test_shells_need_owner_policy_even_with_verified_system_path(self):
        for name in ('powershell.exe', 'conhost.exe'):
            exe = 'C:\\Windows\\System32\\' + name
            with self.subTest(name=name):
                proof, _, _ = self.proof(name, exe)
                self.assertTrue(proof.busy(r'H:\unrelated'))
                proof, _, _ = self.proof(name, exe, policy=[name])
                self.assertFalse(proof.busy(r'H:\unrelated'))

    def test_verified_helper_still_retains_named_paths(self):
        proof, _, _ = self.proof('dllhost.exe', r'C:\Windows\System32\dllhost.exe',
                                 cmd=r'dllhost.exe H:\worktrees\candidate\input')
        self.assertTrue(proof.busy(r'H:\worktrees\candidate'))
        self.assertFalse(proof.busy(r'H:\unrelated'))

    def test_readable_cwd_does_not_hide_unreadable_command_line(self):
        proof, _, _ = self.proof('dllhost.exe', r'C:\Windows\System32\dllhost.exe',
                                 cwd=r'C:\Windows\System32')
        self.assertTrue(proof.busy(r'H:\unrelated'))
        self.assertEqual(proof.unreadable[0]['evidence'], 'command_line')

    def test_stages_four_and_five_can_continue_past_process_check(self):
        proof, _, _ = self.proof('taskhostw.exe', r'C:\Windows\System32\taskhostw.exe')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            request = dict(_processes=proof, now=1000, apply=False, drive=dict(level='hold'))
            machine = dict(repo=str(path / 'repo'), name='fixture')
            entry = dict(worktree=str(path), locked='windows')
            with patch.object(placement, 'cleanup_side', return_value='windows'), \
                    patch.object(placement, 'cleanup_contains', return_value=True), \
                    patch.object(disk_worktrees, 'safe_path', return_value=False):
                row = disk_worktrees.inspect(request, machine, entry)
            self.assertEqual(row['reason_code'], 'protected')
            with patch.object(placement, 'cleanup_side', return_value='windows'), \
                    patch.object(placement, 'cleanup_contains', return_value=True), \
                    patch.object(disk_paths, 'safe_path', return_value=True), \
                    patch.object(disk_worktrees, 'head_at', return_value='head'), \
                    patch.object(disk_worktrees, 'outputs_at', return_value=['bin']), \
                    patch('drover.ownership.read', return_value=None):
                evidence = disk_paths.output_evidence(request, machine, entry)
            self.assertEqual(evidence, (path, 'head', None, ['bin']))

    def test_prior_factory_lists_migrate_but_owner_edits_survive(self):
        current = placement_proof.default_process_ignore()
        previous = [name for name in current if name not in ('taskhostw.exe', 'dllhost.exe')]
        for old in (previous, [name for name in previous if name != 'remoting_desktop.exe']):
            with self.subTest(old=old):
                migrated = config.migrate_disk_config(dict(disk_cleanup=dict(process_ignore=old)))
                self.assertEqual(migrated['disk_cleanup']['process_ignore'], current)
                self.assertIn('taskhostw.exe', current)
                self.assertIn('dllhost.exe', current)
                self.assertEqual(config.migrate_disk_config(migrated), migrated)
        for edited in ([], ['taskhostw.exe'], previous + ['owner-helper']):
            migrated = config.migrate_disk_config(dict(disk_cleanup=dict(process_ignore=edited)))
            self.assertEqual(migrated['disk_cleanup']['process_ignore'], edited)


class ImagePathQuery(unittest.TestCase):
    def test_limited_query_success_denied_and_failure_close_only_open_handles(self):
        for opened, readable in ((False, False), (True, False), (True, True)):
            with self.subTest(opened=opened, readable=readable):
                kernel = Mock()
                handle = 0x123456789
                kernel.OpenProcess.return_value = handle if opened else None

                def query(actual_handle, flags, image, size):
                    self.assertEqual((actual_handle, flags), (handle, 0))
                    self.assertEqual(size._obj.value, 32768)
                    image.value = r'C:\Windows\System32\taskhostw.exe'
                    return readable

                kernel.QueryFullProcessImageNameW.side_effect = query
                with patch.object(ctypes, 'WinDLL', return_value=kernel, create=True):
                    result = placement_process._windows_image_path(77)
                self.assertEqual(result, r'C:\Windows\System32\taskhostw.exe' if readable else None)
                kernel.OpenProcess.assert_called_once_with(0x1000, False, 77)
                if opened:
                    kernel.CloseHandle.assert_called_once_with(handle)
                else:
                    kernel.QueryFullProcessImageNameW.assert_not_called()
                    kernel.CloseHandle.assert_not_called()

    def test_readable_cim_executable_needs_no_fallback(self):
        data = dict(sid='owner', processes=[dict(ProcessId=123456789, Name='tool.exe',
            ExecutablePath=r'H:\worktrees\tool.exe', CommandLine=None, OwnerSid='owner')])
        with patch.object(placement_process.subprocess, 'run', return_value=SimpleNamespace(
                returncode=0, stdout=json.dumps(data))), \
                patch.object(placement_process, '_windows_image_path') as query:
            rows = placement_process._cim_rows()
        query.assert_not_called()
        self.assertEqual(rows[0]['exe'], r'H:\worktrees\tool.exe')
        self.assertTrue(rows[0]['command_unreadable'])

    @unittest.skipUnless(os.name == 'nt', 'native Windows image query')
    def test_native_query_reads_current_process_image(self):
        self.assertEqual(os.path.normcase(placement_process._windows_image_path(os.getpid())),
                         os.path.normcase(sys.executable))
