"""Artifact completion metadata across runner output, persistence and launch recovery."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from drover import launch, runner
from tests.test_launch_runner import FakeRunner, spec


ARTIFACT = {
    "uri": "hub:tasks/T-r/result.md",
    "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
}
DONE = {"status": "done", "sha": "", "artifact": ARTIFACT, "blocker": None, "note": "Evidence ready"}
NONCE = "0123456789abcdef0123456789abcdef"


def final_frames(text):
    return (
        {"type": "result", "result": text},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
    )


class ArtifactRecords(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.runs = Path(temp.name)

    def test_artifact_record_roundtrip(self):
        text = json.dumps(DONE)
        self.assertEqual(runner.parse_action_record(text), DONE)
        for index, frame in enumerate(final_frames(text)):
            with self.subTest(frame=frame["type"]):
                rid = str(index)
                # Bound the real runner as well as its worker: a regression must fail before the gate idles out.
                result = subprocess.run([
                    sys.executable, "-m", "drover.runner", "--run-id", rid, "--runs-dir", str(self.runs),
                    "--nonce", NONCE, "--wall-s", "10", "--kill-after", "1", "--",
                    sys.executable, "-c", f"print({json.dumps(frame)!r})",
                ], capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                status = runner.read_status(self.runs, rid)
                self.assertEqual((status["state"], status["exit_code"]), ("exited", 0))
                directory = self.runs / rid
                self.assertEqual(runner.scan_record(directory / runner.OUT_FILE), DONE)
                self.assertEqual(json.loads((directory / runner.RECORD_FILE).read_text()), DONE)
                self.assertEqual(list(directory.glob("record.json.*.tmp")), [])
                handle = launch.RunnerHandle({"run_id": rid, "task": {"id": "T-r", "title": "artifact"}},
                                             self.runs, None, autostart=False)
                self.assertEqual(handle._record(), DONE)
                result = handle._finish(status)
                self.assertEqual((result["status"], result["record"]), ("done", DONE))

    def test_artifact_recovery(self):
        self.assertIs(launch.parse_action_record, runner.parse_action_record)
        fake = FakeRunner(self.runs, state="exited")
        launcher = launch.RunnerLauncher(runs_dir=self.runs, run=fake, windows=False, autostart=False)
        original = launcher.launch(spec())
        runner.write_json_atomic(original.dir / runner.RECORD_FILE, DONE)
        status = original.status()
        self.assertEqual((status["run_id"], status["nonce"]), ("7", NONCE))
        self.assertEqual(status["state"], "exited")
        # New launcher instances stand in for dispatcher restarts; adoption reads saved files only.
        for _ in range(2):
            restarted = launch.RunnerLauncher(runs_dir=self.runs, run=fake, windows=False)
            handle = restarted.adopt(spec(adopted=True))
            self.addCleanup(handle.cancel)
            result = handle.wait(5)
            self.assertIsNotNone(result)
            self.assertEqual(handle.poll(), result)
            self.assertEqual((result["status"], result["record"], result["adopted"]), ("done", DONE, True))
            self.assertEqual(json.loads((original.dir / runner.RECORD_FILE).read_text()), DONE)
        self.assertEqual(len(fake.calls), 1)

    def test_tool_blocks_in_assistant_frames_never_complete(self):
        text = json.dumps(DONE)
        path = self.runs / runner.OUT_FILE
        for kind in ("tool_result", "tool_use", "thinking"):
            for final_text in ([], [{"type": "text", "text": "No completion record"}]):
                with self.subTest(kind=kind, final_text=final_text):
                    frame = {"type": "assistant", "message": {"content": [
                        {"type": kind, "text": text}, *final_text]}}
                    path.write_text(json.dumps(frame) + "\n", encoding="utf-8")
                    self.assertIsNone(runner.scan_record(path))

    def test_record_authority_and_legacy(self):
        text = json.dumps(DONE)
        path = self.runs / runner.OUT_FILE
        tool_frames = (
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": text}]}},
            {"type": "item.completed", "item": {"type": "command_execution", "text": text}},
            {"type": "tool_result", "result": text},
        )
        for output in (text, *(json.dumps(frame) for frame in tool_frames)):
            with self.subTest(untrusted=output):
                path.write_text(output + "\n", encoding="utf-8")
                self.assertIsNone(runner.scan_record(path))
        for frame in final_frames(text):
            with self.subTest(earlier_final=frame["type"]):
                path.write_text(json.dumps(frame) + '\n' + json.dumps({"type": "result", "result": "no record"}),
                                encoding="utf-8")
                self.assertIsNone(runner.scan_record(path))

        for sha in (None, "", "not-a-sha"):
            with self.subTest(missing_evidence=sha):
                record = runner.parse_action_record(json.dumps({"status": "done", "sha": sha}))
                self.assertEqual((record["status"], record["blocker"]["kind"]), ("blocked", "no-sha"))
        invalid_artifacts = [None, "result.md", [], {}, {"uri": ARTIFACT["uri"]}, {"sha256": ARTIFACT["sha256"]}]
        for uri in ("", "hub:tasks//result.md", "hub:tasks/../result.md", "hub:tasks/./result.md",
                    "hub:tasks/T-r/../result.md", "hub:tasks/T-r//result.md", "hub:tasks/T-r/result.md/",
                    "hub:tasks/T-r/result.md?x=1", "hub:tasks/T-r/result.md#fragment",
                    "hub:tasks/%54-r/result.md", "hub:tasks/T-r%2fother/result.md", "hub:tasks/T-r/result.txt",
                    "hub://tasks/T-r/result.md", "https://hub/tasks/T-r/result.md", "file:///result.md",
                    "hub:tasks/T-r\\result.md", " hub:tasks/T-r/result.md", "hub:tasks/T-r/result.md\n", 7):
            invalid_artifacts.append({**ARTIFACT, "uri": uri})
        for digest in ("", "a" * 63, "a" * 65, "A" * 64, "g" * 64, "a" * 64 + "\n", None, 7):
            invalid_artifacts.append({**ARTIFACT, "sha256": digest})
        for artifact in invalid_artifacts:
            with self.subTest(artifact=artifact):
                record = runner.parse_action_record(json.dumps({**DONE, "artifact": artifact}))
                self.assertEqual((record["status"], record["blocker"]["kind"]), ("blocked", "invalid-artifact"))
                self.assertNotIn("artifact", record)
        for sha in ("abc1234", "not-a-sha", " ", 7, False):
            with self.subTest(ambiguous_sha=sha):
                record = runner.parse_action_record(json.dumps({**DONE, "sha": sha}))
                self.assertEqual((record["status"], record["blocker"]["kind"]), ("blocked", "invalid-artifact"))

        legacy = {"status": "done", "sha": "abc1234", "blocker": None, "note": "ok"}
        self.assertEqual(runner.parse_action_record(json.dumps(legacy)), legacy)
        ready = {"status": "ready_to_commit", "sha": "", "commit_message": "Finish work\n\nExplain why.",
                 "blocker": None, "note": ""}
        self.assertEqual(runner.parse_action_record(json.dumps(ready)), ready)
        for message in (None, "", "  ", [], "bad\0message"):
            record = runner.parse_action_record(json.dumps({**ready, "commit_message": message}))
            self.assertEqual((record["status"], record["blocker"]["kind"]), ("blocked", "no-commit-message"))
        blocked = {"status": "blocked", "sha": "", "blocker": {"kind": "plan", "detail": "api differs"}, "note": ""}
        self.assertEqual(runner.parse_action_record(json.dumps(blocked)), blocked)


if __name__ == "__main__":
    unittest.main()
