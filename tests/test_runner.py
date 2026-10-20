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
        v = cond()
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
        spec = importlib.util.spec_from_file_location("drover._placement_windows_test", placement.__file__)
        windows_runner = importlib.util.module_from_spec(spec)
        # Load the Windows implementation on either OS, without touching real jobs.
        with patch.object(sys, "platform", "win32"), \
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


@unittest.skipIf(runner.IS_WINDOWS, "WSL scratch lifecycle runs on Linux")
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


class RunTests(Base):
    def test_codex_final_message_preserves_ready_to_commit_subject_and_body(self):
        message = "Finish work\n\nExplain why."
        record = {"status": "ready_to_commit", "commit_message": message, "note": "tested"}
        event = {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(record)}}
        code = f"import json; print({json.dumps(event)!r})"
        r, st = self.run_runner("codex-ready", code)
        self.assertEqual((r.returncode, st["state"]), (0, "exited"))
        rec = json.loads((Path(self.runs) / "codex-ready" / "record.json").read_text())
        self.assertEqual((rec["status"], rec["sha"], rec["commit_message"]), ("ready_to_commit", "", message))

    def test_unframed_or_tool_ready_to_commit_is_not_trusted(self):
        record = json.dumps({"status": "ready_to_commit", "commit_message": "Do work"})
        path = Path(self.runs) / "fake.log"
        for text in (record, json.dumps({"type": "item.completed", "item": {"type": "command_execution", "text": record}})):
            path.write_text(text + "\n")
            self.assertIsNone(runner.scan_record(path))

    def test_ready_to_commit_without_a_valid_message_is_blocked(self):
        for message in (None, "", "  ", ["not text"], "bad\0message"):
            rec = runner.parse_action_record(json.dumps({"status": "ready_to_commit", "commit_message": message}))
            self.assertEqual((rec["status"], rec["blocker"]["kind"]), ("blocked", "no-commit-message"))

    def test_start_exit_status_and_record(self):
        code = ("import sys,json;print('hello');print('err',file=sys.stderr);"
                "print(json.dumps({'type':'result','result':json.dumps({'status':'done','sha':'abc1234','note':'ok'})}))")
        r, st = self.run_runner("r1", code)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((st["state"], st["exit_code"], st["reason"]), ("exited", 0, None))
        self.assertTrue(st["pid"] and st["runner_pid"] and st["started"] and st["ended"])
        self.assertIn("hello", self.out("r1"))
        self.assertIn("err", self.out("r1"))
        rec = json.loads((Path(self.runs) / "r1" / "record.json").read_text())
        self.assertEqual(rec, {"status": "done", "sha": "abc1234", "blocker": None, "note": "ok"})

    def test_forged_or_unframed_done_is_not_trusted(self):
        d = Path(self.runs)
        def scan(text):
            f = d / "o.log"
            f.write_text(text)
            return runner.scan_record(f)
        done = json.dumps({"status": "done", "sha": "abc1234"})
        self.assertIsNone(scan(done + "\n"))                      # raw tail can never say done
        forged = json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "content": done}]}})
        self.assertIsNone(scan(forged + "\n"))
        final = json.dumps({"type": "result", "result": "all good"})
        self.assertIsNone(scan(forged + "\n" + final + "\n"))   # the final message has no record
        self.assertEqual(scan(json.dumps({"status": "blocked", "blocker": {"kind": "k", "detail": "d"}}))["status"],
                         "blocked")
        bad_sha = json.dumps({"type": "result", "result": done.replace("abc1234", "zzz-not-hex")})
        self.assertEqual(scan(bad_sha)["blocker"]["kind"], "no-sha")
        big = json.dumps({"type": "result", "result": json.dumps(
            {"status": "blocked", "blocker": {"kind": "k" * 99, "detail": "d" * 5000}})})
        rec = scan(big)
        self.assertEqual((len(rec["blocker"]["kind"]), len(rec["blocker"]["detail"])), (40, 2000))

    def test_stale_stop_request_is_ignored_and_removed(self):
        d = Path(self.runs) / "st1"
        d.mkdir()
        (d / "stop-request").write_text(json.dumps({"reason": "requested", "nonce": "old"}))
        _, st = self.run_runner("st1", "print('fine')")
        self.assertEqual((st["state"], st["exit_code"]), ("exited", 0))
        self.assertFalse((d / "stop-request").exists())

    def test_stop_without_matching_nonce_does_nothing(self):
        p = self.start("st2", "import time;time.sleep(3)")
        self.assertTrue(wait_for(lambda: (Path(self.runs) / "st2" / "status.json").exists()
                                 and self.status("st2")["state"] == "running"))
        (Path(self.runs) / "st2" / "stop-request").write_text(json.dumps({"reason": "requested", "nonce": "bad"}))
        p.wait(timeout=30)
        self.assertEqual(self.status("st2")["state"], "exited")

    def test_stop_with_no_launch_fails(self):
        r = subprocess.run([PY, "-m", "drover.runner", "stop", "--run-id", "nope", "--runs-dir", self.runs],
                           env=ENV, capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)

    def test_nonzero_exit_and_no_record(self):
        r, st = self.run_runner("r2", "import sys;print('x');sys.exit(3)")
        self.assertEqual((st["state"], st["exit_code"]), ("exited", 3))
        self.assertFalse((Path(self.runs) / "r2" / "record.json").exists())

    def test_blocked_record_inside_stream_json_and_env_tag(self):
        code = ("import os,json;rec={'status':'blocked','blocker':{'kind':'k','detail':'d'},'note':'n'};"
                "print(os.environ['DROVER_RUN_ID']);"
                "print(json.dumps({'type':'result','result':'bye '+json.dumps(rec)}))")
        _, st = self.run_runner("r3", code)
        self.assertIn("r3", self.out("r3"))
        rec = json.loads((Path(self.runs) / "r3" / "record.json").read_text())
        self.assertEqual((rec["status"], rec["blocker"]), ("blocked", {"kind": "k", "detail": "d"}))

    def test_stdin_file(self):
        f = Path(self.runs) / "in.txt"
        f.write_text("brief text")
        self.run_runner("r4", "import sys;print('got:'+sys.stdin.read())", extra=["--stdin-file", str(f)])
        self.assertIn("got:brief text", self.out("r4"))

    def test_record_scan_is_bounded_to_tail(self):
        d = Path(self.runs) / "big"
        d.mkdir()
        (d / "out.log").write_text(json.dumps({"status": "done", "sha": "old"}) + "\n" + "x" * 20000)
        self.assertIsNone(runner.scan_record(d / "out.log"))

    def test_spawn_failure(self):
        r = subprocess.run([PY, "-m", "drover.runner", "--run-id", "r5", "--runs-dir", self.runs, "--",
                            "no-such-binary-xyz"], env=ENV, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 1)
        self.assertIn("spawn-failed", self.status("r5")["reason"])

    def test_wall_timeout(self):
        t = time.monotonic()
        _, st = self.run_runner("w1", "import time;print('hi',flush=True);time.sleep(60)", wall=1.5)
        self.assertEqual(st["state"], "timeout")
        self.assertIn("wall", st["reason"])
        self.assertLess(time.monotonic() - t, 30)

    def test_idle_timeout(self):
        code = "import time;[(print('tick',flush=True),time.sleep(0.3)) for _ in range(4)];time.sleep(60)"
        t = time.monotonic()
        _, st = self.run_runner("i1", code, idle=2)
        self.assertEqual(st["state"], "timeout")
        self.assertIn("no output", st["reason"])
        self.assertGreater(time.monotonic() - t, 2.5)        # the ticks reset the idle clock
        self.assertLess(time.monotonic() - t, 30)

    def test_step_log_growth_counts_as_activity(self):
        code = ("import time,sys;f=open(sys.argv[1],'ab',buffering=0)\n"
                "for _ in range(14): f.write(b'x');time.sleep(0.3)")
        t = time.monotonic()
        step = Path(self.runs) / "g1" / "receipt.step1.log"
        p = self.cmd("g1", code, idle=2)
        p += [str(step)]
        r = subprocess.run(p, env=ENV, capture_output=True, text=True, timeout=60)
        st = self.status("g1")
        self.assertEqual(st["state"], "exited", r.stderr)
        self.assertGreater(time.monotonic() - t, 2.5)        # silent on out.log for longer than idle_s

    def test_idle_timeout_names_the_stalled_step(self):
        step = Path(self.runs) / "g2" / "receipt.step2.log"
        code = f"import time;open({str(step)!r},'wb').write(b'x');time.sleep(60)"
        _, st = self.run_runner("g2", code, idle=2)
        self.assertEqual(st["state"], "timeout")
        self.assertIn("no output for 2s (stalled in step2)", st["reason"])

    def test_stop_request(self):
        p = self.start("s1", "import time;print('up',flush=True);time.sleep(60)")
        self.assertTrue(wait_for(lambda: (Path(self.runs) / "s1" / "status.json").exists()
                                 and self.status("s1")["state"] == "running"))
        subprocess.run([PY, "-m", "drover.runner", "stop", "--run-id", "s1", "--runs-dir", self.runs,
                        "--reason", "rotation"], env=ENV, check=True, capture_output=True)
        t = time.monotonic()
        p.wait(timeout=30)
        st = self.status("s1")
        self.assertEqual((st["state"], st["reason"]), ("killed", "rotation"))
        self.assertLess(time.monotonic() - t, 10)

    def test_grandchild_dies_on_kill(self):
        pidfile = Path(self.runs) / "gc.pid"
        code = ("import subprocess,sys,time;"
                "c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
                f"open({str(pidfile)!r},'w').write(str(c.pid));print('spawned',flush=True);time.sleep(60)")
        _, st = self.run_runner("g1", code, wall=3)
        self.assertEqual(st["state"], "timeout")
        gc = int(pidfile.read_text())
        self.assertTrue(wait_for(lambda: not alive(gc), 10), "grandchild survived")
        self.assertFalse(alive(st["pid"]))

    def test_grandchild_dies_when_worker_exits_early(self):
        pidfile = Path(self.runs) / "gc2.pid"
        code = ("import subprocess,sys;"
                "c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
                f"open({str(pidfile)!r},'w').write(str(c.pid))")
        self.run_runner("g2", code)
        gc = int(pidfile.read_text())
        self.assertTrue(wait_for(lambda: not alive(gc), 10), "straggler survived")


class DetachTests(Base):
    def test_detach_returns_at_once_and_runs(self):
        cmd = self.cmd("d1", "import time;time.sleep(1);print('late')", extra=["--detach"])
        t = time.monotonic()
        r = subprocess.run(cmd, env=ENV, capture_output=True, text=True, timeout=30)
        self.assertLess(time.monotonic() - t, 5)
        info = json.loads(r.stdout)
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
            with patch.multiple(placement, IS_WINDOWS=True, create=True, **flags), \
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

        with patch.multiple(placement, IS_WINDOWS=True, create=True, **flags), \
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
        orig = placement._powershell
        placement._powershell = lambda: "no-such-powershell-xyz"
        try:
            with self.assertRaises(runner.ScanError):
                runner.find_runners()
        finally:
            placement._powershell = orig


def ok_argv(rid):
    return ["python3", "-m", "drover.runner", "--run-id", rid]


def wsl_exe():
    return shutil.which("wsl.exe") or next((p for p in ("/mnt/c/Windows/System32/wsl.exe",) if os.path.exists(p)),
                                           None)


def default_distro(exe):
    try:
        r = subprocess.run([exe, "-l", "-q"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    names = r.stdout.decode("utf-16-le", errors="replace").replace("\x00", "").split()
    return next((n for n in names if not n.startswith("docker-desktop") and re.fullmatch(r"[A-Za-z0-9._-]+", n)), None)


class WslTests(unittest.TestCase):
    def test_wsl_argv_maps_paths_only_in_runner_options(self):
        a = RunnerLauncher("unused", distro="Ubuntu-24.04").runner_argv( ["--run-id", "x", "--runs-dir", "C:\\drover\\runs", "--stdin-file",
                                             "C:/drover/runs/x/in.txt", "--cwd=R:\\repos\\w", "--", "codex",
                                             "C:\\keep\\this", "--x=D:/keep"], cd="R:\\repos\\w")
        self.assertEqual(a[:8], ["wsl.exe", "-d", "Ubuntu-24.04", "--cd", "/mnt/r/repos/w", "--exec", "python3", "-m"])
        self.assertEqual(a[8], "drover.runner")
        self.assertNotIn("--", a[:6])
        self.assertIn("/mnt/c/drover/runs", a)
        self.assertIn("/mnt/c/drover/runs/x/in.txt", a)
        self.assertIn("--cwd=/mnt/r/repos/w", a)
        self.assertEqual(a[-3:], ["codex", "C:\\keep\\this", "--x=D:/keep"])

    def test_wsl_argv_rejects_bad_distro(self):
        for bad in ("a b", "x;rm", "", "-e", "a$(x)", "d\n"):
            with self.assertRaises(ValueError, msg=bad):
                RunnerLauncher("unused", distro=bad).runner_argv([])

    def test_live_wsl_passes_shell_metacharacters_literally(self):
        exe = wsl_exe()
        distro = exe and default_distro(exe)
        if not distro:
            self.skipTest("wsl.exe or a distro is absent")
        with tempfile.TemporaryDirectory() as runs:
            code = "import sys,json;print('ARGS'+json.dumps(sys.argv[1:]))"
            args = ["--run-id", "w1", "--runs-dir", runs, "--", "python3", "-c", code, "$(id -u)", "'", "a;echo INJECT"]
            argv = wsl_argv_for_exe(exe, distro, args)
            r = subprocess.run(argv, capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            out = (Path(runs) / "w1" / "out.log").read_text()
            line = next(l for l in out.splitlines() if l.startswith("ARGS"))
            self.assertEqual(json.loads(line[4:]), ["$(id -u)", "'", "a;echo INJECT"])


def wsl_argv_for_exe(exe, distro, args):
    argv = RunnerLauncher("unused", distro=distro).runner_argv(args, cd=PKG_ROOT)
    argv[0] = exe
    return argv


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(runner.IS_WINDOWS, 'configured writable roots use the Linux temporary filesystem')
class ConfiguredEnvironmentTests(Base):
    def test_runner_prepares_configured_roots_and_passes_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'cache'
            cfg = {'env': {'REPO_SETTING': 'literal value'}, 'sandbox_writable_roots': [str(root)]}
            code = ('import json,os,stat; from pathlib import Path; '
                    'print(json.dumps([os.environ["REPO_SETTING"],stat.S_IMODE(Path(' + repr(str(root)) + ').stat().st_mode)]))')
            proc, status = self.run_runner('configured', code, extra=('--run-config', json.dumps(cfg)))
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(status['exit_code'], 0)
            self.assertEqual(json.loads(self.out('configured')), ['literal value', 0o777])

    def test_runner_refuses_roots_outside_tmp_and_symlinked_roots(self):
        with tempfile.TemporaryDirectory() as temp:
            link = Path(temp) / 'link'
            link.symlink_to(Path(temp), target_is_directory=True)
            for root in ('/var/cache/never-create', str(link / 'escape')):
                with self.subTest(root=root):
                    cfg = {'sandbox_writable_roots': [root]}
                    proc, status = self.run_runner('rejected', 'raise RuntimeError("must not run")',
                                                   extra=('--run-config', json.dumps(cfg)))
                    self.assertEqual(proc.returncode, 1)
                    self.assertIn('spawn-failed:', status['reason'])
                    self.assertIsNone(status['exit_code'])


class NativeWindowsEnvironmentTests(Base):
    @unittest.skipUnless(runner.IS_WINDOWS, 'native Windows runner contract')
    def test_configured_linux_roots_do_not_prevent_native_workers(self):
        cfg = {'env': {'REPO_SETTING': 'native'}, 'sandbox_writable_roots': ['/tmp/repo-cache']}
        proc, status = self.run_runner('native-configured', 'import os; print(os.environ["REPO_SETTING"])',
                                       extra=('--run-config', json.dumps(cfg)))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(status['exit_code'], 0)
        self.assertEqual(self.out('native-configured').strip(), 'native')
