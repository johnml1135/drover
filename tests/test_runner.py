from drover import placement
from drover.launch import RunnerLauncher
"""Runner tests: real subprocesses running small python child scripts on the current OS."""
import ctypes
import re
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from drover import runner
from tests.tiers import integration

PKG_ROOT = str(Path(runner.__file__).resolve().parents[1])
ENV = {k: v for k, v in os.environ.items() if k != "DROVER_DETACHED_IN_JOB"}
ENV["PYTHONPATH"] = PKG_ROOT
PY = sys.executable


def alive(pid):
    if runner.IS_WINDOWS:
        import ctypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.restype = ctypes.c_void_p
        h = k.OpenProcess(0x1000, False, pid)      # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_uint32()
        k.GetExitCodeProcess(ctypes.c_void_p(h), ctypes.byref(code))
        k.CloseHandle(ctypes.c_void_p(h))
        return code.value == 259
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[-1].split()[0] != "Z"


def wait_for(cond, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            v = cond()
        except (FileNotFoundError, PermissionError, json.JSONDecodeError):
            v = None  # Status replacement/sharing can race the bounded observation.
        if v:
            return v
        time.sleep(0.1)
    return None


class WindowsMetricsTests(unittest.TestCase):
    def test_job_io_uses_accounting_and_memory_uses_extended_limits(self):
        kernel = Mock()

        def query(job, kind, pointer, size, returned):
            self.assertEqual(job, 42)
            info = pointer._obj
            self.assertEqual(size, ctypes.sizeof(info))
            self.assertIsNone(returned)
            if kind in (1, 8):
                basic = info.BasicInfo if kind == 8 else info
                basic.user, basic.kernel = 20_000_000, 5_000_000
                if kind == 8:
                    info.IoInfo.rb, info.IoInfo.wb = 4096, 8192
            elif kind == 9:
                info.PeakJobMemoryUsed = 2 * 1024 ** 2
                info.IoInfo.rb, info.IoInfo.wb = 900_000, 900_000  # reserved, never read
            else:
                self.fail(f"unexpected job information class {kind}")
            return 1

        kernel.QueryInformationJobObject.side_effect = query
        spec = importlib.util.spec_from_file_location("drover._placement_windows_test", Path(placement.__file__).with_name("placement_tree.py"))
        windows_runner = importlib.util.module_from_spec(spec)
        # Load the Windows implementation on either OS, without touching real jobs.
        with patch.object(sys, "platform", "win32"), patch("drover.placement_paths.IS_WINDOWS", True), \
             patch.object(ctypes, "WinDLL", return_value=kernel, create=True):
            spec.loader.exec_module(windows_runner)
        tree = windows_runner.WindowsTree.__new__(windows_runner.WindowsTree)
        tree.job = 42
        self.assertEqual(tree.metrics(), (2.5, 2 * 1024 ** 2, 12288))
        self.assertEqual([call.args[1] for call in kernel.QueryInformationJobObject.call_args_list], [8, 9])


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.runs = self._tmp.name
        self.procs = []
        self.addCleanup(self._reap)

    def _reap(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait()
            for f in (p.stdout, p.stderr):
                f and f.close()

    def cmd(self, rid, *worker, wall=0, idle=0, extra=()):
        return [PY, "-m", "drover.runner", "--run-id", rid, "--runs-dir", self.runs, "--wall-s", str(wall),
                "--idle-s", str(idle), "--kill-after", "3", *extra, "--", PY, "-c", *worker]

    def run_runner(self, rid, code, **kw):
        r = subprocess.run(self.cmd(rid, code, **kw), env=ENV, capture_output=True, text=True, timeout=60)
        return r, self.status(rid)

    def start(self, rid, code, **kw):
        p = subprocess.Popen(self.cmd(rid, code, **kw), env=ENV, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.procs.append(p)
        return p

    def status(self, rid):
        return json.loads((Path(self.runs) / rid / "status.json").read_text())

    def out(self, rid):
        return (Path(self.runs) / rid / "out.log").read_text()


def _cleanup_detached_fixture(run_dir, pids, timeout=20):
    deadline = time.monotonic() + timeout
    while True:
        live = [pid for pid in pids if alive(pid)]
        if not live:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"detached runner/launcher survived fixture teardown: {live}")
        time.sleep(min(0.1, remaining))

    log = Path(run_dir) / runner.RUNNER_LOG
    while True:
        try:
            log.unlink()
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            if not runner.IS_WINDOWS or getattr(exc, "winerror", None) != 32:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(0.1, remaining))


@unittest.skipIf(runner.IS_WINDOWS, "WSL scratch lifecycle runs on Linux")
@integration
class ScratchTests(Base):
    def scratch(self):
        root = Path('/tmp/drover-scratch')
        task_scratch = Path(os.environ.get('TMPDIR', ''))
        parent = task_scratch if task_scratch.parent == root else root
        path = parent / f'acme-{uuid.uuid4().hex}'
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        return path

    def test_runner_creates_private_native_scratch_for_temp_wal_and_cleans_it(self):
        scratch = self.scratch()
        code = """import json, os, stat, tempfile
from pathlib import Path
from drover.ledger.db import connect
scratch = Path(os.environ['TMPDIR'])
env = {k: os.environ.get(k) for k in ('TMPDIR', 'TMP', 'TEMP')}
with tempfile.TemporaryDirectory() as tmp:
    conn = connect(Path(tmp) / 'ledger.db', migrate_db=False)
    mode = conn.execute('PRAGMA journal_mode').fetchone()[0]
    conn.close()
    print(json.dumps(dict(env=env, mode=stat.S_IMODE(scratch.stat().st_mode), tmp=tmp, journal=mode)))
(scratch / 'leftover').write_text('remove with run')
"""
        r = subprocess.run(self.cmd('scratch', code, extra=('--scratch-dir', str(scratch))),
                           env=ENV, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        data = json.loads(self.out('scratch'))
        self.assertEqual(data['env'], {
            'TMPDIR': str(scratch), 'TMP': str(scratch), 'TEMP': str(scratch),
        })
        self.assertEqual(data['mode'], 0o700)
        self.assertEqual(Path(data['tmp']).parent, scratch)
        self.assertEqual(data['journal'], 'wal')
        self.assertFalse(scratch.exists())


    def test_scratch_is_cleaned_after_timeout_and_spawn_failure(self):
        for rid, code, kw, state in (
                ('scratch-timeout', 'import time; time.sleep(60)', dict(wall=0.2), 'timeout'),
                ('scratch-spawn', 'pass', dict(extra=('--cwd', str(Path(self.runs) / 'missing'))), 'exited')):
            with self.subTest(rid=rid):
                scratch = self.scratch()
                extra = (*kw.pop('extra', ()), '--scratch-dir', str(scratch))
                r, st = self.run_runner(rid, code, extra=extra, **kw)
                self.assertEqual(r.returncode, 1)
                self.assertEqual(st['state'], state)
                if rid == 'scratch-spawn':
                    self.assertTrue(st['reason'].startswith('spawn-failed:'))
                self.assertFalse(scratch.exists())

    def test_outside_scratch_namespace_is_never_removed_or_used(self):
        outside = Path(self.runs) / 'outside'
        outside.mkdir()
        marker = outside / 'keep'
        marker.write_text('keep')
        for value in (str(outside), '/tmp/drover-scratch',
                      f'/tmp/drover-scratch/../../{str(outside).lstrip("/")}'):
            with self.subTest(path=value):
                r, st = self.run_runner('scratch-outside', 'raise SystemExit("worker must not start")',
                                        extra=('--scratch-dir', value))
                self.assertEqual(r.returncode, 1)
                self.assertTrue(st['reason'].startswith('spawn-failed:'))
                self.assertFalse((Path(self.runs) / 'scratch-outside' / runner.OUT_FILE).exists())
                self.assertEqual(marker.read_text(), 'keep')

    def test_preexisting_scratch_is_never_removed(self):
        scratch = self.scratch()
        scratch.parent.mkdir(exist_ok=True)
        scratch.mkdir()
        marker = scratch / 'keep'
        marker.write_text('keep')
        r, st = self.run_runner('scratch-existing', 'pass', extra=('--scratch-dir', str(scratch)))
        self.assertEqual(r.returncode, 1)
        self.assertTrue(st['reason'].startswith('spawn-failed:'))
        self.assertEqual(marker.read_text(), 'keep')

    def test_symlink_to_outside_is_never_followed_during_setup_or_cleanup(self):
        outside = Path(self.runs) / 'outside'
        outside.mkdir()
        marker = outside / 'keep'
        marker.write_text('keep')
        for replace in (False, True):
            with self.subTest(replace_during_run=replace):
                scratch = self.scratch()
                scratch.parent.mkdir(exist_ok=True)
                self.addCleanup(scratch.unlink, missing_ok=True)
                if replace:
                    code = f"""from pathlib import Path
scratch = Path({str(scratch)!r})
scratch.rmdir()
scratch.symlink_to({str(outside)!r}, target_is_directory=True)
"""
                else:
                    scratch.symlink_to(outside, target_is_directory=True)
                    code = 'pass'
                r, st = self.run_runner('scratch-symlink', code, extra=('--scratch-dir', str(scratch)))
                self.assertEqual(r.returncode, 0 if replace else 1, r.stderr + r.stdout)
                if not replace:
                    self.assertTrue(st['reason'].startswith('spawn-failed:'))
                self.assertEqual(marker.read_text(), 'keep')

    def test_cleanup_unlinks_nested_symlinks_without_removing_their_targets(self):
        scratch = self.scratch()
        outside = Path(self.runs) / 'outside'
        outside.mkdir()
        marker = outside / 'keep'
        marker.write_text('keep')
        code = f"""from pathlib import Path
(Path({str(scratch)!r}) / 'outside').symlink_to({str(outside)!r}, target_is_directory=True)
"""
        r, _ = self.run_runner('scratch-nested-link', code, extra=('--scratch-dir', str(scratch)))
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertFalse(scratch.exists())
        self.assertEqual(marker.read_text(), 'keep')






@integration
class FindTests(Base):
    def test_find_matches_only_marker_processes(self):
        # Discovery scans the host, so concurrent gates must not share a run id.
        rid = f"find-{uuid.uuid4().hex}"
        decoys = [
            subprocess.Popen([PY, "-c", "import time;time.sleep(60)  # -m drover.runner --run-id decoy1"]),
            subprocess.Popen([PY, "-c", "import time;time.sleep(60)", "drover.runner", "--run-id", "decoy2"]),
        ]
        self.procs += decoys
        p = self.start(rid, "import time;time.sleep(60)")
        wait_for(lambda: (Path(self.runs) / rid / "status.json").exists())
        found = wait_for(lambda: runner.find_runners(runs_dir=self.runs))
        # under a venv the Popen pid is a launcher over the real runner, which is what status.json records
        self.assertEqual(found, [{"pid": self.status(rid)["runner_pid"], "run_id": rid}])
        ids = {r["run_id"] for r in runner.find_runners()}
        self.assertFalse(ids & {"decoy1", "decoy2"})
        self.assertFalse({r["pid"] for r in runner.find_runners()} & {d.pid for d in decoys})
        subprocess.run([PY, "-m", "drover.runner", "stop", "--run-id", rid, "--runs-dir", self.runs], env=ENV,
                       check=True, capture_output=True)
        p.wait(timeout=30)
        runner_pid = self.status(rid)["runner_pid"]
        self.assertTrue(wait_for(lambda: not alive(runner_pid), timeout=30),
                        f"runner process {runner_pid} did not exit before the deadline")
        self.assertEqual(wait_for(lambda: runner.find_runners(runs_dir=self.runs) == [], timeout=30), True)


    def test_find_runners_injected(self):
        lines = [(7, ok_argv("a") + ["--runs-dir", "runs-a"]),
                 (8, ["python3", "x.py"]), (9, ok_argv("b") + ["--runs-dir", "runs-b"])]
        decoys = [
            ["wsl.exe", "-d", "u", "--", *ok_argv("spoof")],
            ["bash", "-c", "python3 -m drover.runner --run-id spoof"],
            ["python3", "-m", "drover.runner", "stop", "--run-id", "spoof"],
            ["python3", "-m", "drover.runner", "--", "--run-id", "spoof"],
            ["python3", "-m", "drover.runner", "--run-id", "../evil"],
            ["python3-evil", "-m", "drover.runner", "--run-id", "spoof"],
        ]
        lines += [(100 + i, argv) for i, argv in enumerate(decoys)]
        self.assertEqual(runner.find_runners(cmdlines=lines), [{"pid": 7, "run_id": "a"}, {"pid": 9, "run_id": "b"}])
        self.assertEqual(runner.find_runners("b", cmdlines=lines), [{"pid": 9, "run_id": "b"}])
        self.assertEqual(runner.find_runners(cmdlines=lines, runs_dir="runs-a"), [{"pid": 7, "run_id": "a"}])


    def test_find_dedupes_launcher_pair_keeping_child(self):
        rows = [(50, ok_argv("a"), 10), (70, ok_argv("a"), 50), (60, ok_argv("b"), 1)]
        self.assertEqual(runner.find_runners(cmdlines=rows), [{"pid": 60, "run_id": "b"}, {"pid": 70, "run_id": "a"}])

    @unittest.skipUnless(runner.IS_WINDOWS, "CIM is Windows only")
    def test_windows_scan_failure_raises(self):
        with patch("drover.placement_process._powershell", return_value="no-such-powershell-xyz"):
            with self.assertRaises(runner.ScanError):
                runner.find_runners()


def ok_argv(rid):
    return ["python3", "-m", "drover.runner", "--run-id", rid]










if __name__ == "__main__":
    unittest.main()
