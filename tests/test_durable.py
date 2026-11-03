"""A brief Windows reader cannot lose a write, and a failed write leaves the target whole and no temp behind."""
import contextlib
import errno
import inspect
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from drover.durable import (NO_WAIT, ORDINARY, RELEASE, ROUTINE, TERMINAL, Wait, move, publish, remove, rmdir, write_bytes,
                            write_json, write_text)
from drover import durable, runner_supervise
from tests.disk_trace_scene import quick_state

REAL_REPLACE, REAL_UNLINK, REAL_RMDIR, REAL_FDOPEN = os.replace, os.unlink, os.rmdir, os.fdopen


class Clock:
    """A clock the retry loop can sleep on without waiting."""
    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)
        self.now += s


class Flaky:
    """A call that raises PermissionError for the first `fails` calls, or while `blocked()` says so, then does `real`."""
    def __init__(self, real=REAL_REPLACE, fails=0, blocked=None):
        self.real, self.fails, self.blocked, self.calls = real, fails, blocked, 0

    def __call__(self, *paths):
        self.calls += 1
        if self.calls <= self.fails or (self.blocked and self.blocked()):
            raise PermissionError(13, f"refused {self.calls}", str(paths[0]))
        return self.real(*paths)


def on_clock(clock):
    return patch.object(durable, "time", SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep))


class FullDisk:
    """The file a write opens, on a drive that runs out at `at`: 'write', 'flush' or, as buffered data meets it, 'close'."""
    def __init__(self, at):
        self.at, self.flushed = at, False

    def __call__(self, *args, **options):
        self.stream = REAL_FDOPEN(*args, **options)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.stream.close()
        if self.at == 'close':
            raise OSError(errno.ENOSPC, 'No space left on device')

    def write(self, data):
        if self.at == 'write':
            raise OSError(errno.ENOSPC, 'No space left on device')
        return self.stream.write(data)

    def flush(self):
        self.flushed = True
        if self.at == 'flush':
            raise OSError(errno.ENOSPC, 'No space left on device')
        self.stream.flush()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.target = self.dir / "status.json"
        self.clock = Clock()
        patcher = on_clock(self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def temps(self):
        return sorted(p.name for p in self.dir.glob("*.tmp"))

    def holding(self, text):
        self.target.write_text(text, encoding="utf-8")
        return self.target

    def spare_temp(self, text="new"):
        spare = self.dir / "spare.tmp"
        spare.write_text(text, encoding="utf-8")
        return spare


class Waits(unittest.TestCase):
    def test_wait_constants(self):
        self.assertEqual([w.seconds for w in (NO_WAIT, ROUTINE, ORDINARY, RELEASE, TERMINAL)], [0, .5, 2, 5, 30])
        self.assertEqual(ORDINARY.step_s, .05)

    def test_sync_has_no_default(self):
        for write in (write_bytes, write_text, write_json):
            self.assertIs(inspect.signature(write).parameters['sync'].default, inspect.Parameter.empty)
            with self.assertRaises(TypeError):
                write('unused', b'' if write is write_bytes else '')


class Retry(Base):
    def test_refused_replace_retries_until_the_wait_runs_out(self):
        flaky = Flaky(blocked=lambda: True)
        with patch.object(durable.os, "replace", flaky), self.assertRaises(PermissionError):
            write_json(self.target, {}, sync=False, wait=Wait(1, step_s=.25))
        self.assertEqual((flaky.calls, self.clock.slept), (5, [.25] * 4))

    def test_a_replace_that_fails_a_few_times_then_succeeds_lands(self):
        flaky = Flaky(fails=5)
        with patch.object(durable.os, "replace", flaky):
            write_json(self.target, {"a": 1}, sync=False)
        self.assertEqual((json.loads(self.target.read_text()), flaky.calls, self.temps()), ({"a": 1}, 6, []))

    def test_a_longer_wait_outlasts_a_conflict_the_default_gives_up_on(self):
        until = lambda: self.clock.now < 1000.0 + ORDINARY.seconds + 3
        with patch.object(durable.os, "replace", Flaky(blocked=until)), self.assertRaises(PermissionError):
            write_json(self.target, {"a": 1}, sync=False)
        self.clock.now = 1000.0
        with patch.object(durable.os, "replace", Flaky(blocked=until)):
            write_json(self.target, {"a": 2}, sync=False, wait=TERMINAL)
        self.assertEqual((json.loads(self.target.read_text()), self.temps()), ({"a": 2}, []))

    def test_only_permission_error_is_retried(self):
        for error in (OSError("disk gone"), FileNotFoundError("gone"), IsADirectoryError("no")):
            with patch.object(durable.os, "replace", side_effect=error) as replace, self.assertRaises(type(error)):
                write_json(self.target, {}, sync=False, wait=TERMINAL)
            self.assertEqual((replace.call_count, self.clock.slept, self.temps()), (1, [], []))

    def test_a_write_that_lands_reads_no_clock(self):
        clock = MagicMock()
        spare, moved = self.spare_temp(), self.dir / "moved"
        with patch.object(durable, "time", clock):
            write_json(self.target, {}, sync=False)
            publish(spare, self.target)
            move(self.target, moved)
            remove(moved)
            remove(moved)
        self.assertEqual(clock.mock_calls, [])

    def test_the_clock_starts_at_the_first_refusal(self):
        """A long write before the replace takes nothing from the wait."""
        def slow(*args, **options):
            self.clock.now += 100
            return REAL_FDOPEN(*args, **options)
        flaky = Flaky(fails=3)
        with patch.object(durable.os, "fdopen", slow), patch.object(durable.os, "replace", flaky):
            write_json(self.target, {}, sync=False, wait=ROUTINE)
        self.assertEqual(flaky.calls, 4)

    def test_last_permission_error_is_raised_and_temp_is_gone(self):
        self.holding('{"old": true}')
        flaky = Flaky(blocked=lambda: True)
        with patch.object(durable.os, "replace", flaky), self.assertRaises(PermissionError) as raised:
            write_json(self.target, {"a": 1}, sync=False)
        self.assertEqual(raised.exception.strerror, f"refused {flaky.calls}")
        self.assertEqual((self.temps(), json.loads(self.target.read_text())), ([], {"old": True}))
        self.assertGreaterEqual(self.clock.now - 1000.0, ORDINARY.seconds)


class Writes(Base):
    def test_target_whole_or_unchanged_on_failure(self):
        self.holding('{"old": true}')
        refused, bad_sync = patch.object(durable.os, "replace", Flaky(blocked=lambda: True)), \
            patch.object(durable.os, "fsync", side_effect=OSError("io"))
        for failure, value, sync, error in ((refused, {}, False, PermissionError), (bad_sync, {}, True, OSError),
                                            (patch.object(durable, "time"), {"a": object()}, False, TypeError)):
            with failure, self.assertRaises(error):
                write_json(self.target, value, sync=sync, wait=NO_WAIT)
            self.assertEqual((self.target.read_text(), self.temps()), ('{"old": true}', []))

    def test_temp_is_unique_and_in_the_target_folder(self):
        names = []

        def noted(temporary, path):
            names.append(Path(temporary).name)
            return REAL_REPLACE(temporary, path)
        real = tempfile.mkstemp
        with patch.object(durable.tempfile, "mkstemp", side_effect=real) as mkstemp, patch.object(durable.os, "replace", noted):
            write_bytes(self.target, b"a", sync=False)
            write_bytes(str(self.target), b"b", sync=False)
        for call in mkstemp.call_args_list:
            self.assertEqual(call.kwargs, dict(dir=self.dir, prefix="status.json.", suffix=".tmp"))
        self.assertEqual(len(set(names)), 2)

    def test_posix_temp_takes_umask_default_not_0600(self):
        seen = []

        def chmod(path, mode):
            seen.append((Path(path).suffix, Path(path).exists(), self.target.exists(), mode))
        with patch.object(durable, "POSIX", True), patch.object(durable, "_UMASK", 0o027), patch.object(durable.os, "chmod", chmod):
            write_text(self.target, "x", sync=False)
        self.assertEqual(seen, [(".tmp", True, False, 0o640)])      # set before the file is published
        with patch.object(durable, "POSIX", False), patch.object(durable.os, "chmod", chmod):
            write_text(self.target, "x", sync=False)
        self.assertEqual(len(seen), 1)
        self.assertEqual(os.umask(durable._UMASK), durable._UMASK)  # the reading is true and left the process's umask alone

    def test_private_keeps_the_temps_0600(self):
        for write, content in ((write_bytes, b"x"), (write_text, "x"), (write_json, {})):
            with patch.object(durable, "POSIX", True), patch.object(durable.os, "chmod") as chmod:
                write(self.target, content, sync=False, private=True)
            chmod.assert_not_called()
            self.assertIs(inspect.signature(write).parameters["private"].default, False)
            self.assertIs(inspect.signature(write).parameters["private"].kind, inspect.Parameter.KEYWORD_ONLY)

    @unittest.skipUnless(os.name == "posix", "POSIX file modes")
    def test_posix_file_has_the_default_mode(self):
        write_text(self.target, "x", sync=False)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o666 & ~durable._UMASK)

    @unittest.skipUnless(os.name == "posix", "POSIX file modes")
    def test_posix_private_file_is_0600(self):
        for write, content in ((write_bytes, b"x"), (write_text, "x"), (write_json, {})):
            write(self.target, content, sync=False, private=True)
            self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)

    def test_write_json_keeps_each_encoding(self):
        write_json(self.target, {"a": float("nan")}, sync=False)
        self.assertEqual(self.target.read_text(), json.dumps({"a": float("nan")}, indent=1))
        write_json(self.target, {"a": 1}, sync=False, indent=None, allow_nan=False)
        self.assertEqual(self.target.read_text(), '{"a": 1}')
        with self.assertRaises(ValueError):
            write_json(self.target, {"a": float("nan")}, sync=False, indent=None, allow_nan=False)
        self.assertEqual((self.target.read_text(), self.temps()), ('{"a": 1}', []))

    def test_each_write_lands_exact_content(self):
        write_bytes(self.target, b"\x00\r\n\xff", sync=False)
        self.assertEqual(self.target.read_bytes(), b"\x00\r\n\xff")
        write_text(self.target, "é\n", sync=False)
        self.assertEqual(self.target.read_bytes(), ("é" + os.linesep).encode("utf-8"))
        write_text(self.target, "é\n", sync=False, newline="\n")
        self.assertEqual(self.target.read_bytes(), "é\n".encode("utf-8"))
        write_text(self.target, "a\n", sync=False, newline="\r\n")
        self.assertEqual(self.target.read_bytes(), b"a\r\n")

    def test_sync_flushes_all_of_it_to_one_fsync_before_the_replace(self):
        events = []
        replace = lambda *paths: (events.append("replace"), REAL_REPLACE(*paths))
        fsync = lambda fd: events.append(("fsync", os.fstat(fd).st_size))
        with patch.object(durable.os, "fsync", fsync), patch.object(durable.os, "replace", replace):
            write_bytes(self.target, b"12345", sync=True)
            self.assertEqual(events, [("fsync", 5), "replace"])
            events.clear()
            write_bytes(self.target, b"12345", sync=False)
            self.assertEqual(events, ["replace"])

    def test_the_trace_scenes_stub_the_fsync_a_sync_makes(self):
        """The decision trace pays for no fsync: a sync through `durable` must reach what `quick_state` replaces."""
        with patch.object(durable.os, "fsync", side_effect=AssertionError("a real fsync")):
            stubs = quick_state()
            for stub in stubs:
                stub.start()
            try:
                write_bytes(self.target, b"x", sync=True)
            finally:
                for stub in reversed(stubs):
                    stub.stop()

    def test_disk_full_leaves_target_whole_removes_temp_and_raises_at_once(self):
        self.holding('{"old": true}')
        at_write, at_flush, at_close = (patch.object(durable.os, "fdopen", FullDisk(at)) for at in ("write", "flush", "close"))
        at_fsync = patch.object(durable.os, "fsync", side_effect=OSError(errno.ENOSPC, "No space left on device"))
        for full, sync in ((at_write, True), (at_flush, True), (at_fsync, True), (at_close, False)):
            with full, patch.object(durable.os, "replace") as replace, self.assertRaises(OSError) as raised:
                write_json(self.target, {"a": 1}, sync=sync, wait=TERMINAL)
            self.assertEqual(raised.exception.errno, errno.ENOSPC)
            self.assertEqual((replace.call_count, self.clock.slept), (0, []))
            self.assertEqual((self.target.read_text(), self.temps()), ('{"old": true}', []))
        self.assertFalse(at_close.new.flushed)      # without a sync, only the close meets the full drive

    def test_a_reader_on_the_temp_gets_a_wait_and_never_hides_the_error(self):
        unlink = Flaky(real=REAL_UNLINK, fails=2)
        with patch.object(durable.os, "replace", side_effect=OSError("disk gone")), patch.object(durable.os, "unlink", unlink), \
                self.assertRaisesRegex(OSError, "disk gone"):
            write_json(self.target, {}, sync=False)
        self.assertEqual((unlink.calls, self.temps()), (3, []))
        with patch.object(durable.os, "replace", side_effect=OSError("disk gone")), \
                patch.object(durable.os, "unlink", Flaky(real=REAL_UNLINK, blocked=lambda: True)), \
                self.assertRaisesRegex(OSError, "disk gone"):
            write_json(self.target, {}, sync=False)

    def test_missing_parent_raises_at_once_and_makes_no_directory(self):
        parent = self.dir / "absent"
        with self.assertRaises(FileNotFoundError):
            write_text(parent / "x.json", "x", sync=False, wait=TERMINAL)
        self.assertEqual((parent.exists(), self.clock.slept), (False, []))

    def test_two_threads_publishing_one_path_both_land_whole(self):
        texts = ["a" * 4096, "b" * 4096]
        errors, seen = [], set()
        ready = threading.Barrier(2)

        def writer(text):
            ready.wait()
            try:
                for _ in range(20):
                    write_text(self.target, text, sync=False, wait=Wait(3600))    # the fake clock runs far faster than a refusal lasts
                    with contextlib.suppress(PermissionError):                    # Windows refuses this read during the other's replace
                        seen.add(self.target.read_text())
            except OSError as error:
                errors.append(error)
        threads = [threading.Thread(target=writer, args=(text,)) for text in texts]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual((errors, self.temps()), ([], []))
        self.assertLessEqual(seen, set(texts))
        self.assertIn(self.target.read_text(), texts)


class RunFiles(Base):
    def status(self):
        return runner_supervise.Status(self.target, "r1", "n" * 16)

    def refused_for(self, write):
        self.clock.now = 1000.0
        with patch.object(durable.os, "replace", Flaky(blocked=lambda: True)), contextlib.redirect_stderr(io.StringIO()), \
                contextlib.suppress(PermissionError):
            self.assertFalse(write(state="running"))
        return self.clock.now - 1000.0

    def test_status_routine_heartbeat_waits_half_a_second_and_terminal_thirty(self):
        status = self.status()
        for write, wait in ((status.routine, ROUTINE), (status.set, ORDINARY), (status.terminal, TERMINAL)):
            self.assertAlmostEqual(self.refused_for(write), wait.seconds, delta=wait.step_s * 1.5)
        self.assertEqual(self.temps(), [])

    def test_status_routine_keeps_nan_writable(self):
        self.assertTrue(self.status().routine(peaks={"cpu": float("nan")}))
        self.assertIn("NaN", self.target.read_text())


class Publish(Base):
    def test_publish_removes_temp_on_failure_and_syncs_directory_when_asked(self):
        spare = self.spare_temp()
        with patch.object(durable.os, "replace", Flaky(blocked=lambda: True)), self.assertRaises(PermissionError):
            publish(spare, self.target, wait=NO_WAIT)
        self.assertEqual((spare.exists(), self.target.exists()), (False, False))

        spare, calls = self.spare_temp(), []
        fake_os = dict(open=lambda path, flags: calls.append(("open", path)) or 7, close=lambda fd: calls.append(("close", fd)),
                       fsync=lambda fd: calls.append(("fsync", fd)))
        patches = [patch.object(durable.os, name, value) for name, value in fake_os.items()]
        with patch.object(durable, "POSIX", True), patches[0], patches[1], patches[2]:
            publish(spare, self.target, sync_dir=True)
        self.assertEqual(calls, [("open", self.dir), ("fsync", 7), ("close", 7)])
        self.assertEqual(self.target.read_text(), "new")

    def test_publish_makes_no_directory_sync_unless_asked_or_on_windows(self):
        with patch.object(durable.os, "fsync") as fsync, patch.object(durable, "POSIX", True):
            publish(self.spare_temp(), self.target)
        with patch.object(durable.os, "fsync") as windows, patch.object(durable, "POSIX", False):
            publish(self.spare_temp(), self.target, sync_dir=True)
        self.assertEqual((fsync.call_count, windows.call_count), (0, 0))

    def test_move_does_no_temp_handling_and_no_wait_is_one_attempt(self):
        source = self.spare_temp()
        flaky = Flaky(blocked=lambda: True)
        with patch.object(durable.os, "replace", flaky), self.assertRaises(PermissionError):
            move(source, self.target)
        self.assertEqual((flaky.calls, self.clock.slept, source.exists()), (1, [], True))
        with patch.object(durable.os, "replace", Flaky(fails=2)):
            move(source, self.target, wait=ORDINARY)
        self.assertEqual((source.exists(), self.target.read_text()), (False, "new"))


class Remove(Base):
    def test_remove_and_rmdir_outlast_a_reader_and_missing_ok(self):
        until = lambda: self.clock.now < 1000.0 + RELEASE.seconds - 1
        self.holding("x")
        (self.dir / "folder").mkdir()
        with patch.object(durable.os, "unlink", Flaky(real=REAL_UNLINK, blocked=until)):
            remove(self.target)
        self.clock.now = 1000.0
        with patch.object(durable.os, "rmdir", Flaky(real=REAL_RMDIR, blocked=until)):
            rmdir(self.dir / "folder")
        self.assertEqual((self.target.exists(), (self.dir / "folder").exists()), (False, False))

        slept = list(self.clock.slept)
        remove(self.target)
        with self.assertRaises(FileNotFoundError):
            remove(self.target, missing_ok=False)
        with self.assertRaises(FileNotFoundError):
            rmdir(self.dir / "folder")
        self.assertEqual(self.clock.slept, slept)

    def test_the_release_wait_runs_out_and_raises(self):
        self.holding("x")
        (self.dir / "folder").mkdir()
        for call, name, path in ((remove, "unlink", self.target), (rmdir, "rmdir", self.dir / "folder")):
            self.clock.now = 1000.0
            with patch.object(durable.os, name, Flaky(blocked=lambda: True)), self.assertRaises(PermissionError):
                call(path)
            self.assertGreaterEqual(self.clock.now - 1000.0, RELEASE.seconds)

    def test_rmdir_of_a_full_folder_raises_at_once(self):
        (self.dir / "folder").mkdir()
        (self.dir / "folder" / "x").write_text("x")
        with self.assertRaises(OSError):
            rmdir(self.dir / "folder", wait=TERMINAL)
        self.assertEqual(self.clock.slept, [])


if __name__ == "__main__":
    unittest.main()
