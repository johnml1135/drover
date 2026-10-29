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

from tests.test_runner import Base, PKG_ROOT, ENV, PY, alive, wait_for, _cleanup_detached_fixture


@integration
class DetachTests(Base):
    def test_detach_returns_at_once_and_runs(self):
        hooks = Path(self.runs) / "shutdown-hook"
        hooks.mkdir()
        (hooks / "sitecustomize.py").write_text(
            "import atexit,sys,time\n"
            "if '--run-id' in sys.argv and '--detach' not in sys.argv:\n"
            "    atexit.register(lambda: time.sleep(2))\n")
        env = {**ENV, "PYTHONPATH": os.pathsep.join([str(hooks), ENV["PYTHONPATH"]])}
        cmd = self.cmd("d1", "import time;time.sleep(1);print('late')", extra=["--detach"])
        t = time.monotonic()
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30)
        self.assertLess(time.monotonic() - t, 5)
        info = json.loads(r.stdout)

        # Keep PID exit and sharing-safe log cleanup inside one teardown deadline.
        self.addCleanup(_cleanup_detached_fixture, Path(self.runs) / "d1",
                        {info["runner_pid"], info["launcher_pid"]})
        self.assertEqual(info["run_id"], "d1")
        self.assertTrue(wait_for(lambda: self.status("d1")["state"] == "exited"))
        self.assertIn("late", self.out("d1"))
        status = self.status("d1")
        self.assertEqual(status["state"], "exited")
        self.assertEqual(status["runner_pid"], info["runner_pid"])
        self.assertIsInstance(info["detached_in_job"], bool)
        self.assertIsInstance(status["detached_in_job"], bool)
        self.assertEqual(info["detached_in_job"], status["detached_in_job"])
        if not runner.IS_WINDOWS:
            self.assertFalse(info["detached_in_job"])
            self.assertFalse(status["detached_in_job"])

    @unittest.skipUnless(runner.IS_WINDOWS, "Windows sharing semantics")
    def test_detached_fixture_cleanup_retries_after_dead_process_log_release(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            run_dir.mkdir()
            log = run_dir / runner.RUNNER_LOG
            log.write_text("detached runner output")
            child = subprocess.Popen([PY, "-c", "pass"])
            child.wait(timeout=10)
            self.assertFalse(alive(child.pid), "controlled child must be dead before fixture cleanup")

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateFileW.restype = ctypes.c_void_p
            kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                           ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                           ctypes.c_void_p]
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel.CreateFileW(str(log), 0x80000000, 0x1 | 0x2, None, 3, 0x80, None)
            self.assertNotIn(handle, (None, ctypes.c_void_p(-1).value),
                             f"CreateFileW failed: {ctypes.get_last_error()}")
            held = [handle]

            def release_log(_duration):
                self.assertTrue(kernel.CloseHandle(held.pop()))

            attempts = []
            original_unlink = Path.unlink

            def tracked_unlink(path, *args, **kwargs):
                attempts.append(path)
                return original_unlink(path, *args, **kwargs)

            try:
                with patch.object(Path, "unlink", tracked_unlink), \
                        patch.object(time, "sleep", side_effect=release_log) as sleep:
                    _cleanup_detached_fixture(run_dir, {child.pid}, timeout=2)
                self.assertEqual(attempts, [log, log])
                sleep.assert_called_once()
                self.assertFalse(log.exists())
            finally:
                if held:
                    kernel.CloseHandle(held.pop())

    def test_detached_fixture_cleanup_keeps_persistent_and_unrelated_errors_visible(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            log = run_dir / runner.RUNNER_LOG
            log.write_text("runner output")
            persistent = PermissionError(13, "sharing violation")
            persistent.winerror = 32
            attempts = []

            def always_sharing_violation(*args, **kwargs):
                attempts.append(True)
                raise persistent

            started = time.monotonic()
            with patch.object(runner, "IS_WINDOWS", True), \
                    patch.object(Path, "unlink", side_effect=always_sharing_violation):
                with self.assertRaises(PermissionError) as raised:
                    _cleanup_detached_fixture(run_dir, set(), timeout=0.05)
            self.assertIs(raised.exception, persistent)
            self.assertGreater(len(attempts), 1)
            self.assertLess(time.monotonic() - started, 1)

            unrelated = OSError(5, "unrelated filesystem failure")
            with patch.object(runner, "IS_WINDOWS", True), \
                    patch.object(Path, "unlink", side_effect=unrelated) as unlink:
                with self.assertRaises(OSError) as raised:
                    _cleanup_detached_fixture(run_dir, set())
            self.assertIs(raised.exception, unrelated)
            unlink.assert_called_once_with()

    def test_windows_detach_reports_breakaway_success_and_refusal(self):
        flags = {"CREATE_NO_WINDOW": 0x08000000, "CREATE_NEW_PROCESS_GROUP": 0x200,
                 "CREATE_BREAKAWAY_FROM_JOB": 0x01000000}
        base_flags = flags["CREATE_NO_WINDOW"] | flags["CREATE_NEW_PROCESS_GROUP"]

        def run_detach(run_id, attempts):
            proc = Mock(pid=123)
            popen = Mock(side_effect=attempts(proc))
            observed_status = {}

            def read_child_status(_runs_dir, _run_id):
                child_env = popen.call_args.kwargs["env"]
                observed_status.update(state="running", runner_pid=456,
                                       detached_in_job=child_env.get("DROVER_DETACHED_IN_JOB") == "1")
                return observed_status

            clock = Mock(return_value=100)
            with patch.multiple('drover.placement_exec', IS_WINDOWS=True, create=True, **flags), \
                    patch.dict(os.environ, {}, clear=True), \
                    patch.object(runner.subprocess, "Popen", popen), \
                    patch.object(runner, "read_status", side_effect=read_child_status), \
                    patch.object(runner.time, "monotonic", clock):
                info = runner.detach(run_id, self.runs, ["--run-id", run_id])
            clock.assert_called_once_with()
            return info, dict(observed_status), popen

        with self.subTest(breakaway_succeeds=True):
            info, status, popen = run_detach("breakaway-success", lambda proc: [proc])
            self.assertEqual(popen.call_count, 1)
            first = popen.call_args_list[0].kwargs
            self.assertEqual(first["creationflags"], base_flags | flags["CREATE_BREAKAWAY_FROM_JOB"])
            self.assertNotIn("DROVER_DETACHED_IN_JOB", first["env"])
            self.assertIs(info["detached_in_job"], False)
            self.assertIs(status["detached_in_job"], False)
            self.assertEqual(info["detached_in_job"], status["detached_in_job"])

        with self.subTest(breakaway_refused=True):
            refused = OSError(5, "breakaway refused")
            info, status, popen = run_detach("breakaway-fallback", lambda proc: [refused, proc])
            self.assertEqual(popen.call_count, 2)
            first, fallback = [call.kwargs for call in popen.call_args_list]
            self.assertEqual(first["creationflags"], base_flags | flags["CREATE_BREAKAWAY_FROM_JOB"])
            self.assertNotIn("DROVER_DETACHED_IN_JOB", first["env"])
            self.assertEqual(fallback["creationflags"], base_flags)
            self.assertEqual(fallback["env"]["DROVER_DETACHED_IN_JOB"], "1")
            self.assertIs(info["detached_in_job"], True)
            self.assertIs(status["detached_in_job"], True)
            self.assertEqual(info["detached_in_job"], status["detached_in_job"])
            log = (Path(self.runs) / "breakaway-fallback" / runner.RUNNER_LOG).read_text()
            self.assertIn("breakaway from the parent job was refused", log)


    def test_no_window_helper_is_empty_off_windows(self):
        from drover import placement
        self.assertEqual(placement.NO_WINDOW, {"creationflags": 0x08000000} if os.name == "nt" else {})

    def test_windows_detach_propagates_failed_fallback_spawn(self):
        flags = {"CREATE_NO_WINDOW": 0x08000000, "CREATE_NEW_PROCESS_GROUP": 0x200,
                 "CREATE_BREAKAWAY_FROM_JOB": 0x01000000}
        base_flags = flags["CREATE_NO_WINDOW"] | flags["CREATE_NEW_PROCESS_GROUP"]
        first_error = OSError(5, "breakaway refused")
        fallback_error = OSError(87, "fallback spawn failed")
        popen = Mock(side_effect=[first_error, fallback_error])

        with patch.multiple('drover.placement_exec', IS_WINDOWS=True, create=True, **flags), \
                patch.dict(os.environ, {}, clear=True), \
                patch.object(runner.subprocess, "Popen", popen), \
                patch.object(runner.time, "monotonic", Mock(return_value=100)):
            with self.assertRaises(OSError) as raised:
                runner.detach("breakaway-fallback-fails", self.runs, ["--run-id", "breakaway-fallback-fails"])

        self.assertIs(raised.exception, fallback_error)
        self.assertEqual(popen.call_count, 2)
        first, fallback = [call.kwargs for call in popen.call_args_list]
        self.assertEqual(first["creationflags"], base_flags | flags["CREATE_BREAKAWAY_FROM_JOB"])
        self.assertNotIn("DROVER_DETACHED_IN_JOB", first["env"])
        self.assertEqual(fallback["creationflags"], base_flags)
        self.assertEqual(fallback["env"]["DROVER_DETACHED_IN_JOB"], "1")

    def test_detach_reports_a_child_that_dies(self):
        r = subprocess.run([PY, "-m", "drover.runner", "--run-id", "d2", "--runs-dir", self.runs, "--detach",
                            "--wall-s", "-1", "--", PY, "-c", "pass"], env=ENV, capture_output=True, text=True,
                           timeout=30)
        self.assertNotEqual(r.returncode, 0)       # argparse rejects it before detaching
        info = runner.detach("d3", self.runs, ["--bogus-flag", "--", "x"], wait_s=5)
        self.assertIn("error", info)
