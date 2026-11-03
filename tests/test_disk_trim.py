import subprocess
import sys
from unittest.mock import patch
from tests.disk_fixture import DiskCase, GIB, empty_result, staged
from tests.fakeci import FakeCommands, process_options


class Trim(DiskCase):
    def test_root_argv_and_retrim_threshold(self):
        from drover import placement
        with patch.object(placement, 'IS_WINDOWS', True) as _placement_patch_1, patch('drover.placement_exec.IS_WINDOWS', _placement_patch_1), patch('drover.placement_git.IS_WINDOWS', _placement_patch_1), patch('drover.placement_host.IS_WINDOWS', _placement_patch_1), patch('drover.placement_paths.IS_WINDOWS', _placement_patch_1), patch('drover.placement_process.IS_WINDOWS', _placement_patch_1), patch('drover.placement_proof.IS_WINDOWS', _placement_patch_1), patch('drover.placement_tree.IS_WINDOWS', _placement_patch_1), patch('drover.placement_worker.IS_WINDOWS', _placement_patch_1):
            argv = placement.trim_argv()
        self.assertEqual(argv[0].lower().removesuffix('.exe'), 'wsl')
        self.assertEqual(argv[argv.index('-u') + 1], 'root')
        self.assertEqual(argv[argv.index('-d') + 1], 'Disk Test Distro')
        self.assertEqual(argv[-4:], ['--', 'fstrim', '-v', '/'])
        self.assertFalse(any(word in argv for word in ('sudo', 'RunAs', '-Verb')))
        for reclaimed, retrims in ((GIB - 1, 1), (GIB, 2)):
            with self.subTest(reclaimed=reclaimed):
                def stage(side, request):
                    result = empty_result()
                    if side == 'wsl' and request['stage'] == 2:
                        result['reclaimed_bytes'] = reclaimed
                    return result
                self.approve_report()
                self.calls.clear()
                self.run_cleanup(apply=True, real=False, handler=stage)
                self.assertEqual(sum(side == 'windows' and request['stage'] == 1
                                     for side, request in self.calls), retrims)

    def test_a_failed_stage_that_freed_a_gib_still_sends_the_trim(self):
        """Ruling (Task 33): a failed or timed-out stage reports its last `reclaimed` total, so blocks it really
        freed are handed back to the host; one that freed less than 1 GiB sends no trim."""
        for reclaimed, retrims in ((GIB - 1, 1), (GIB, 2)):
            with self.subTest(reclaimed=reclaimed):
                def stage(side, request):
                    if side == 'wsl' and request['stage'] == 2:      # cleanup_call's answer for a timed-out worker
                        return dict(empty_result(), errors=[dict(reason_code='worker_timeout', termination_confirmed=True)],
                                    reclaimed_bytes=reclaimed, complete=False, partial=True)
                    return empty_result()
                self.approve_report()
                self.calls.clear()
                self.run_cleanup(apply=True, real=False, handler=stage)
                self.assertEqual(sum(side == 'windows' and request['stage'] == 1
                                     for side, request in self.calls), retrims)

    def test_cache_command_runs_with_its_literal_argv_and_cwd(self):
        """A declared cache command reaches `process` word for word, in its cwd, with no shell."""
        from drover import process
        from drover.disk_paths import caches
        literal = ['space value', 'line\nbreak', 'a;$(literal)']
        argv = [sys.executable, str(self.root / 'side cache.py'), *literal]
        request = dict(stage=3, apply=True, now=self.now,
                       drive=dict(name='test-drive', path=str(self.root), free_gb=80., level='queue'),
                       repos=[], state_dir=str(self.state), tasks={},
                       paths=dict(temp_roots=[], report_roots=[], caches=[dict(
                           argv=argv, cwd=str(self.temp_root), placement=self.side)]))
        fake = FakeCommands(self.root / 'fake')
        fake.expect(argv, output='cleaned', **process_options(120, cwd=str(self.temp_root)))
        with fake.patch(process):
            result = staged(caches, request)
        self.assertEqual(result['errors'], [])
        self.assertEqual([(row['action'], row['returncode'], row['output']) for row in result['items']],
                         [('executed', 0, 'cleaned')])

    def test_trim_failure_and_timeout_keep_trim_unavailable(self):
        from drover import disk_side, placement, process
        argv = ['fstrim', '-v', '/']
        fake = FakeCommands(self.root / 'fake')
        fake.expect(argv, returncode=1, stderr_output='fstrim: not permitted', **process_options(120))
        fake.expect(argv, error=subprocess.TimeoutExpired(argv, 120), **process_options(120))
        fake.expect(argv, error=FileNotFoundError(2, 'No such file or directory'), **process_options(120))
        request = dict(stage=1, apply=True)
        vhdx = dict(path='C:/wsl/ext4.vhdx', action='report', reason_code='wsl_vhdx', bytes=1)
        with fake.patch(process), patch.object(placement, 'trim_argv', return_value=argv), \
                patch.object(placement, 'cleanup_vhdx_report', return_value=[vhdx]):
            answers = [staged(disk_side.trim, request) for _ in range(3)]
        failed, timed_out, missing = (answer['errors'] for answer in answers)
        self.assertEqual([answer['items'] for answer in answers], [[vhdx]] * 3)     # the report crosses on the run
        self.assertEqual(failed, [dict(reason_code='trim_unavailable', output='fstrim: not permitted')])
        for errors in (timed_out, missing):
            self.assertEqual([error['reason_code'] for error in errors], ['trim_unavailable'])
            self.assertTrue(errors[0]['error'])

    def test_retrim_returns_to_windows_before_next_destructive_stage(self):
        def stage(side, request):
            answer = empty_result()
            if side == 'wsl' and request['stage'] == 2:
                answer['reclaimed_bytes'] = GIB
            return answer
        self.approve_report()
        self.calls.clear()
        self.run_cleanup(apply=True, handler=stage)
        steps = [(side, r['stage']) for side, r in self.calls]
        reclaimed = steps.index(('wsl', 2))
        self.assertEqual(steps[reclaimed + 1], ('windows', 1))
