"""The budget purge leaves alone whatever a live process holds, on Windows and in WSL."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from drover import disk_cleanup, disk_state, placement, placement_proof
from drover.disk_stage import StageRequest
from tests.disk_budget_fixture import DAY, LocalSide, live_row, row
from tests.disk_fixture import served


class WindowsProcesses(LocalSide):
    """The WSL worker cannot see Windows processes, so the coordinator looks and the purge keeps what it found."""
    SHARE = r'\\wsl.localhost\Disk Test Distro\home\me\wt'

    def relay(self, tree):
        """The worker's call as it travels, with only the spelling of the fixture's worktree changed: the fixture's
        trees are folders on this host, a real one is the same /home path on both sides of the call."""
        sent = []
        worker = self.runtime(real=True)

        def call(side, request):
            if request.get('budget'):
                sent.append(request['budget']['windows_paths'])
                named = [str(tree) + p.removeprefix('/home/me/wt') for p in request['budget']['windows_paths']]
                request = dict(request, budget=dict(request['budget'], windows_paths=named))
            return worker.call(side, StageRequest.from_wire(request))
        return call, sent

    def test_windows_process_in_a_wsl_worktree_keeps_its_paths(self):
        used, idle = self.output('open-in-windows', 9 * DAY), self.output('idle', 8 * DAY)
        self.windows_rows.append(dict(pid=8, name='devenv.exe', same_user=True, cwd=self.SHARE + r'\src', cmd='devenv.exe'))
        self.approve_report()
        self.claim()
        call, sent = self.relay(used.parents[1])
        result = self.run_cleanup(apply=True, mode='triggered', handler=call)
        self.assertEqual(sent, [['/home/me/wt/src']])
        self.assertTrue(used.exists())
        self.assertFalse(idle.exists())
        self.assertEqual(self.receipt()['budget'], 'shortfall')
        self.assertTrue([i for i in result['items'] if 'windows process using worktree' in i.get('error', '')])

    def test_windows_command_line_naming_a_wsl_worktree_keeps_its_paths(self):
        used = self.output('built-from-windows', 9 * DAY)
        self.windows_rows.append(dict(pid=8, name='msbuild.exe', same_user=True, cwd=r'C:\work',
                                      cmd=f'msbuild.exe "{self.SHARE}\\app.sln"'))
        self.approve_report()
        self.claim()
        call, sent = self.relay(used.parents[1])
        self.run_cleanup(apply=True, mode='triggered', handler=call)
        self.assertEqual(sent, [['/home/me/wt/app.sln']])
        self.assertTrue(used.exists())

    def test_unknown_snapshot_purges_nothing_with_a_reason(self):
        path = self.output('idle', 9 * DAY)
        self.windows_rows = []
        self.windows_failure = dict(rows=[], error='timed out', reason_code='process_query_timeout')
        result = self.purge()
        self.assertTrue(path.exists())
        self.assertEqual(self.receipt()['budget'], 'processes_unknown')
        self.assertFalse(self.receipt()['applied'])
        self.assertTrue([i for i in result['items'] if i.get('reason_code') == 'budget_processes_unknown'])
        self.assertIsNone(self.episode()['claim'])

    def test_process_it_cannot_read_makes_the_snapshot_unknown(self):
        path = self.output('idle', 9 * DAY)
        self.windows_rows.append(dict(pid=9, name='secret.exe', same_user=True, cwd=None, cmd='secret.exe'))
        result = self.purge()
        self.assertTrue(path.exists())
        rows = [i for i in result['items'] if i.get('reason_code') == 'budget_processes_unknown']
        self.assertEqual([r['count'] for r in rows], [1])

    def test_failed_snapshot_purges_nothing(self):
        path = self.output('idle', 9 * DAY)
        with patch.object(placement, 'cleanup_windows_proof', side_effect=OSError('no cim')):
            self.purge()
        self.assertTrue(path.exists())
        self.assertEqual(self.receipt()['budget'], 'processes_unknown')

    def test_coordinator_off_windows_purges_nothing(self):
        path = self.output('idle', 9 * DAY)
        with patch.object(placement_proof, 'IS_WINDOWS', False):
            self.purge()
        self.assertTrue(path.exists())
        self.assertEqual(self.receipt()['budget'], 'processes_unknown')

    def test_worker_refuses_a_budget_without_a_windows_answer(self):
        path = self.output('idle', 9 * DAY)
        request = dict(stage=5, mode='triggered', apply=True, now=self.now, repos=disk_cleanup._repos(self.cfg),
                       drive=dict(path=str(self.root), level='ok'), paths=dict(temp_roots=[], report_roots=[], caches=[]),
                       state_dir=str(self.state), budget=dict(target_gb=225.0))
        proof = SimpleNamespace(busy=lambda path: False)
        with patch.object(placement, 'cleanup_processes', return_value=proof), \
                patch.object(placement, 'physical_disk_path', return_value=str(self.root)):
            answer = served(request)
        self.assertEqual(answer['budget']['status'], 'processes_unknown')
        self.assertTrue(path.exists())


class LiveSnapshot(LocalSide):
    """The snapshot of 2026-10-31: session 1 processes with no birth, exe, owner, cwd or command line, the set that
    stopped every purge. Every row goes through the refresh path, which a fake with a birth time never reaches."""
    BLOCKERS = (('csrss.exe', 1720), ('winlogon.exe', 1784), ('fontdrvhost.exe', 1008), ('dwm.exe', 2252),
                ('NVDisplay.Container.exe', 6764), ('RtkAudUService64.exe', 10060), ('WacomHost.exe', 14048),
                ('Wacom_Tablet.exe', 14628))

    def setUp(self):
        super().setUp()
        self.windows_rows = [live_row(pid, name, 1) for name, pid in self.BLOCKERS] + [live_row(2032, 'svchost.exe', 0)]
        for context in (patch.object(placement_proof, '_windows_processes',
                                     side_effect=lambda: {str(r['pid']): [r['name'], None] for r in self.windows_rows}),
                        patch.object(placement_proof, '_cim_rows',
                                     side_effect=lambda timeout=240, pids=None: [dict(r) for r in self.windows_rows]),
                        patch.object(placement_proof, '_windows_cwd', return_value=None)):
            context.start()
            self.addCleanup(context.stop)

    def outbox(self):
        return [entry['notice'] for entry in disk_state.read(self.state).get('outbox', {}).values()
                if entry['notice']['kind'] == 'wsl_budget']

    def test_named_system_processes_no_longer_stop_the_purge(self):
        path = self.output('idle', 9 * DAY)
        self.purge()
        self.assertFalse(path.exists())
        self.assertEqual(self.receipt()['budget'], 'met')
        self.assertEqual(self.outbox(), [])

    def test_an_unnamed_unreadable_process_still_blocks_and_is_named(self):
        path = self.output('idle', 9 * DAY)
        self.windows_rows.append(live_row(25672, 'sleep.exe', 1))
        result = self.purge()
        self.assertTrue(path.exists())
        self.assertEqual(self.receipt()['budget'], 'processes_unknown')
        row = next(i for i in result['items'] if i.get('reason_code') == 'budget_processes_unknown')
        self.assertEqual([p['name'] for p in row['processes']], ['sleep.exe'])
        self.assertIn('sleep.exe', self.outbox()[0]['body'])

    def test_the_owner_naming_it_clears_it(self):
        path = self.output('idle', 9 * DAY)
        self.windows_rows.append(live_row(25672, 'sleep.exe', 1))
        self.cfg['disk_cleanup']['process_ignore'] = [*placement_proof.PROCESS_IGNORE, 'SLEEP.EXE']
        self.purge()
        self.assertFalse(path.exists())

    def test_a_name_does_not_clear_a_readable_image_elsewhere(self):
        path = self.output('idle', 9 * DAY)
        self.windows_rows[0].update(exe=r'C:\Users\me\csrss.exe')
        self.purge()
        self.assertTrue(path.exists())
        self.assertEqual(self.receipt()['budget'], 'processes_unknown')

    def test_a_reused_pid_is_not_cleared_by_the_old_name(self):
        def renamed(timeout=240, pids=None):
            return [dict(r, name='devenv.exe') if r['pid'] == 1720 else dict(r) for r in self.windows_rows]
        self.output('idle', 9 * DAY)
        with patch.object(placement_proof, '_cim_rows', side_effect=renamed):
            self.purge()
        self.assertEqual(self.receipt()['budget'], 'processes_unknown')

    def test_a_cleared_process_still_counts_as_path_evidence(self):
        self.windows_rows[0].update(cmd=r'csrss.exe --root=\\wsl$\Ubuntu\home\me\wt')
        proof = placement_proof._CleanupProcesses(windows=True, budget=True)
        self.assertEqual(proof.wsl_paths('Ubuntu'), ['/home/me/wt'])

    def test_a_failed_refresh_query_clears_nothing(self):
        self.output('idle', 9 * DAY)
        with patch.object(placement_proof, '_cim_rows', side_effect=OSError('no cim')):
            self.purge()
        self.assertEqual(self.receipt()['budget'], 'processes_unknown')

    def test_ordinary_stages_keep_their_rule(self):
        proof = placement_proof._CleanupProcesses(windows=True, paths={})
        self.assertTrue(proof.unknown)
        self.assertEqual(sorted(row['name'] for row in proof.unreadable), sorted(name for name, _ in self.BLOCKERS))
        self.assertTrue(proof.busy('/anywhere'))


class WslProcesses(unittest.TestCase):
    """The WSL side, from a /proc tree shaped like the distro's: on 2026-10-31 systemd --user and (sd-pam) are the only
    processes of this user with no cwd or exe, as for any non-dumpable daemon (ssh-agent, gpg-agent)."""
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.proc = Path(folder.name)
        uid = patch.object(os, 'getuid', return_value=1000, create=True)
        uid.start()
        self.addCleanup(uid.stop)

    def process(self, pid, name):
        entry = self.proc / str(pid)
        entry.mkdir()
        (entry / 'status').write_text(f'Name:\t{name}\nUid:\t1000\t1000\t1000\t1000\n')
        (entry / 'cmdline').write_bytes(name.encode() + b'\0')

    def proof(self, *ignored, budget):
        request = dict(paths=dict(process_ignore=list(ignored)), **(dict(budget=dict(target_gb=225)) if budget else {}))
        return placement_proof._CleanupProcesses(proc_root=self.proc, windows=False, **request)

    def test_a_named_process_clears_for_a_purge_as_for_any_stage(self):
        self.process(364, 'systemd')
        self.process(366, '(sd-pam)')
        for budget in (True, False):
            self.assertFalse(self.proof('systemd', '(sd-pam)', budget=budget).unknown)

    def test_an_unnamed_process_blocks_and_is_named(self):
        self.process(900, 'ssh-agent')
        proof = self.proof('systemd', budget=True)
        self.assertTrue(proof.busy('/home/me/wt'))
        self.assertEqual([row['name'] for row in proof.unreadable], ['ssh-agent'])

    def test_a_long_name_matches_for_a_purge_though_linux_cut_it(self):
        self.process(901, 'xdg-document-po')
        self.assertFalse(self.proof('xdg-document-portal', budget=True).unknown)
        self.assertTrue(self.proof('xdg-document-portal.x', budget=False).unknown)


class WslPaths(unittest.TestCase):
    def proof(self, *rows):
        return placement_proof._CleanupProcesses(windows=True, cim=lambda: [
            dict(pid=i + 1, name='tool.exe', same_user=True, cmd='tool.exe', cwd=r'C:\work', **row)
            if 'cmd' not in row and 'cwd' not in row else dict(dict(pid=i + 1, name='tool.exe', same_user=True,
                                                                     cmd='tool.exe', cwd=r'C:\work'), **row)
            for i, row in enumerate(rows)])

    def test_every_spelling_of_this_distro(self):
        proof = self.proof(dict(cwd=r'\\wsl$\Ubuntu\home\a'), dict(cwd='\\\\wsl.localhost\\UBUNTU\\home\\b\\'),
                           dict(cwd=r'\\?\UNC\wsl.localhost\Ubuntu\home\c'), dict(cwd='//wsl.localhost/Ubuntu/home/d'),
                           dict(cmd='wsl.exe --cd /home/e'), dict(cmd=r'tool.exe --path=\\wsl$\Ubuntu\home\f'))
        self.assertEqual(proof.wsl_paths('Ubuntu'), ['/home/a', '/home/b', '/home/c', '/home/d', '/home/e', '/home/f'])

    def test_linux_case_is_kept(self):
        self.assertEqual(self.proof(dict(cwd=r'\\wsl$\Ubuntu\Home\Mixed')).wsl_paths('Ubuntu'), ['/Home/Mixed'])

    def test_other_distros_and_windows_paths_are_left_out(self):
        proof = self.proof(dict(cwd=r'\\wsl$\Debian\home\a'), dict(cwd=r'C:\home\b'), dict(cwd='/mnt/c/home/c'))
        self.assertEqual(proof.wsl_paths('Ubuntu'), [])

    def test_unknown_snapshot_has_no_answer(self):
        def fail():
            raise OSError('no cim')
        self.assertIsNone(placement_proof._CleanupProcesses(windows=True, cim=fail).wsl_paths('Ubuntu'))


if __name__ == '__main__':
    unittest.main()
