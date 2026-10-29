"""RunnerLauncher and RunnerHandle against a fake runner (files only), plus one real runner smoke test."""
from drover import placement as placement_mod
import json
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from drover import launch, runner
from drover.runner import ScanError

from tests.test_launch_runner import Base, DONE, TASK, WT, NONCE, spec, assistant, wait_for, FakeRunner


class Handle(Base):
    def test_done_record_usage_and_tail(self):
        h = self.start()
        self.assertIsNone(h.poll())
        self.emit({"type": "system"}, assistant("working", 1234))
        wait_for(lambda: h.tail.lines)
        self.emit({"type": "result", "result": DONE, "usage": {"input_tokens": 50, "output_tokens": 9}})
        self.record()
        self.set_status(state="exited", exit_code=0)
        r = wait_for(h.poll)
        self.assertEqual((r["status"], r["record"]["sha"]), ("done", "abc1234"))
        self.assertEqual((r["usage"]["tokens_in"], r["usage"]["tokens_out"]), (50, 9))
        self.assertIn("assistant: working", (self.dir() / "tail.log").read_text())
        self.assertEqual(h.pid, 1234)

    def test_exit_states_map_to_statuses(self):
        cases = [(dict(state="exited", exit_code=0), "no-record"), (dict(state="exited", exit_code=2), "error"),
                 (dict(state="timeout", exit_code=-9, reason="wall-clock limit 5s"), "timeout"),
                 (dict(state="killed", exit_code=-15, reason="requested"), "error"),
                 (dict(state="exited", exit_code=None, reason="spawn-failed: x"), "error")]
        for i, (st, want) in enumerate(cases):
            rid = 20 + i
            h = self.rl.launch(spec(run_id=rid))
            self.addCleanup(h.cancel)
            self.set_status(rid, **st)
            r = wait_for(h.poll)
            self.assertEqual(r["status"], want, st)
            if want == "timeout":
                self.assertEqual(r["timeout"], "wall-clock limit 5s")

    def test_blocked_record(self):
        h = self.start()
        self.record('{"status": "blocked", "sha": "", "blocker": {"kind": "plan", "detail": "api differs"}, "note": ""}')
        self.set_status(state="exited", exit_code=0)
        r = wait_for(h.poll)
        self.assertEqual((r["status"], r["record"]["blocker"]["kind"]), ("blocked", "plan"))

    def test_haiku_context_ceiling_requests_a_rotation_stop_then_rotates(self):
        h = self.start(model="haiku-5.5")
        self.emit(assistant("big", 90000))
        read = Path.read_text
        transient_reads = []
        def sharing_once(path, *args, **kwargs):
            if path.name == runner.STOP_FILE and not transient_reads:
                transient_reads.append(True)
                raise PermissionError("transient stop-file sharing error")
            return read(path, *args, **kwargs)
        with patch.object(Path, "read_text", sharing_once):
            stop = wait_for(lambda: runner._read_stop_file(self.dir()))
        self.assertEqual(transient_reads, [True])
        self.assertEqual(stop, {"reason": "rotation", "nonce": NONCE})
        self.set_status(state="killed", exit_code=-15, reason="rotation")
        r = wait_for(h.poll)
        self.assertEqual(r["status"], "rotate")
        self.assertIn("Rotated", r["handoff"])
        self.assertIsNone(r["timeout"])

    def test_rotation_from_the_status_alone_after_a_restart(self):
        self.start()                                  # creates the files
        self.set_status(state="killed", exit_code=-15, reason="rotation")
        h = self.rl.adopt(spec(adopted=True))
        self.addCleanup(h.cancel)
        r = wait_for(h.poll)
        self.assertEqual((r["status"], r["adopted"]), ("rotate", True))

    def test_a_finished_record_beats_rotation(self):
        h = self.start(model="haiku-5.5")
        self.emit(assistant("big", 90000))
        wait_for(lambda: (self.dir() / runner.STOP_FILE).exists())
        self.record()
        self.set_status(state="killed", exit_code=-15, reason="rotation")
        self.assertEqual(wait_for(h.poll)["status"], "done")

    def test_sonnet_has_no_ceiling(self):
        self.assertIsNone(self.rl.ceiling("sonnet-5.5"))
        self.assertEqual(self.rl.ceiling("haiku-5.5"), launch.HAIKU_CEILING)

    def test_stop_writes_a_nonce_checked_request(self):
        h = self.start()
        self.assertTrue(h.stop("requested"))
        self.assertEqual(json.loads((self.dir() / runner.STOP_FILE).read_text()), {"reason": "requested", "nonce": NONCE})
        self.assertFalse(self.rl.adopt(spec(run_id=99)).stop())          # no launch recorded: nothing to stop

    def test_run_that_never_leaves_starting_is_stopped_and_errored(self):
        now = [1000.0]
        self.rl.run = FakeRunner(self.runs, state="starting", rc=1,
                                 info={"runner_pid": None, "error": "runner still starting after 5s"})
        h = launch.RunnerHandle(spec(), self.runs, None, clock=lambda: now[0], poll_s=0.02, start_timeout_s=60, autostart=False)
        self.rl.launch(spec())                      # writes status.json in `starting`
        h.status()
        now[0] += 61
        h._thread.start()
        r = wait_for(h.poll)
        self.assertEqual(r["status"], "error")
        self.assertIn("still starting", r["text"])
        self.assertTrue((self.dir() / runner.STOP_FILE).exists())

    def test_overlong_line_is_cut_and_later_lines_still_parse(self):
        h = self.start()
        self.emit("x" * (launch.MAX_LINE * 3))
        self.emit(assistant("after", 77))
        self.set_status(state="exited", exit_code=0)
        r = wait_for(h.poll)
        self.assertEqual(r["usage"]["context"], 77)
        self.assertTrue(all(len(line) <= 1000 for line in h.tail.lines))

    def test_cancel_publishes_nothing_even_with_a_pass_in_flight(self):
        for i in range(30):
            rid = 100 + i
            with patch.dict(self.rl.handle_kw, autostart=False):
                h = self.rl.launch(spec(run_id=rid))
            self.addCleanup(h.cancel)
            gate = threading.Event()
            release = threading.Event()
            orig = h.terminal

            def held(orig=orig, gate=gate, release=release):
                st = orig()
                if st:                      # the thread has seen the terminal status; hold it before it publishes
                    gate.set()
                    self.assertTrue(release.wait(5), "terminal observation was not released")
                return st
            h.terminal = held
            self.set_status(rid, state="exited", exit_code=0)
            h._thread.start()                # install the observer before the first terminal read
            try:
                self.assertTrue(gate.wait(5))
                h.cancel()                  # cancel lands while the thread is between reading and publishing
            finally:
                release.set()
                h._thread.join(5)
            self.assertFalse(h._thread.is_alive(), i)
            self.assertIsNone(h.poll(), i)

    def test_cancel_before_anything_happens_publishes_nothing(self):
        for i in range(30):
            h = self.rl.launch(spec(run_id=200 + i))
            h.cancel()
            self.set_status(200 + i, state="exited", exit_code=0)
            time.sleep(0.02)
            self.assertIsNone(h.poll(), i)



class Scanning(Base):
    def test_scan_rejects_non_numeric_ids_and_accepts_mixed_numeric_ids(self):
        rows = [{"run_id": rid} for rid in ("5", 7, "acme-app", "", None, True, 8.5)]
        self.rl.run = lambda *a, **k: SimpleNamespace(
            stdout=json.dumps(rows + [{"run_id": "6"}, {"run_id": 9}]), stderr="", returncode=0)
        with patch.object(runner, "find_runners", return_value=rows):
            self.assertEqual(self.rl.scan(True), {5: "windows", 7: "windows", 6: "wsl", 9: "wsl"})

    def test_windows_only_unless_wsl_is_wanted(self):
        calls = []
        orig = runner.find_runners
        runner.find_runners = lambda: [{"pid": 1, "run_id": "5"}]
        self.addCleanup(setattr, runner, "find_runners", orig)
        self.rl.run = lambda *a, **k: calls.append(a) or SimpleNamespace(stdout="[]", stderr="", returncode=0)
        self.assertEqual(self.rl.scan(False), {5: "windows"})
        self.assertEqual(calls, [])
        self.rl.run = lambda cmd, **k: calls.append(cmd) or SimpleNamespace(
            stdout=json.dumps([{"pid": 2, "run_id": "6"}]), stderr="", returncode=0)
        self.assertEqual(self.rl.scan(True), {5: "windows", 6: "wsl"})
        self.assertEqual(calls[0][:5], ["wsl.exe", "-d", "Dist", "--cd", "/mnt/r/repos/drover"])
        self.assertEqual(calls[0][-4:], ["-m", "drover.runner", "find", "--json"])

    def test_any_wsl_failure_is_a_scan_error(self):
        orig = runner.find_runners
        runner.find_runners = lambda: []
        self.addCleanup(setattr, runner, "find_runners", orig)
        for res in (SimpleNamespace(stdout="", stderr="unknown", returncode=3),
                    SimpleNamespace(stdout="not json", stderr="", returncode=0),
                    SimpleNamespace(stdout='{"a": 1}', stderr="", returncode=0)):
            self.rl.run = lambda *a, _r=res, **k: _r
            with self.assertRaises(ScanError):
                self.rl.scan(True)

        def boom(*a, **k):
            raise subprocess.TimeoutExpired("wsl", 1)
        self.rl.run = boom
        with self.assertRaises(ScanError):
            self.rl.scan(True)

    def test_windows_scan_error_propagates(self):
        orig = runner.find_runners

        def bad(*a):
            raise ScanError("powershell died")
        runner.find_runners = bad
        self.addCleanup(setattr, runner, "find_runners", orig)
        with self.assertRaises(ScanError):
            self.rl.scan(False)

    def test_wsl_running_reads_utf16_list(self):
        out = "Ubuntu\r\nDebian\r\n".encode("utf-16-le")
        run = lambda *a, **k: SimpleNamespace(stdout=out, stderr=b"", returncode=0)
        self.assertTrue(__import__("drover.resources", fromlist=["wsl_running"]).wsl_running("ubuntu", run))
        self.assertFalse(__import__("drover.resources", fromlist=["wsl_running"]).wsl_running("Other", run))
        self.assertFalse(__import__("drover.resources", fromlist=["wsl_running"]).wsl_running("Ubuntu", lambda *a, **k: SimpleNamespace(stdout=b"", stderr=b"", returncode=1)))



class PathHelpers(unittest.TestCase):
    def test_paths(self):
        self.assertEqual(placement_mod.to_windows_path("/mnt/r/a/b"), r"R:\a\b")
        self.assertEqual(placement_mod.to_windows_path("/mnt/c"), "C:\\")
        self.assertEqual(placement_mod.to_windows_path(r"R:\x"), r"R:\x")
        self.assertTrue(placement_mod.is_linux_native("/home/x"))
        self.assertFalse(placement_mod.is_linux_native("/mnt/r/x"))
        self.assertFalse(placement_mod.is_linux_native(r"R:\x"))



class LateRunner(unittest.TestCase):
    """A stop-request written with the launch's own nonce, before the runner exists, is honoured by a late runner."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)

    def test_stop_before_status_exists_ends_a_late_runner_at_once(self):
        self.assertFalse(runner.request_stop(self.tmp.name, "9"))               # no nonce, no status: nothing to do
        self.assertTrue(runner.request_stop(self.tmp.name, "9", "requested", NONCE))
        self.assertFalse((Path(self.tmp.name) / "9" / runner.STATUS_FILE).exists())
        t0 = time.time()
        st = runner.supervise("9", self.tmp.name, [sys.executable, "-c", "import time; time.sleep(60)"],
                              nonce=NONCE, kill_after=3, poll_s=0.05)
        self.assertLess(time.time() - t0, 30)
        self.assertEqual((st["state"], st["reason"], st["nonce"]), ("killed", "requested", NONCE))

    def test_a_stop_with_another_nonce_is_ignored_and_removed(self):
        runner.request_stop(self.tmp.name, "8", "requested", "f" * 32)
        st = runner.supervise("8", self.tmp.name, [sys.executable, "-c", "pass"], nonce=NONCE, poll_s=0.05)
        self.assertEqual(st["state"], "exited")

    def test_bad_nonce_is_refused_and_detach_uses_the_given_one(self):
        with self.assertRaises(ValueError):
            runner.request_stop(self.tmp.name, "7", "requested", "../x")
        info = runner.detach("6", self.tmp.name, ["--run-id", "6", "--runs-dir", self.tmp.name, "--nonce", NONCE, "--",
                                                  sys.executable, "-c", "pass"], wait_s=20, nonce=NONCE)
        self.assertNotIn("error", info)
        self.assertEqual(runner.read_status(self.tmp.name, "6")["nonce"], NONCE)
        with self.assertRaises(SystemExit):
            runner.main(["--run-id", "5", "--runs-dir", self.tmp.name, "--nonce", "short", "--", "x"])



@unittest.skipUnless(sys.platform == "win32", "the real Windows placement")
class RealRunnerSmoke(unittest.TestCase):
    """The real runner, started detached by the real RunnerLauncher: python -c prints a stream-json done record."""

    def test_python_dash_c_worker_finishes_done(self):
        code = ("import sys,json\nbrief=sys.stdin.read()\n"
                "text=brief+' '+%r\n"
                "print(json.dumps({'type':'assistant','message':{'usage':{'input_tokens':5,'output_tokens':2},"
                "'content':[{'type':'text','text':text}]}}),flush=True)\n"
                "print(json.dumps({'type':'result','result':text,'usage':{'input_tokens':5,'output_tokens':2}}),flush=True)\n"
                % DONE)
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        wt = Path(tmp.name) / "wt"
        wt.mkdir()
        rl = launch.RunnerLauncher(runs_dir=Path(tmp.name) / "runs", poll_s=0.05)
        orig = launch.build_argv
        launch.build_argv = lambda sp: ([sys.executable, "-c", code], sp["user_prompt"])
        self.addCleanup(setattr, launch, "build_argv", orig)
        h = rl.launch(spec(cwd=str(wt), run_id=31))
        self.addCleanup(h.cancel)
        r = wait_for(h.poll, 60)
        self.assertEqual(r["status"], "done")
        self.assertEqual(r["record"]["sha"], "abc1234")
        self.assertIn("BRIEF", r["text"])
        self.assertEqual(r["usage"]["tokens_in"], 5)
        st = runner.read_status(rl.runs_dir, 31)
        self.assertEqual((st["state"], st["exit_code"]), ("exited", 0))
