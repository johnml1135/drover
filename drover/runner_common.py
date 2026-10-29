"""Shared runner paths, file operations and constants."""
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from .placement import IS_WINDOWS

POLL_S = 0.2
WATCH_GLOBS = ("*.step*.log",)       # additional output files watched by existing worker runs
STOP_FILE, STATUS_FILE, RECORD_FILE, OUT_FILE, RUNNER_LOG = "stop-request", "status.json", "record.json", \
    "out.log", "runner.log"
STOP_REASONS = ("rotation", "requested")
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
def write_json_atomic(path, obj):
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, indent=1), encoding="utf-8")
    for i in range(20):           # Windows refuses the replace while a reader has the target open
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == 19:
                raise
            time.sleep(0.05)


def read_status(runs_dir, run_id):
    try:
        return json.loads((run_dir(runs_dir, run_id) / STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
