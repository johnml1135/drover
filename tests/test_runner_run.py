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

    def test_long_final_message_keeps_the_action_record(self):
        record = json.dumps({'status': 'done', 'sha': 'abc1234', 'note': 'ok'})
        for provider in ('claude', 'codex'):
            for text in ('x' * 30000 + record, record + 'x' * 30000):
                event = {'type': 'result', 'result': text} if provider == 'claude' else {
                    'type': 'item.completed', 'item': {'type': 'agent_message', 'text': text}}
                path = Path(self.runs) / 'long.log'
                path.write_text(json.dumps(event) + '\n')
                with self.subTest(provider=provider, record_at_start=text.startswith(record)):
                    self.assertEqual((runner.scan_record(path) or {}).get('status'), 'done')

    def test_large_single_line_output_does_not_stall_record_scanning(self):
        path = Path(self.runs) / 'large-line.log'
        path.write_bytes(b'x' * (16 * 1024 * 1024))
        started = time.monotonic()
        self.assertIsNone(runner.scan_record(path))
        self.assertLess(time.monotonic() - started, 2)

    def test_runner_persists_done_from_a_long_final_event(self):
        code = ('import json; text="x"*30000+json.dumps({"status":"done","sha":"abc1234"}); '
                'print(json.dumps({"type":"result","result":text}))')
        result, status = self.run_runner('long-final', code)
        self.assertEqual((result.returncode, status['exit_code']), (0, 0))
        record = json.loads((Path(self.runs) / 'long-final' / 'record.json').read_text())
        self.assertEqual((record['status'], record['sha']), ('done', 'abc1234'))

    def test_long_tool_message_cannot_replace_final_assistant_authority(self):
        path = Path(self.runs) / 'long-tool.log'
        record = json.dumps({'status': 'done', 'sha': 'abc1234'})
        events = [{'type': 'result', 'result': record},
                  {'type': 'item.completed', 'item': {'type': 'command_execution', 'text': 'x' * 30000 + record}}]
        path.write_text('\n'.join(json.dumps(event) for event in events))
        self.assertEqual((runner.scan_record(path) or {}).get('status'), 'done')
        events.append({'type': 'result', 'result': 'No delivery. ' + 'x' * 30000})
        path.write_text('\n'.join(json.dumps(event) for event in events))
        self.assertIsNone(runner.scan_record(path))

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
        read = self.status
        transient_reads = []
        def sharing_once(rid):
            if not transient_reads:
                transient_reads.append(True)
                raise PermissionError("transient status-file sharing error")
            return read(rid)
        with patch.object(self, "status", side_effect=sharing_once):
            self.assertTrue(wait_for(lambda: (Path(self.runs) / "s1" / "status.json").exists()
                                     and self.status("s1")["state"] == "running"))
        self.assertEqual(transient_reads, [True])
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
