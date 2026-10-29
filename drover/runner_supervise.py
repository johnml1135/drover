"""Worker supervision and stop requests."""
import json
import os
import re
import shutil
import signal
import subprocess
import time
import uuid
from pathlib import Path
from . import placement
from .placement import KILL_AFTER_S
from .runner_common import (POLL_S, WATCH_GLOBS, STOP_FILE, STATUS_FILE, RECORD_FILE, OUT_FILE,
                            STOP_REASONS, NONCE_RE, scratch_path, now_iso, run_dir, write_json_atomic, read_status)
from .runner_record import scan_record

# ---- supervising ------------------------------------------------------------------------------------------
class Status:
    def __init__(self, path, run_id, nonce):
        self.path = path
        self.d = {"run_id": run_id, "state": "starting", "pid": None, "runner_pid": os.getpid(),
                  "started": now_iso(), "ended": None, "exit_code": None, "reason": None, "nonce": nonce,
                  "detached_in_job": os.environ.get("DROVER_DETACHED_IN_JOB") == "1"}

    def set(self, **kw):
        self.d.update(kw)
        write_json_atomic(self.path, self.d)


def _read_stop_file(d):
    try:
        obj = json.loads((d / STOP_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def read_stop(d, nonce):
    """The stop reason when a stop-request carrying this launch's nonce exists, else None."""
    obj = _read_stop_file(d)
    if not obj or obj.get("nonce") != nonce:
        return None
    return obj.get("reason") if obj.get("reason") in STOP_REASONS else "requested"


def request_stop(runs_dir, run_id, reason="requested", nonce=None):
    """Write the stop-request for the launch recorded in status.json. False when there is no launch to stop.

    A caller that chose the launch's nonce (--nonce) passes it: the request can then be written before status.json
    exists, and a runner that starts late honours it."""
    if nonce:
        if not NONCE_RE.fullmatch(str(nonce)):
            raise ValueError("bad nonce")
        d = run_dir(runs_dir, run_id)
        d.mkdir(parents=True, exist_ok=True)
        write_json_atomic(d / STOP_FILE, {"reason": reason, "nonce": str(nonce)})
        return True
    st = read_status(runs_dir, run_id)
    if not st or not st.get("nonce"):
        return False
    write_json_atomic(run_dir(runs_dir, run_id) / STOP_FILE, {"reason": reason, "nonce": st["nonce"]})
    return True


def _cleanup_scratch(path):
    try:
        shutil.rmtree(scratch_path(path))
    except (OSError, ValueError):
        pass


def _watched_sizes(d, globs):
    """((name, size), ...) of the run directory files matching `globs`, sorted by name."""
    seen = []
    for g in globs:
        for f in d.glob(g):
            try:
                seen.append((f.name, f.stat().st_size))
            except OSError:
                pass
    return tuple(sorted(seen))


def supervise(run_id, runs_dir, argv, wall_s=0, idle_s=0, stdin_file=None, cwd=None, kill_after=KILL_AFTER_S,
              poll_s=POLL_S, nonce=None, scratch_dir=None, watch_globs=WATCH_GLOBS, run_config=None):
    """Run the worker to its end. Returns the final status dict. Growth of out.log or of any file in the run
    directory matching `watch_globs` (a gate's per-step logs) counts as activity for the idle watchdog."""
    d = run_dir(runs_dir, run_id)
    d.mkdir(parents=True, exist_ok=True)
    out = d / OUT_FILE
    nonce = nonce or os.environ.get("DROVER_RUN_NONCE") or uuid.uuid4().hex
    st = Status(d / STATUS_FILE, run_id, nonce)
    st.set()
    stale = _read_stop_file(d)
    if stale is not None and stale.get("nonce") != nonce:       # left by an earlier launch of this run id
        try:
            (d / STOP_FILE).unlink()
        except OSError:
            pass
    sig_reason, old_handlers = [], {}
    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        if hasattr(signal, name):
            sig = getattr(signal, name)
            old_handlers[sig] = signal.signal(sig, lambda *_: sig_reason.append("signal"))
    scratch = None
    try:
        if scratch_dir:
            try:
                path = scratch_path(scratch_dir)
                path.parent.mkdir(mode=0o700, exist_ok=True)
                path.mkdir(mode=0o700)
                scratch = path
                path.chmod(0o700)
            except (OSError, ValueError) as e:
                st.set(state="exited", ended=now_iso(), exit_code=None, reason=f"spawn-failed: {e}",
                       missing_executable=False)
                return st.d
        with placement.cleanup_process_record(Path(runs_dir).parent):
            return _supervise(st, d, out, argv, wall_s, idle_s, stdin_file, cwd, kill_after, poll_s, nonce, sig_reason,
                              scratch, watch_globs, run_config)
    finally:
        if scratch is not None:
            _cleanup_scratch(scratch)
        for sig, h in old_handlers.items():
            signal.signal(sig, h)


def prepare_writable_roots(roots):
    """Pre-create explicitly configured sandbox roots under /tmp, refusing traversal and symlinks."""
    for value in placement.sandbox_roots(roots):
        path = Path(value)
        if not path.is_absolute() or not path.is_relative_to('/tmp') or path == Path('/tmp') \
                or '..' in path.parts or path.resolve() != path:
            raise ValueError(f"sandbox writable root must be under /tmp without symlinks: {value}")
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o777)


def _supervise(st, d, out, argv, wall_s, idle_s, stdin_file, cwd, kill_after, poll_s, nonce, sig_reason, scratch=None,
               watch_globs=WATCH_GLOBS, run_config=None):
    env = {k: v for k, v in os.environ.items() if k not in ("DROVER_RUN_NONCE", "DROVER_DETACHED_IN_JOB")}
    env["DROVER_RUN_ID"] = st.d["run_id"]
    run_config = run_config or {}
    try:
        prepare_writable_roots(run_config.get("sandbox_writable_roots", []))
    except (OSError, ValueError) as e:
        st.set(state="exited", ended=now_iso(), exit_code=None, reason=f"spawn-failed: {e}",
               missing_executable=False)
        return st.d
    if scratch is not None:
        env.update({key: str(scratch) for key in ("TMPDIR", "TMP", "TEMP")})
    env.update(run_config.get("env", {}))
    for key in ("CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        if env.get(key):
            env[key] = os.path.expanduser(env[key])
    stdin_f = subprocess.DEVNULL
    try:
        if stdin_file:
            stdin_f = open(stdin_file, "rb")
        with open(out, "ab") as out_f:
            proc, tree = placement.spawn_worker(argv, out_f, stdin_f, cwd, env)
    except OSError as e:
        # ENOENT can name the cwd or stdin file rather than the executable. Windows may omit the filename.
        missing = isinstance(e, FileNotFoundError) and bool(argv) and (
            e.filename == argv[0] or e.filename is None and (cwd is None or Path(cwd).is_dir()))
        st.set(state="exited", ended=now_iso(), exit_code=None, reason=f"spawn-failed: {e}",
               missing_executable=missing)
        return st.d
    finally:
        if stdin_file and stdin_f is not subprocess.DEVNULL:
            stdin_f.close()
    from .metrics import Peaks
    peaks = Peaks(tree.metrics)
    watch = None
    try:
        with placement.cleanup_process_record(d.parent.parent, cwd, pid=proc.pid):
            st.set(state="running", pid=proc.pid)
            watch = open(out, "rb")        # an open handle's size is exact on Windows, where stat() can lag
            t0 = last_change = time.monotonic()
            size, outcome, watched = 0, None, ()
            while proc.poll() is None:
                now = time.monotonic()
                if peaks.last is None or now - peaks.last[0] >= 1:
                    st.set(peaks=dict(peaks.sample()), heartbeat=now_iso())
                watch.seek(0, 2)
                if watch.tell() != size:
                    size, last_change = watch.tell(), now
                seen = _watched_sizes(d, watch_globs)
                if seen != watched:
                    watched, last_change = seen, now
                stop = read_stop(d, nonce) or (sig_reason[0] if sig_reason else None)
                if stop:
                    outcome = ("killed", stop)
                elif wall_s and now - t0 > wall_s:
                    outcome = ("timeout", f"wall-clock limit {wall_s:g}s")
                elif idle_s and now - last_change > idle_s:
                    steps = [m.group(1) for name, _ in watched for m in [re.search(r"\.(step\d+)\.log$", name)] if m]
                    step = max(steps, key=lambda n: int(n[4:]), default=None)
                    outcome = ("timeout", f"no output for {idle_s:g}s" + (f" (stalled in {step})" if step else ""))
                if outcome:
                    tree.kill(kill_after)
                    break
                time.sleep(poll_s)
            else:
                tree.reap_stragglers()
            rc = proc.wait()
            state, reason = outcome or ("exited", None)
            rec = scan_record(out)
            if rec:
                write_json_atomic(d / RECORD_FILE, rec)
            st.set(state=state, reason=reason, exit_code=rc, ended=now_iso(), peaks=dict(peaks.sample()))
    except BaseException:
        try:
            tree.kill(min(kill_after, 3))       # never leave the worker running unsupervised
        finally:
            raise
    finally:
        if watch:
            watch.close()
        tree.close()
    return st.d
