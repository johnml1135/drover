"""Shared runner paths, file operations and constants."""
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from .placement import IS_WINDOWS

POLL_S = 0.2
WATCH_GLOBS = ("*.step*.log",)       # additional output files watched by existing worker runs
STOP_FILE, STATUS_FILE, RECORD_FILE, OUT_FILE, RUNNER_LOG = "stop-request", "status.json", "record.json", \
    "out.log", "runner.log"
STOP_REASONS = ("rotation", "requested", "compaction")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
NONCE_RE = re.compile(r"^[A-Za-z0-9]{16,64}$")
DETACH_WAIT_S = 5


def scratch_path(value):
    root, path = Path("/tmp/drover-scratch"), Path(value)
    parent = path.parent
    direct = parent == root
    task_scratch = Path(os.environ.get("TMPDIR", ""))
    nested = (not direct and parent == task_scratch and parent.parent == root and parent.is_dir()
              and not parent.is_symlink() and parent.resolve() == parent)
    if (IS_WINDOWS or (not direct and not nested) or root.resolve() != root
            or path.is_symlink() or path.resolve().parent != parent):
        raise ValueError("scratch must be under /tmp/drover-scratch/ or its assigned TMPDIR")
    return path


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_dir(runs_dir, run_id):
    if not RUN_ID_RE.fullmatch(str(run_id)) or str(run_id) in (".", ".."):
        raise ValueError(f"bad run id {run_id!r}")
    return Path(runs_dir) / str(run_id)


# ---- files ------------------------------------------------------------------------------------------------
REPLACE_WAIT_S = 2.0       # an ordinary write outlasts a reader this long
TERMINAL_WAIT_S = 30.0     # the write that records a run's outcome outlasts a reader much longer
REPLACE_STEP_S = 0.05


def write_json_atomic(path, obj, wait_s=REPLACE_WAIT_S):
    """Write obj to path through a temp file. A PermissionError on the replace is retried for up to wait_s seconds:
    the status files live on /mnt/c, where a Windows reader (the hub, Defender) can hold the target open for a moment.
    When the wait runs out the temp file is removed and the last error is raised."""
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(obj, indent=1), encoding="utf-8")
        deadline = None          # the clock starts at the first refusal: a write that lands costs no clock read
        while True:
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                now = time.monotonic()
                deadline = now + wait_s if deadline is None else deadline
                if now >= deadline:
                    raise
                time.sleep(REPLACE_STEP_S)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def read_status(runs_dir, run_id):
    try:
        return json.loads((run_dir(runs_dir, run_id) / STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
