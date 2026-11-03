"""A status write that meets a Windows reader holding the file (a sharing conflict on /mnt/c) is a transient."""
import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from drover import runner_common, runner_supervise
from drover.runner_common import REPLACE_WAIT_S, TERMINAL_WAIT_S

REAL_REPLACE = os.replace


class Clock:
    """A clock the retry loop can sleep on without waiting."""
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.now += s


class Flaky:
    """os.replace that raises PermissionError for the first `fails` calls, or while `blocked()` says so."""
    def __init__(self, fails=0, blocked=None):
        self.fails, self.blocked, self.calls = fails, blocked, 0

    def __call__(self, src, dst):
        self.calls += 1
        if self.calls <= self.fails or (self.blocked and self.blocked()):
            raise PermissionError(13, "Permission denied", str(src))
        return REAL_REPLACE(src, dst)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.clock = Clock()
        patcher = patch.object(runner_common, "time", SimpleNamespace(
            monotonic=self.clock.monotonic, sleep=self.clock.sleep))
        patcher.start()
        self.addCleanup(patcher.stop)

    def temps(self):
        return sorted(p.name for p in self.dir.glob("*.tmp"))


class FakeProc:
    """A worker that runs for `ticks` polls, then exits 0; `on_exit` runs when it does."""
    pid = 4242

    def __init__(self, ticks, on_exit=None):
        self.ticks, self.on_exit = ticks, on_exit

    def poll(self):
        if self.ticks > 0:
            self.ticks -= 1
            return None
        if self.on_exit:
            self.on_exit()
        return 0

    def wait(self):
        return 0


class FakeTree:
    metrics = staticmethod(lambda: (0.0, 1024 ** 2, 0))
    kill = reap_stragglers = close = lambda self, *a: None


class Supervise(Base):
    def run_supervise(self, proc, replace):
        """_supervise over a fake worker; each loop pass counts as 2s for the one-second heartbeat throttle."""
        real = time.monotonic
        ticks = [0]

        def mono():
            ticks[0] += 1
            return real() + 2 * ticks[0]

        st = runner_supervise.Status(self.dir / "status.json", "r1", "n" * 16)
        out = self.dir / "out.log"
        out.write_bytes(b"")
        err = io.StringIO()
        with patch("drover.placement.spawn_worker", return_value=(proc, FakeTree())), \
                patch("drover.placement.cleanup_process_record", lambda *a, **k: contextlib.nullcontext()), \
                patch.object(runner_supervise, "time", SimpleNamespace(monotonic=mono, sleep=lambda s: None)), \
                patch("os.replace", replace), contextlib.redirect_stderr(err):
            result = runner_supervise._supervise(st, self.dir, out, ["worker"], 0, 0, None, None, 1, 0, "n" * 16, [])
        return result, err.getvalue()

    def status(self):
        return json.loads((self.dir / "status.json").read_text())

    def test_a_heartbeat_that_always_fails_does_not_end_supervision(self):
        ending = []
        replace = Flaky(blocked=lambda: not ending)
        result, _ = self.run_supervise(FakeProc(6, on_exit=lambda: ending.append(1)), replace)
        self.assertEqual((result["state"], result["exit_code"]), ("exited", 0))
        self.assertEqual((self.status()["state"], self.temps()), ("exited", []))
        self.assertGreater(replace.calls, 3)      # it tried again on later ticks, not once

    def test_the_terminal_write_retries_longer_than_a_heartbeat(self):
        exit_at = []
        replace = Flaky(blocked=lambda: exit_at and self.clock.now < exit_at[0] + REPLACE_WAIT_S + 3)
        result, err = self.run_supervise(FakeProc(3, on_exit=lambda: exit_at.append(self.clock.now)), replace)
        self.assertEqual((self.status()["state"], err, self.temps()), ("exited", "", []))
        self.assertGreater(self.clock.now - exit_at[0], REPLACE_WAIT_S)

    def test_a_terminal_write_that_cannot_land_reports_the_outcome_and_leaves_no_temp(self):
        result, err = self.run_supervise(FakeProc(0), Flaky(blocked=lambda: True))
        self.assertEqual((result["state"], result["exit_code"]), ("exited", 0))
        self.assertEqual(self.temps(), [])
        self.assertIn("could not write the terminal status", err)
        self.assertEqual(json.loads(err.splitlines()[-1])["state"], "exited")
        self.assertGreaterEqual(self.clock.now - 1000.0, TERMINAL_WAIT_S)


if __name__ == "__main__":
    unittest.main()
