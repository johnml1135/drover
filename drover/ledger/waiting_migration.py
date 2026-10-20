"""Atomic conversion of legacy Waiting state; extended by the migration steps."""
from .. import contract
from . import states


def migrate_waiting(conn) -> dict:
    """Refuse unconverted legacy state until its explicit converter is available.

    This initial converter is deliberately conservative. Migration steps T27–T29
    replace these refusals with an inventory and conversion of each family.
    """
    if conn.execute("SELECT 1 FROM tasks WHERE blocker IS NOT NULL OR status=? LIMIT 1",
                    (states.BLOCKED,)).fetchone():
        raise ValueError("legacy blockers require Waiting conversion")
    prefixes = ("_gate_question_", "gate_cmd_", "_origin_pause_", "_land_dirty_q_")
    exact = {"landing_hold", "landing_hold_question", "_dispatch_backoff"}
    for key, value in conn.execute("SELECT key,value FROM settings"):
        if value.strip() and (key in exact or key.startswith(prefixes)):
            raise ValueError(f"legacy setting {key!r} requires Waiting conversion")
    if conn.execute("SELECT 1 FROM base_gate_legacy LIMIT 1").fetchone():
        raise ValueError("legacy base cache requires Waiting conversion")
    if conn.execute("SELECT 1 FROM questions q JOIN tasks t ON q.target_kind='task' AND q.target_id=t.id "
                    "WHERE t.status=? LIMIT 1", (states.NEEDS_OWNER,)).fetchone():
        raise ValueError("legacy owner questions require Waiting conversion")
    if conn.execute("SELECT 1 FROM task_events WHERE kind IN ('replan-request','director-borrow') LIMIT 1").fetchone():
        raise ValueError("legacy request events require Waiting conversion")
    if conn.execute("SELECT 1 FROM runs r JOIN tasks t ON t.id=r.task_id WHERE t.status=? "
                    "AND r.role='review' AND r.outcome=? AND r.ended IS NOT NULL LIMIT 1",
                    (states.GATED, contract.PROVIDER_ERROR)).fetchone():
        raise ValueError("legacy review deadlines require Waiting conversion")
    return {"converted": {name: 0 for name in ("holds", "causes", "questions", "requests", "deadlines", "cache")},
            "retired": [], "diagnostics": []}
