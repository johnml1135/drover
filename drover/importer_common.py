"""Shared importer records and values."""
import hashlib
import json

from .ledger import ops

ACTOR = "importer"
PRIORITIES = ("P0", "P1", "P2", "P3", "P4")
SIZES = ("S", "M", "L")
IMPORT_EPIC = "Import"
APPROVE_OPTIONS = (("Approve", "Approve this batch as a deletion/landing proposal; the garden step still runs it later."),
                   ("Reject", "Keep every branch in this batch."),
                   ("Review individually", "Break the batch up and decide branch by branch."))


class ImportFailed(ValueError):
    pass


def sha(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:24]


def _s(v, default=""):
    return default if v is None else str(v)


def _list(v):
    return [str(x) for x in v] if isinstance(v, (list, tuple)) else ([] if v in (None, "") else [str(v)])


def _dicts(v):
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


# ---- keyed records -------------------------------------------------------------------------------------

def _index(conn, kind):
    """{source_hash: latest event} for one import event kind."""
    return {e["data"]["source_hash"]: e for e in ops.events(conn, kind=kind) if "source_hash" in e["data"]}


def _record(conn, idx, kind, key, payload, task_id=None, epic_id=None):
    """Append an import event unless the latest one for `key` already holds this payload."""
    old = idx.get(key)
    if old and {k: v for k, v in old["data"].items() if k != "source_hash"} == payload:
        return "unchanged"
    ops.event(conn, kind, ACTOR, {"source_hash": key, **payload}, task_id=task_id, epic_id=epic_id)
    return "updated" if old else "created"


class Tally:
    def __init__(self):
        self.counts, self.warnings = {}, []

    def add(self, kind, action):
        self.counts.setdefault(kind, {})
        self.counts[kind][action] = self.counts[kind].get(action, 0) + 1

    def warn(self, msg):
        self.warnings.append(msg)
