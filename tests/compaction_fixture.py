"""The overnight compaction's fake runner, clock and job."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from drover import compaction, compaction_hold, disk, disk_state
from tests.disk_fixture import DiskCase


NOW = 2_000_000_000.0
VHDX = r'H:\WSL\Ubuntu\ext4.vhdx'
ROOT = Path(__file__).resolve().parents[1]
DONE_RECORD = '{"status": "done", "sha": "' + 'a' * 40 + '", "blocker": null, "note": "ok"}'


def done(stdout='', returncode=0):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr='')


class FakeRun:
    """Records every argv. `answers` maps a substring of the joined argv to a result, or to an exception to raise."""

    def __init__(self, **answers):
        self.calls, self.answers = [], answers

    def __call__(self, argv, **_kw):
        text = ' '.join(argv)
        self.calls.append(text)
        for key, value in self.answers.items():
            if key in text:
                if isinstance(value, Exception):
                    raise value
                return value
        return done()

    def seen(self, text):
        return [call for call in self.calls if text in call]


class Vhdx(FakeRun):
    """A disk with attached state. Like the real command, Dismount-VHD refuses a disk that is not attached."""

    def __init__(self, refuse_mount=False, refuse_dismount=False, attached=False):
        super().__init__()
        self.refuse_mount, self.refuse_dismount, self.attached = refuse_mount, refuse_dismount, attached

    def __call__(self, argv, **kw):
        text = ' '.join(argv)
        if ' Mount-VHD ' in text:
            self.calls.append(text)
            self.attached = not self.refuse_mount
            return done('', 1 if self.refuse_mount else 0)
        if 'Dismount-VHD' in text:
            self.calls.append(text)
            if not self.attached:
                return done('', 0 if '.Attached' in text else 1)
            if self.refuse_dismount:
                return done('', 1)
            self.attached = False
            return done()
        return super().__call__(argv, **kw)


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Job(DiskCase):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.fake = FakeRun()
        self.ack = dict(stopped=[1], killed=[2], requeued=['T-1'], drained=True)
        patcher = mock.patch.object(compaction_hold, 'clock', self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def runtime(self, cls=compaction.Runtime):
        job = cls(self.cfg, run=self.fake, clock=self.clock, sleep=self.clock.sleep)
        job.vhdx = lambda: VHDX
        job.wait_for_hub = lambda record: dict(self.ack)
        return job

    def run_job(self, cls=compaction.Runtime):
        return compaction.run(self.cfg, runtime=self.runtime(cls), now=NOW)

    def hold(self):
        return compaction_hold.read(self.state)

    def report(self):
        return json.loads((self.state / 'disk' / compaction_hold.REPORT_FILE).read_text(encoding='utf-8'))

    def outbox(self):
        return disk_state.read(self.state).get('outbox', {})
