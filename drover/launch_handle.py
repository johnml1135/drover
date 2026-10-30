"""Follow runner output and status files."""
import json
import os
import threading
import time
from collections import deque
from pathlib import Path

from . import runner
from .launch_usage import Usage, infrastructure_error_of, summarise, rotation_handoff

TAIL_LINES = 200
TAIL_FLUSH_EVERY = 5
POLL_S = 0.5
MAX_LINE = 1 << 18           # a longer line is cut (and the rest of it skipped) so a newline-less flood stays bounded
START_TIMEOUT_S = 90         # a run still `starting` after this is stopped and reported as an error
TERMINAL = ("exited", "killed", "timeout")
IN_FILE, TAIL_FILE = "in.txt", "tail.log"


class Tail:
    def __init__(self, path):
        self.path, self.lines, self.pending, self.error = Path(path), deque(maxlen=TAIL_LINES), 0, None

    def add(self, line):
        self.lines.append(line.rstrip("\n"))
        self.pending += 1
        if self.pending >= TAIL_FLUSH_EVERY:
            self.flush()

    def flush(self):
        """Never raises: a tail write failure is remembered once and the run carries on."""
        self.pending = 0
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text("\n".join(self.lines) + "\n", encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as e:
            self.error = self.error or repr(e)


class RunnerHandle:
    """Follows one run through <runs>/<id>/{status.json,out.log,record.json}, by offset, from the dispatcher's own
    launch or after a restart (adopt): it keeps no pipe to the worker. Stop is a nonce-checked stop-request file,
    the same one `python -m drover.runner stop` writes."""

    def __init__(self, spec, runs_dir, ceiling, pid=None, clock=time.time, poll_s=POLL_S,
                 start_timeout_s=START_TIMEOUT_S, tail=None, autostart=True):
        self.spec, self.runs_dir, self.ceiling = spec, runs_dir, ceiling
        self.run_id, self.pid, self.clock, self.poll_s = str(spec["run_id"]), pid, clock, poll_s
        self.placement, self.start_timeout_s = spec.get("placement", "windows"), start_timeout_s
        self.dir = runner.run_dir(runs_dir, self.run_id)
        self.tail = tail or Tail(self.dir / TAIL_FILE)
        self.started, self.offset, self.buf, self.skipping = clock(), 0, b"", False
        self.rotate, self.result, self.last_status = False, None, None
        self._lock, self._done, self._usage = threading.Lock(), threading.Event(), Usage()
        self._cancelled = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        if autostart:
            self._thread.start()

    # ---- output ---------------------------------------------------------------------------------------
    def _ingest(self, final=False):
        try:
            with open(self.dir / runner.OUT_FILE, "rb") as f:
                f.seek(self.offset)
                data = f.read(1 << 20)
        except OSError:
            data = b""
        self.offset += len(data)
        lines = (self.buf + data).split(b"\n")
        self.buf = lines.pop()
        if final and self.buf:
            lines.append(self.buf)
            self.buf = b""
        if len(self.buf) > MAX_LINE:                 # no newline yet and already too long: keep the head only
            lines.append(self.buf[:MAX_LINE])
            self.buf, self.skipping = b"", True
        for raw in lines:
            if self.skipping:                        # the rest of an over-long line
                self.skipping = False
                continue
            if not self.rotate:
                self._line(raw[:MAX_LINE].decode("utf-8", errors="replace"))
        if len(data) == 1 << 20:
            self._ingest(final)

    def _line(self, raw):
        try:
            ev = json.loads(raw)
        except ValueError:
            ev = None
        if isinstance(ev, dict):
            self._usage.feed(ev)
            self.tail.add(summarise(ev, raw.strip()))
        elif raw.strip():
            self.tail.add(raw.strip()[:200])
        if self.ceiling and self._usage.context > self.ceiling and not self.rotate:
            self.rotate = True
            self.stop("rotation")

    # ---- status ---------------------------------------------------------------------------------------
    def status(self):
        """status.json now (also remembered as last_status); None when it is unreadable."""
        st = runner.read_status(self.runs_dir, self.run_id)
        if st is not None:
            self.last_status = st
        return st

    def terminal(self):
        st = self.status()
        return st if st and st.get("state") in TERMINAL else None

    def stop(self, reason="requested"):
        """Ask the runner to end this run; False when no launch is recorded (nothing to stop)."""
        try:
            return runner.request_stop(self.runs_dir, self.run_id, reason)
        except OSError:
            return False

    def cancel(self):
        """Stop following this run (the dispatcher dropped it); no result is published, even by a pass in flight."""
        with self._lock:
            self._cancelled = True
        self._done.set()

    def _publish(self, result):
        with self._lock:
            if self._cancelled:
                return
            if self.result is None:
                self.result = result
        self._done.set()

    def _run(self):
        try:
            while not self._done.is_set():
                self._ingest()
                st = self.terminal()
                if st:
                    self._ingest(final=True)
                    self.tail.flush()
                    return self._publish(self._finish(st))
                if self.last_status and self.last_status.get("state") == "starting" \
                        and self.clock() - self.started > self.start_timeout_s:
                    self.stop()                     # the runner may yet start: its stop-request then ends it at once
                    return self._publish(self._error(f"runner still starting after {self.start_timeout_s:g}s"))
                self._done.wait(self.poll_s)
        except Exception as e:  # a crash must surface as a result
            self._publish(self._error(f"handle failed: {e!r}"))

    def _error(self, text):
        return {"status": "error", "record": None, "usage": self._usage.as_dict(), "text": text, "handoff": None,
                "returncode": None, "timeout": None, "tail_error": self.tail.error, "adopted": False}

    def _record(self):
        try:
            obj = json.loads((self.dir / runner.RECORD_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return runner._record_from(obj)

    def _finish(self, st):
        usage, record = self._usage, self._record()
        state, reason, rc = st.get("state"), st.get("reason") or "", st.get("exit_code")
        rotate = self.rotate or (state == "killed" and reason == "rotation")
        if record:                          # a finished record beats rotation and a late stop
            status, rotate = record["status"], False
        elif rotate:
            status = "rotate"
        elif state == "timeout":
            status = "timeout"
        elif state == "killed" or rc not in (0, None) or reason.startswith("spawn-failed"):
            status = "error"
        else:
            status = "no-record"
        text = usage.text if status not in ("error", "timeout") or not reason else f"{reason}. {usage.text}"
        try:
            with (self.dir / runner.OUT_FILE).open('rb') as output:
                diagnostic = output.read(16384).decode('utf-8', errors='replace')
        except OSError:
            diagnostic = ''
        infrastructure = None if record or self.spec.get('role') == 'gate' else infrastructure_error_of(
            st, usage.model_started, diagnostic)
        return {"status": status, "record": record, "usage": usage.as_dict(), "text": text,
                "infrastructure_error": infrastructure,
                "handoff": rotation_handoff(self.spec["task"], usage, self.ceiling) if rotate else None,
                "returncode": rc, "timeout": reason if state == "timeout" else None, "tail_error": self.tail.error,
                "provider_error": None if record else usage.provider_error,
                "adopted": bool(self.spec.get("adopted")), "peaks": st.get("peaks", {})}

    def poll(self):
        return self.result if self._done.is_set() else None

    def wait(self, timeout=None):
        self._done.wait(timeout)
        return self.poll()
