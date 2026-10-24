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

DONE = '{"status": "done", "sha": "abc1234", "blocker": null, "note": "ok"}'
TASK = {"id": "T", "title": "t", "goal": "g"}
WT = r"R:\repos\wt"
NONCE = "0123456789abcdef0123456789abcdef"


def spec(**kw):
    return {"run_id": 7, "backend": "claude", "model": "sonnet-5.5", "effort": "high", "cwd": WT,
            "placement": "windows", "system_append": "SYS", "user_prompt": "BRIEF", "task": TASK,
            "wall_s": 3600, "idle_s": 600, "nonce": NONCE, **kw}


def assistant(text="", ctx=0):
    return {"type": "assistant", "message": {"usage": {"input_tokens": ctx, "output_tokens": 1},
                                             "content": [{"type": "text", "text": text}]}}


def wait_for(fn, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.02)
    raise AssertionError("timed out")


class FakeRunner:
    """Stands in for `python -m drover.runner --detach ...`: records the command and writes the files it would."""

    def __init__(self, runs, state="running", info=None, rc=0, reason=None):
        self.runs, self.calls, self.state, self.info, self.rc, self.reason = runs, [], state, info, rc, reason

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        args = cmd[cmd.index("--detach") - 0:]
        rid = args[args.index("--run-id") + 1]
        d = Path(self.runs) / rid
        st = {"run_id": rid, "state": self.state, "pid": 99, "runner_pid": 1234, "nonce": args[args.index("--nonce") + 1], "exit_code": None,
              "reason": self.reason, "started": "x", "ended": None, "detached_in_job": False}
        if getattr(self, 'missing_executable', None) is not None:
            st['missing_executable'] = self.missing_executable
        (d / runner.STATUS_FILE).write_text(json.dumps(st))
        out = self.info if self.info is not None else {"runner_pid": 1234, "run_id": rid}
        return SimpleNamespace(stdout=json.dumps(out) + "\n", stderr="", returncode=self.rc)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.runs = Path(self.tmp.name) / "runs"
        self.fake = FakeRunner(self.runs)
        self.rl = launch.RunnerLauncher(runs_dir=self.runs, distro="Dist", drover_dir=r"R:\repos\drover",
                                        python=r"C:\py\python.exe", run=self.fake, which=lambda n: r"C:\bin\claude.exe", windows=True,
                                        poll_s=0.02)

    def start(self, **kw):
        h = self.rl.launch(spec(**kw))
        self.addCleanup(h.cancel)
        return h

    def dir(self, rid=7):
        return self.runs / str(rid)

    def set_status(self, rid=7, **kw):
        d = self.dir(rid)
        st = json.loads((d / runner.STATUS_FILE).read_text())
        st.update(kw)
        runner.write_json_atomic(d / runner.STATUS_FILE, st)

    def emit(self, *objs, rid=7):
        with open(self.dir(rid) / runner.OUT_FILE, "ab") as f:
            for o in objs:
                f.write((o if isinstance(o, str) else json.dumps(o)).encode() + b"\n")

    def record(self, text=DONE, rid=7):
        runner.write_json_atomic(self.dir(rid) / runner.RECORD_FILE, runner.parse_action_record(text))


class Commands(Base):
    def test_codex_scratch_is_private_and_supplies_the_temp_environment(self):
        for rid, placement in ((7, 'wsl'), (8, 'windows')):
            with self.subTest(placement=placement):
                self.start(run_id=rid, backend='codex', model='gpt-6.1-sol', placement=placement)
                cmd, _ = self.fake.calls[-1]
                head, worker = cmd[:cmd.index('--')], cmd[cmd.index('--') + 1:]
                settings = tomllib.loads('\n'.join(worker[i + 1] for i, arg in enumerate(worker[:-1])
                    if arg == '-c' and worker[i + 1].startswith(
                        ('sandbox_workspace_write.', 'shell_environment_policy.'))))
                sandbox = settings['sandbox_workspace_write']
                roots = sandbox['writable_roots']
                self.assertEqual(len(roots), 1)
                if placement == 'wsl':
                    self.assertEqual(roots, [f'/tmp/drover-scratch/7-{NONCE}'])
                    self.assertEqual(head[head.index('--scratch-dir') + 1], roots[0])
                    self.assertEqual(list(self.dir(rid).glob('scratch-*')), [])
                else:
                    scratch = list(self.dir(rid).glob('scratch-*'))
                    self.assertEqual(len(scratch), 1)
                    self.assertEqual(roots, [placement_mod.to_windows_path(scratch[0])])
                    self.assertNotIn('--scratch-dir', head)
                self.assertTrue(sandbox['exclude_slash_tmp'])
                self.assertTrue(sandbox['exclude_tmpdir_env_var'])
                self.assertNotIn('--add-dir', worker)
                self.assertEqual(settings['shell_environment_policy']['set'],
                                 dict(TMPDIR=roots[0], TMP=roots[0], TEMP=roots[0]))

    def test_home_path_is_expanded_on_windows_before_wsl_translation(self):
        for rid, placement, expected in ((71, 'windows', r'R:\Users\acme\repos\acme-lib.worktrees\drover-T1'),
                                          (72, 'wsl', '/mnt/r/Users/acme/repos/acme-lib.worktrees/drover-T1')):
            with self.subTest(placement=placement), patch('os.path.expanduser', return_value=
                    r'R:\Users\acme\repos\acme-lib.worktrees\drover-T1'):
                self.start(run_id=rid, placement=placement, cwd='~/repos/acme-lib.worktrees/drover-T1')
            cmd, _ = self.fake.calls[-1]
            self.assertEqual(cmd[cmd.index('--cwd') + 1], expected)

    def test_windows_placement_runs_the_runner_with_the_hubs_python(self):
        self.start()
        cmd, kw = self.fake.calls[0]
        self.assertEqual(cmd[:3], [r"C:\py\python.exe", "-m", "drover.runner"])
        i = cmd.index("--")
        head, worker = cmd[3:i], cmd[i + 1:]
        self.assertEqual(head[0], "--detach")
        for flag, val in (("--run-id", "7"), ("--runs-dir", str(self.runs)), ("--wall-s", "3600"),
                          ("--idle-s", "600"), ("--cwd", WT), ("--stdin-file", str(self.dir() / "in.txt"))):
            self.assertEqual(head[head.index(flag) + 1], val)
        self.assertEqual(worker[0], r"C:\bin\claude.exe")
        self.assertIn("--append-system-prompt", worker)
        self.assertEqual(worker[worker.index("--model") + 1], "claude-sonnet-5-5")
        self.assertIn(r"R:\repos\drover", kw["env"]["PYTHONPATH"])
        self.assertEqual((self.dir() / "in.txt").read_text(), "BRIEF")

    def test_wsl_placement_goes_through_wsl_exec_with_cd_and_mapped_paths(self):
        self.start(placement="wsl")
        cmd, kw = self.fake.calls[0]
        self.assertEqual(cmd[:8], ["wsl.exe", "-d", "Dist", "--cd", "/mnt/r/repos/drover", "--exec",
                                   "python3", "-m"])
        self.assertEqual(cmd[8], "drover.runner")
        i = cmd.index("--")
        head, worker = cmd[9:i], cmd[i + 1:]
        self.assertEqual(head[head.index("--cwd") + 1], "/mnt/r/repos/wt")
        self.assertEqual(head[head.index("--runs-dir") + 1], placement_mod.to_wsl_path(str(self.runs)))
        self.assertEqual(worker[0], "claude")
        self.assertIsNone(kw.get("env"))

    def test_codex_argv_and_wsl_cwd_given_as_a_linux_path(self):
        self.start(backend="codex", model="gpt-6-luna", effort="xhigh", placement="wsl", cwd="/home/u/wt")
        cmd, _ = self.fake.calls[0]
        self.assertEqual(cmd[cmd.index("--cwd") + 1], "/home/u/wt")
        self.assertEqual(cmd[cmd.index("--") + 1:cmd.index("--") + 4], ["codex", "exec", "--json"])
        worker = cmd[cmd.index("--") + 1:]
        self.assertEqual(worker[worker.index("-s") + 1], "workspace-write")
        self.assertEqual(worker[worker.index("-C") + 1], "/home/u/wt")
        self.assertEqual(self.fake.calls[0][1]["stdin"], subprocess.DEVNULL)

    def test_codex_review_uses_read_only_even_on_a_workspace_write_seat(self):
        self.start(backend="codex", model="gpt-6.1-sol", placement="wsl", role="review", sandbox="workspace-write")
        cmd, _ = self.fake.calls[0]
        worker = cmd[cmd.index("--") + 1:]
        self.assertEqual(worker[worker.index("-s") + 1], "read-only")
        self.assertEqual(worker[worker.index("-C") + 1], "/mnt/r/repos/wt")
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", worker)

    def test_windows_placement_maps_mnt_paths_and_refuses_linux_only_ones(self):
        self.start(cwd="/mnt/r/repos/wt")
        cmd, _ = self.fake.calls[0]
        self.assertEqual(cmd[cmd.index("--cwd") + 1], r"R:\repos\wt")
        with self.assertRaises(launch.LaunchError):
            self.rl.launch(spec(run_id=8, cwd="/home/u/wt"))

    def test_missing_binary_on_windows_is_a_missing_launch_error(self):
        self.rl.which = lambda n: None
        with self.assertRaises(launch.LaunchError) as c:
            self.rl.launch(spec())
        self.assertTrue(c.exception.missing)

    def test_windows_resolves_the_real_exe_and_refuses_script_shims(self):
        asked = []
        self.rl.which = lambda n: asked.append(n) or {"claude.exe": r"C:\n\claude.exe", "claude": r"C:\n\claude.cmd"}.get(n)
        self.assertEqual(self.rl.windows_exe("claude"), r"C:\n\claude.exe")
        self.assertEqual(asked[0], "claude.exe")
        self.rl.which = lambda n: {"claude": r"C:\n\claude.CMD"}.get(n)
        with self.assertRaises(launch.LaunchError) as c:
            self.rl.launch(spec())
        self.assertIn("script shim", str(c.exception))
        self.rl.which = lambda n: {"claude": r"C:\n\claude.bat"}.get(n)
        with self.assertRaises(launch.LaunchError):
            self.rl.windows_exe("claude")

    def test_failed_detach_asks_the_runner_to_stop_before_raising(self):
        for i, fake in enumerate((FakeRunner(self.runs, state="starting", rc=1,
                                             info={"error": "runner exited before starting the worker"}),)):
            rid = 40 + i
            self.rl.run = fake
            with self.assertRaises(launch.LaunchError) as c:
                self.rl.launch(spec(run_id=rid))
            self.assertTrue(c.exception.cancel_requested)
            stop = json.loads((self.dir(rid) / runner.STOP_FILE).read_text())
            self.assertEqual(stop, {"reason": "requested", "nonce": NONCE})

    def test_detach_timeout_before_status_exists_still_cancels_by_nonce(self):
        def boom(*a, **k):
            raise subprocess.TimeoutExpired("x", 1)
        self.rl.run = boom
        with self.assertRaises(launch.LaunchError) as c:
            self.rl.launch(spec(run_id=50))
        self.assertTrue(c.exception.cancel_requested)         # the launch nonce is known: stop even before status.json
        self.assertEqual(json.loads((self.dir(50) / runner.STOP_FILE).read_text())["nonce"], NONCE)
        self.assertFalse((self.dir(50) / runner.STATUS_FILE).exists())

    def test_detach_timeout_with_a_starting_runner_cancels_it(self):
        fake = FakeRunner(self.runs, state="starting", info={"runner_pid": None})

        def slow(cmd, **kw):
            fake(cmd, **kw)
            st = json.loads((self.dir(51) / runner.STATUS_FILE).read_text())
            st["runner_pid"] = None
            runner.write_json_atomic(self.dir(51) / runner.STATUS_FILE, st)
            raise subprocess.TimeoutExpired("x", 1)
        self.rl.run = slow
        with self.assertRaises(launch.LaunchError) as c:
            self.rl.launch(spec(run_id=51))
        self.assertTrue(c.exception.cancel_requested)
        self.assertTrue((self.dir(51) / runner.STOP_FILE).exists())

    def test_missing_python_is_missing(self):
        def boom(*a, **k):
            raise FileNotFoundError("python")
        self.rl.run = boom
        with self.assertRaises(launch.LaunchError) as c:
            self.rl.launch(spec())
        self.assertTrue(c.exception.missing)

    def test_runner_that_died_before_starting_is_a_launch_error(self):
        self.rl.run = FakeRunner(self.runs, state="starting", rc=1, info={"error": "runner exited before starting the worker"})
        with self.assertRaises(launch.LaunchError):
            self.rl.launch(spec())

    def test_spawn_failure_in_status_is_a_launch_error_and_missing_when_no_such_file(self):
        self.rl.run = FakeRunner(self.runs, state="exited", reason="spawn-failed: [Errno 2] No such file or directory: 'claude'")
        with self.assertRaises(launch.LaunchError) as c:
            self.rl.launch(spec())
        self.assertTrue(c.exception.missing)

    def test_missing_working_directory_does_not_disable_the_backend(self):
        missing_cwd = str(Path(self.tmp.name) / 'missing-worktree')
        error = FileNotFoundError(2, 'No such file or directory', missing_cwd)
        with patch.object(placement_mod, 'spawn_worker', side_effect=error):
            status = runner.supervise('cwd-probe', self.runs, [sys.executable, '-c', 'pass'], cwd=missing_cwd)
        self.assertEqual(status['state'], 'exited')
        self.fake.reason = status['reason']
        self.fake.missing_executable = status.get('missing_executable')
        self.fake.state = 'exited'
        with self.assertRaises(launch.LaunchError) as caught:
            self.rl.launch(spec())
        self.assertFalse(caught.exception.missing)

    def test_runner_marks_a_missing_executable_so_the_backend_can_be_reprobed(self):
        binary = str(Path(self.tmp.name) / 'missing-executable')
        status = runner.supervise('binary-probe', self.runs, [binary], cwd=self.tmp.name)
        self.assertTrue(status['missing_executable'])
        self.fake.reason = status['reason']
        self.fake.missing_executable = status['missing_executable']
        self.fake.state = 'exited'
        with self.assertRaises(launch.LaunchError) as caught:
            self.rl.launch(spec())
        self.assertTrue(caught.exception.missing)

    def test_still_starting_after_detach_wait_still_gets_a_handle(self):
        self.rl.run = FakeRunner(self.runs, state="starting", rc=1,
                                 info={"runner_pid": None, "error": "runner still starting after 5s"})
        self.assertIsNotNone(self.start())

    def test_fresh_launch_of_a_reused_run_id_clears_old_files(self):
        d = self.dir()
        d.mkdir(parents=True)
        for n in (runner.OUT_FILE, runner.RUNNER_LOG, launch.TAIL_FILE):
            (d / n).write_text("OLD\n")
        runner.write_json_atomic(d / runner.RECORD_FILE, {"status": "done", "sha": "deadbee", "blocker": None, "note": ""})
        runner.write_json_atomic(d / runner.STOP_FILE, {"reason": "requested", "nonce": "OLDNONCE"})
        h = self.start()
        for n in (runner.OUT_FILE, runner.RUNNER_LOG, launch.TAIL_FILE, runner.RECORD_FILE, runner.STOP_FILE):
            self.assertFalse((d / n).exists(), n)
        self.set_status(state="exited", exit_code=0)
        self.assertEqual(wait_for(h.poll)["status"], "no-record")     # not the old record's "done"


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
            h = self.rl.launch(spec(run_id=rid))
            gate = threading.Event()
            orig = h.terminal

            def held(orig=orig, gate=gate):
                st = orig()
                if st:                      # the thread has seen the terminal status; hold it before it publishes
                    gate.set()
                    time.sleep(0.05)
                return st
            h.terminal = held
            self.set_status(rid, state="exited", exit_code=0)
            self.assertTrue(gate.wait(5))
            h.cancel()                      # cancel lands while the thread is between reading and publishing
            time.sleep(0.1)
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


if __name__ == "__main__":
    unittest.main()
