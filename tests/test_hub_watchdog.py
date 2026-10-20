"""Watchdog decisions with a fake wsl.exe."""
import tempfile
import unittest
from pathlib import Path

from hub.events import Bus
from hub.settings import Settings
from hub.store import Store
from hub.watchdog import Watchdog


class Fake:
    def __init__(self, probe_ok=True):
        self.calls, self.probe_ok = [], probe_ok

    def __call__(self, args, timeout):
        self.calls.append(args)
        if args[-2:] == ["--exec", "true"] or args[-1] == "true":
            return (0 if self.probe_ok else -1), ""
        return 0, ""


class FakeKeepalive:
    def __init__(self, distro, seconds):
        self.distro, self.seconds, self.alive, self.killed = distro, seconds, True, False

    def poll(self):
        return None if self.alive else 0

    def kill(self):
        self.alive, self.killed = False, True

    def wait(self, timeout=None):
        return 0


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.bus = Bus()
        self.store = Store(Path(self.tmp.name) / "l.db", publish=self.bus.publish)
        self.addCleanup(self.store.close)
        self.t = 1000.0
        self.fake = Fake()
        self.settings = Settings(token="t", distro="Dist")
        self.dog = self.mk(self.fake)

    def mk(self, fake):
        self.spawned = []
        return Watchdog(self.store, self.settings, bus=self.bus, wsl=fake, now=lambda: self.t,
                        keepalive=lambda d, n: self.spawned.append(FakeKeepalive(d, n)) or self.spawned[-1])

    def need(self):
        self.store._put_json_setting("_wsl_needed", {"ts": self.t, "task": "T-1"})

    def test_no_note_does_nothing_even_with_a_stale_or_missing_heartbeat(self):
        self.t += 10000
        self.assertEqual(self.dog.check(), "ok")
        self.assertEqual(self.fake.calls, [])

    def test_never_restarts_a_dispatcher(self):
        self.need()
        self.dog.check()
        self.assertNotIn("systemctl", [a for c in self.fake.calls for a in c])

    def test_needed_and_wsl_up_starts_one_keepalive_and_probes_no_more(self):
        self.need()
        self.assertEqual(self.dog.check(), "keepalive")
        self.assertEqual(self.fake.calls, [["-d", "Dist", "--exec", "true"]])
        self.assertEqual((self.spawned[0].distro, self.spawned[0].seconds), ("Dist", 600.0))
        self.assertEqual(self.dog.check(), "ok")
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(len(self.spawned), 1)

    def test_keepalive_is_killed_when_no_wsl_work_remains_and_respawned_when_it_ends(self):
        self.need()
        self.dog.check()
        self.spawned[0].alive = False                     # the 600 s sleep ended while work is still due
        self.need()
        self.assertEqual(self.dog.check(), "keepalive")
        self.assertEqual(len(self.spawned), 2)
        self.t += 301                                     # the note expires: nothing is waiting for WSL
        self.assertEqual(self.dog.check(), "ok")
        self.assertTrue(self.spawned[1].killed)
        self.assertIsNone(self.dog.keepalive)

    def test_stop_kills_the_keepalive(self):
        self.need()
        self.dog.check()
        self.dog.stop()
        self.assertTrue(self.spawned[0].killed)

    def test_old_note_is_ignored(self):
        self.need()
        self.t += 301
        self.assertEqual(self.dog.check(), "ok")
        self.assertEqual(self.fake.calls, [])

    def test_wsl_down_two_checks_boots_without_shutdown(self):
        fake = Fake(probe_ok=False)
        dog = self.mk(fake)
        self.need()
        self.assertEqual(dog.check(), "wsl-down-wait")
        self.assertEqual(dog.check(), "boot-wsl")
        flat = [a for c in fake.calls for a in c]
        self.assertNotIn("--shutdown", flat)
        self.assertEqual(fake.calls[-1], ["-d", "Dist", "--exec", "true"])
        self.assertIn("watchdog.boot-wsl", [e["kind"] for e in self.store.list_events()])
        self.assertIn("alert", [e["kind"] for e in self.bus.recent()])
        self.assertEqual(len(self.spawned), 0)               # the boot probe failed (fake): no keepalive

    def test_boot_wsl_has_a_ten_minute_cooldown(self):
        fake = Fake(probe_ok=False)
        dog = self.mk(fake)
        self.need()
        dog.check()
        self.assertEqual(dog.check(), "boot-wsl")
        n = len(fake.calls)
        self.t += 30
        self.need()
        self.assertEqual(dog.check(), "boot-cooldown")
        self.assertEqual(len(fake.calls), n + 1)  # only the probe, no second boot
        self.t += 600
        self.need()
        self.assertEqual(dog.check(), "boot-wsl")

    def test_one_wsl_failure_then_recovery_resets(self):
        fake = Fake(probe_ok=False)
        dog = self.mk(fake)
        self.need()
        dog.check()
        fake.probe_ok = True
        self.assertEqual(dog.check(), "keepalive")
        self.assertEqual(dog.wsl_failures, 0)

    def test_distro_from_settings(self):
        self.store.update_settings({"wsl_distro": "Other"})
        self.need()
        self.dog.check()
        self.assertEqual(self.fake.calls[0][:2], ["-d", "Other"])


if __name__ == "__main__":
    unittest.main()
