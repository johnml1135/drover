"""LedgerPort: the operations the dispatcher needs from the ledger, behind one seam.

DirectPort calls drover.ledger on a connection (tests, local use). HttpPort is the WSL-side client of the Windows
hub API (ADR 0001). Since ADR 0002 the dispatcher runs inside the hub and uses DirectPort.
"""
import json
import time
from typing import Protocol

from . import config
from .ledger import ops


class LedgerPort(Protocol):
    waiting: "Waiting"
    def claim_next(self, worker=None, exclude=()): ...
    def get_task(self, task_id): ...
    def list_tasks(self, status=None): ...
    def dependents(self, task_id): ...
    def list_runs(self, task_id): ...
    def start_run(self, task_id, role, backend, model=None, effort=None): ...
    def finish_run(self, run_id, outcome, **fields): ...
    def set_status(self, task_id, new, reason=None, data=None): ...
    def set_handoff(self, task_id, text): ...
    def append_event(self, kind, data=None, task_id=None, actor="dispatcher"): ...
    def events(self, kind=None, after=0): ...
    def heartbeat(self, data=None): ...
    def owner_notes(self, task_id): ...
    def record_decision(self, **fields): ...
    def get_policy(self): ...
    def note_wsl_needed(self, task_id): ...
    def get_setting(self, key, default=None): ...
    def set_setting(self, key, value): ...
    def record_outcome(self, decision_id, outcome): ...
    def amend_run(self, run_id, outcome=None, gate_receipt=None, note=None): ...
    def update_task(self, task_id, **fields): ...
    def ask_question(self, target_kind, target_id, title, body="", options=(), needs_owner=False): ...
    def get_question(self, qid): ...


class DirectPort:
    def __init__(self, conn, actor="dispatcher", now=time.time):
        from .ledger.waiting import Waiting
        self.conn, self.actor, self.now = conn, actor, now
        self.waiting = Waiting(conn, actor)

    def claim_next(self, worker=None, exclude=()):
        return ops.claim_next(self.conn, {"actor": self.actor, **(worker or {})}, exclude=exclude)

    def get_task(self, task_id):
        return ops.get_task(self.conn, task_id)

    def list_tasks(self, status=None):
        return ops.list_tasks(self.conn, status=status)

    def dependents(self, task_id):
        return ops.dependents_of(self.conn, task_id)

    def list_runs(self, task_id):
        return ops.list_runs(self.conn, task_id)

    def start_run(self, task_id, role, backend, model=None, effort=None):
        return ops.start_run(self.conn, task_id, role, backend, model, effort, actor=self.actor)

    def set_run_context(self, run_id, **fields):
        from . import stats
        return stats.update_run(self.conn, run_id, **fields)

    def finish_run(self, run_id, outcome, **fields):
        return ops.finish_run(self.conn, run_id, outcome, actor=self.actor, **fields)

    def set_status(self, task_id, new, reason=None, data=None):
        return ops.set_status(self.conn, task_id, new, self.actor, reason=reason, data=data)

    def set_handoff(self, task_id, text):
        return ops.update_task(self.conn, task_id, self.actor, handoff=text)


    def append_event(self, kind, data=None, task_id=None, actor=None):
        with ops.tx(self.conn):
            return ops.event(self.conn, kind, actor or self.actor, data, task_id=task_id)

    def events(self, kind=None, after=0):
        return [e for e in ops.events(self.conn, kind=kind) if e["id"] > after]

    def owner_notes(self, task_id):
        return [c["body"] for c in ops.list_comments(self.conn, "task", task_id) if c["author"] == "owner"]

    def heartbeat(self, data=None):
        ops.set_setting(self.conn, "dispatcher_heartbeat", json.dumps({"at": ops.stamp(), **(data or {})}))

    def record_decision(self, **fields):
        return ops.record_decision(self.conn, **fields)


    def note_wsl_needed(self, task_id):
        """Tell the hub watchdog a WSL-placed task is due and WSL is not up (it boots WSL, nothing else does)."""
        ops.set_setting(self.conn, "_wsl_needed", json.dumps({"ts": self.now(), "task": task_id}))

    def get_setting(self, key, default=None):
        return ops.get_setting(self.conn, key, default)

    def set_setting(self, key, value):
        ops.set_setting(self.conn, key, value)

    def record_outcome(self, decision_id, outcome):
        ops.record_outcome(self.conn, decision_id, outcome)

    def amend_run(self, run_id, outcome=None, gate_receipt=None, note=None):
        return ops.amend_run(self.conn, run_id, outcome=outcome, gate_receipt=gate_receipt, note=note)

    def update_task(self, task_id, **fields):
        return ops.update_task(self.conn, task_id, self.actor, **fields)

    def ask_question(self, target_kind, target_id, title, body="", options=(), needs_owner=False):
        return ops.ask_question(self.conn, target_kind, target_id, title, body, options, actor=self.actor,
                                needs_owner=needs_owner)

    def get_question(self, qid):
        return ops.get_question(self.conn, qid)

    def get_policy(self):
        """{'version', 'data', 'parallel_cap'}; the cap comes from policy data, then the ledger setting, then config capacity.parallel_cap (4)."""
        version = ops.current_policy(self.conn)
        row = self.conn.execute("SELECT data FROM policy_versions WHERE version=?", (version,)).fetchone()
        data = json.loads(row[0]) if row else {}
        cap = data.get("parallel_cap", ops.get_setting(self.conn, "parallel_cap", config.get()["capacity"]["parallel_cap"]))
        return {"version": version, "data": data, "parallel_cap": int(cap)}


class HttpPort:
    """Stub: the hub API client arrives with the hub slice."""

    def __init__(self, base_url=None, token=None):
        self.base_url, self.token = base_url, token

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def unwired(*a, **kw):
            raise NotImplementedError(f"HttpPort.{name}: the hub API is not wired yet")
        return unwired
