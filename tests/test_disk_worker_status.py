"""A worker that dies at startup says how: its exit status and stderr reach the report. The worker's start and its
startup proof reach the one fake at `process`, with the console flag."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from drover import disk_runtime, placement, placement_exec, process
from drover.disk_stage import StageRequest, read_rows
from tests.fakeci import FakeCommands, process_options

TOKEN = 't0k3n'
CODE = ('import json,sys;sys.path.insert(0,sys.argv[1]);'
        'from drover.placement import cleanup_worker;cleanup_worker(json.load(sys.stdin))')
ROOT = str(Path(disk_runtime.__file__).resolve().parents[1])
NATIVE = 'windows' if placement.IS_WINDOWS else 'wsl'    # this host's own side: no path translation


class WorkerExitStatusTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name) / 'state'
        self.state.mkdir()
        self.fake = FakeCommands(Path(temp.name) / 'fake')
        job = SimpleNamespace(terminate=lambda: True, close=lambda: None)
        for item in (mock.patch('subprocess.run', side_effect=AssertionError('real process')),
                     mock.patch('uuid.uuid4', return_value=SimpleNamespace(hex=TOKEN)),
                     mock.patch.object(placement, '_CleanupJob', return_value=job),
                     mock.patch.object(placement, 'recover_ci_children', return_value=True)):
            item.start()
            self.addCleanup(item.stop)

    def request(self, **paths):
        return StageRequest(stage=1, side=None, mode=None, apply=False, now=time.time(), state_dir=str(self.state),
                            repos=(), tasks={}, drive=dict(path=str(self.state)), policy=paths)

    def expect_worker(self, timeout=1800, **answer):
        self.fake.expect([sys.executable, '-c', CODE, ROOT, TOKEN], **answer,
                         **process_options(timeout, env={**os.environ, 'DROVER_RUN_ID': TOKEN}, input=mock.ANY))

    def call(self, returncode, stderr):
        self.expect_worker(returncode=returncode, stderr_output=stderr)
        with self.fake.patch(process):
            return disk_runtime.cleanup_call(NATIVE, self.request())['errors'][0]

    def test_silent_startup_death_reports_its_exit_status(self):
        error = self.call(0xC0000142, '')
        self.assertEqual(error['reason_code'], 'worker_error')
        self.assertEqual(error['exit_status'], 0xC0000142)
        self.assertIn('0xc0000142', error['error'].lower())

    def test_stderr_is_kept_beside_the_status(self):
        error = self.call(1, 'ImportError: boom')
        self.assertIn('ImportError: boom', error['error'])
        self.assertEqual(error['exit_status'], 1)
        self.assertNotEqual(error['error'].strip(), '')

    def test_cleanup_worker_starts_with_the_console_flag(self):
        """A console that died with the shell must reach neither the worker (disk_runtime:92) nor its startup proof
        (:141): both go through `process`, which adds the flag."""
        self.fake.expect(['wsl.exe', '-d', 'Dist', '--', 'env', 'DROVER_RUN_ID=' + TOKEN, 'python3', '-c', CODE,
                          '/w/' + ROOT, TOKEN], returncode=3,
                         **process_options(11, env={**os.environ, 'DROVER_RUN_ID': TOKEN}, input=mock.ANY))
        self.fake.expect(['wsl.exe', '-d', 'Dist', '--exec', 'python3', '-c', placement._CLEANUP_STARTUP_PROOF, TOKEN],
                         **process_options(30))
        with self.fake.patch(process), mock.patch.object(placement, 'IS_WINDOWS', True), \
                mock.patch.object(placement, 'to_wsl_path', lambda p: '/w/' + str(p)), \
                mock.patch.object(placement, 'cleanup_path_side', lambda p: 'wsl'), \
                mock.patch.object(placement, 'distro', lambda: 'Dist'), \
                mock.patch.object(placement, 'detect_vhdx_drives', lambda: {}), \
                mock.patch.object(placement_exec, '_settings', lambda: {'wsl_distro': 'Dist'}):
            answer = disk_runtime.cleanup_call('wsl', self.request(worker_timeout_s=11))
        self.assertEqual(answer['errors'][0]['reason_code'], 'worker_error')
        self.assertTrue(answer['errors'][0]['termination_confirmed'])
        for _, options in self.fake.calls:
            self.assertEqual({key: options.get(key) for key in process.NO_WINDOW}, dict(process.NO_WINDOW))
        if sys.platform == 'win32':
            self.assertEqual(process.NO_WINDOW, {'creationflags': 0x08000000})
        self.assertEqual(json.loads(self.fake.calls[0][1]['input'])['_worker_id'], TOKEN)

    def claim(self):
        claim = self.state / 'disk' / f'worker-{TOKEN}.json'
        claim.parent.mkdir(exist_ok=True)
        claim.write_text(json.dumps(dict(pid=4242, run_id=TOKEN)), encoding='utf-8')

    @contextlib.contextmanager
    def termination(self, proofs, confirmed):
        """The termination proof this host takes, the job's or the worker group's, answering `confirmed`."""
        def prove(*_):
            proofs.append('proved')
            return confirmed
        job = SimpleNamespace(terminate=prove, close=lambda: None)
        with mock.patch.object(placement, '_CleanupJob', return_value=job), \
                mock.patch.object(placement, 'recover_ci_children', prove):
            yield

    def saved(self):
        return json.loads((self.state / 'disk' / f'worker-error-{TOKEN}.json').read_text(encoding='utf-8'))

    def test_timeout_returns_rows_plus_todays_error_row(self):
        """A timed-out worker that did claim its work: the rows that arrived are kept, a partial last line is not,
        and the error row keeps today's reason, output and stderr."""
        self.claim()
        out = lines(row('/w/a'), row('/w/b')) + b'{"v":1,"row":{"pa'
        self.expect_worker(11, error=subprocess.TimeoutExpired('worker', 11, output=out, stderr=b'late'))
        with self.fake.patch(process):
            answer = disk_runtime.cleanup_call(NATIVE, self.request(worker_timeout_s=11))
        error = dict(reason_code='worker_timeout', output=out.decode(), error='late', termination_confirmed=True,
                     work_started=True)
        self.assertEqual(answer, dict(items=[row('/w/a'), row('/w/b')], errors=[error], reclaimed_bytes=0,
                                      peak_intermediate_bytes=0, budget=None, complete=False, partial=True))
        self.assertEqual(self.saved(), dict(error, rows=dict(items=[row('/w/a'), row('/w/b')], errors=[],
                                                             reclaimed_bytes=0)))

    def test_partial_answer_does_not_set_answer_so_evidence_branch_runs(self):
        """Lines that arrived are not an answer: termination is still proven and the error row still written."""
        self.claim()
        proofs = []
        self.expect_worker(11, error=subprocess.TimeoutExpired('worker', 11, output=lines(row('/w/a')), stderr=b''))
        with self.fake.patch(process), self.termination(proofs, True):
            answer = disk_runtime.cleanup_call(NATIVE, self.request(worker_timeout_s=11))
        self.assertEqual(proofs, ['proved'])
        self.assertEqual([e['reason_code'] for e in answer['errors']], ['worker_timeout'])
        self.assertEqual(self.saved()['reason_code'], 'worker_timeout')

    def test_rows_are_saved_whole_in_their_own_key_of_worker_error(self):
        """Not through the 4,000-character `output` cut."""
        self.claim()
        rows = [row('/w/' + 'x' * 100 + str(n)) for n in range(60)]
        out = lines(*rows, reclaimed=70)
        self.assertGreater(len(out), 4000)
        self.expect_worker(11, error=subprocess.TimeoutExpired('worker', 11, output=out, stderr=b''))
        with self.fake.patch(process):
            answer = disk_runtime.cleanup_call(NATIVE, self.request(worker_timeout_s=11))
        saved = self.saved()
        self.assertEqual(saved['rows'], dict(items=rows, errors=[], reclaimed_bytes=70))
        self.assertEqual(len(saved['output']), 4000)
        self.assertEqual((answer['items'], answer['reclaimed_bytes']), (rows, 70))

    def test_unconfirmed_termination_still_raises_with_rows_saved(self):
        self.claim()
        self.expect_worker(11, error=subprocess.TimeoutExpired('worker', 11, output=lines(row('/w/a')), stderr=b''))
        with self.fake.patch(process), self.termination([], False), \
                self.assertRaises(placement.CleanupWorkerError) as raised:
            disk_runtime.cleanup_call(NATIVE, self.request(worker_timeout_s=11))
        evidence = json.loads(str(raised.exception))
        self.assertEqual((evidence['reason_code'], evidence['termination_confirmed']), ('worker_timeout', False))
        self.assertEqual(self.saved()['rows']['items'], [row('/w/a')])

    def test_nonzero_exit_with_an_end_line_keeps_rows_and_complete_false(self):
        """An `end` line from a worker that then exited non-zero: the rows stand, the stage is neither complete nor
        stamped, and today's worker_failed row says why."""
        self.claim()
        self.expect_worker(returncode=1, output=lines(row('/w/a'), end=dict(reclaimed_bytes=5)), stderr_output='boom')
        with self.fake.patch(process):
            answer = disk_runtime.cleanup_call(NATIVE, self.request())
        self.assertEqual((answer['items'], answer['reclaimed_bytes']), ([row('/w/a')], 5))
        self.assertEqual((answer['complete'], answer['partial']), (False, True))
        [error] = answer['errors']
        self.assertEqual((error['reason_code'], error['exit_status']), ('worker_failed', 1))
        self.assertIn('boom', error['error'])
        self.assertEqual(self.saved()['rows']['items'], [row('/w/a')])

    def test_a_complete_answer_comes_from_the_lines(self):
        self.claim()
        out = lines(row('/w/a'), error=dict(reason_code='stage_error'),
                    end=dict(reclaimed_bytes=9, partial=False, peak_intermediate_bytes=4, budget=None))
        self.expect_worker(output=out)
        with self.fake.patch(process):
            answer = disk_runtime.cleanup_call(NATIVE, self.request())
        self.assertEqual(answer, dict(items=[row('/w/a')], errors=[dict(reason_code='stage_error')], reclaimed_bytes=9,
                                      peak_intermediate_bytes=4, budget=None, complete=True, partial=False))
        self.assertFalse((self.state / 'disk' / f'worker-error-{TOKEN}.json').exists())

    def test_exit_zero_without_an_end_line_is_a_worker_failure(self):
        self.claim()
        self.expect_worker(output=lines(row('/w/a')))
        with self.fake.patch(process):
            answer = disk_runtime.cleanup_call(NATIVE, self.request())
        self.assertEqual(answer['items'], [row('/w/a')])
        self.assertEqual([e['reason_code'] for e in answer['errors']], ['worker_failed'])
        self.assertEqual((answer['complete'], answer['partial']), (False, True))

    def test_the_worker_reads_todays_wire_keys(self):
        self.claim()
        self.expect_worker(11, output=lines(end=dict(reclaimed_bytes=0)))
        with self.fake.patch(process):
            disk_runtime.cleanup_call(NATIVE, self.request(worker_timeout_s=11, cpu_limit=2))
        sent = json.loads(self.fake.calls[0][1]['input'])
        self.assertEqual((sent['_worker_id'], sent['side'], sent['paths'], sent['state_dir']),
                         (TOKEN, NATIVE, dict(worker_timeout_s=11, cpu_limit=2), str(self.state)))


def row(path):
    return dict(path=path, action='keep', reason_code='unsettled', owner=None, files=1, age_s=5, bytes=10)


def lines(*rows, error=None, reclaimed=None, end=None):
    """A worker's stdout: one JSON line per row, then any error, the reclaimed total and the end line."""
    values = [dict(v=1, row=r) for r in rows]
    values += [dict(v=1, error=error)] if error else []
    values += [dict(v=1, reclaimed=reclaimed)] if reclaimed is not None else []
    values += [dict(v=1, end=end)] if end is not None else []
    return b''.join(json.dumps(value).encode() + b'\n' for value in values)


class Serve(unittest.TestCase):
    """`disk_side.serve` on the worker's side: clock, scope, evidence, proof, stage, `finish`."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)

    def wire(self, **changes):
        return dict(dict(stage=4, side=NATIVE, apply=False, now=time.time(), drive=dict(path=str(self.state)), repos=[],
                         paths=dict(worker_timeout_s=900), state_dir=str(self.state), tasks={}), **changes)

    def test_serve_builds_the_clock_before_the_process_proof(self):
        """Section 3 item 4: the deadline started after the proof, so the margin was partly spent before it began."""
        from drover import disk_evidence, disk_side, disk_stage
        events = []

        def clock_factory(timeout_s, **options):
            events.append(('clock', timeout_s))
            return disk_stage.StageClock(timeout_s, **options)

        def scope(drive_path, paths):
            events.append('scope')
            return paths, []

        def processes(**_):
            events.append('proof')
            return SimpleNamespace(busy=lambda path: False)

        out = io.BytesIO()
        with mock.patch.object(disk_side, 'StageEvidence', partial(disk_evidence.StageEvidence, processes=processes)), \
                mock.patch.object(placement, 'cleanup_scope', scope):
            disk_side.serve(self.wire(), out=out, clock_factory=clock_factory)
        self.assertEqual(events[:3], [('clock', 900), 'scope', 'proof'])
        self.assertTrue(read_rows(out.getvalue()).complete)


class Claim(unittest.TestCase):
    def test_claim_keeps_pid_run_id_request_and_drops_completed_result(self):
        """O9: the `end` line replaces `completed` and `result`; the termination proof reads only what stays."""
        from drover import disk_side, placement_worker
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        request = dict(_worker_id='claim', state_dir=temp.name, stage=4, side='wsl', apply=False,
                       drive=dict(path='/mnt/h'), repos=[], paths={})
        stdout = SimpleNamespace(buffer=io.BytesIO())

        def serve(wire, *, out):
            out.write(lines(row('/w/a'), end=dict(reclaimed_bytes=0)))

        with mock.patch.object(placement_worker, 'IS_WINDOWS', False), \
                mock.patch.object(placement_worker, 'lower_priority'), mock.patch.object(os, 'setsid', create=True), \
                mock.patch.object(disk_side, 'serve', serve), mock.patch('sys.stdout', stdout):
            placement_worker.cleanup_worker(request)
        claim = json.loads((Path(temp.name) / 'disk' / 'worker-claim.json').read_text(encoding='utf-8'))
        self.assertEqual((claim['pid'], claim['run_id'], claim['request']['stage']), (os.getpid(), 'claim', 4))
        self.assertNotIn('completed', claim)
        self.assertNotIn('result', claim)
        self.assertEqual(read_rows(stdout.buffer.getvalue()).items, [row('/w/a')])


if __name__ == '__main__':
    unittest.main()
