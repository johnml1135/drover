"""Shadow accuracy, proposals, batch size and capacity tuning."""
import json
import statistics
from datetime import datetime, timezone

from . import contract, route
from .ledger import ops


PROMOTE_MIN = 100
SPOT_LABEL_MIN = 30
NO_RULE = ("note_admit", "harness_pick")
BATCH_RANGE = (1, 5)
BATCH_OFF_GATE_S = 600
GATE_MIN_N = 5
CAP_RANGE = (1, 8)
TUNER_EPIC = "tuner_epic"


def _policy():
    from . import config
    return config.get()["policy"]


def _dt(v):
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v, timezone.utc)
    return datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def shadow_accuracy(conn):
    """Per question (latest criteria version first seen with outcomes): n, model accuracy, rule accuracy."""
    out = {}
    rows = conn.execute("SELECT * FROM decisions WHERE shadow=1 AND outcome IS NOT NULL ORDER BY id")
    for r in rows:
        ans, rule, o = json.loads(r["answer"]), (json.loads(r["rule_answer"]) if r["rule_answer"] else None), json.loads(r["outcome"])
        model_ok, rule_ok = contract.decision_hit(ans, o), contract.decision_rule_hit(rule, o)
        if model_ok is None:
            continue
        q = out.setdefault(r["question_name"], {"version": r["criteria_version"], "n": 0, "m": 0, "rn": 0, "rm": 0})
        if q["version"] != r["criteria_version"]:
            q.update(version=r["criteria_version"], n=0, m=0, rn=0, rm=0)
        q["n"] += 1
        q["m"] += bool(model_ok)
        if rule_ok is not None:
            q["rn"] += 1
            q["rm"] += bool(rule_ok)
    return {name: {"version": q["version"], "n": q["n"], "model_acc": q["m"] / q["n"],
                   "rule_n": q["rn"], "rule_acc": (q["rm"] / q["rn"]) if q["rn"] else None}
            for name, q in out.items()}


def _tuner_epic(conn):
    eid = ops.get_setting(conn, TUNER_EPIC)
    if eid:
        return eid
    eid = ops.create_epic(conn, "drover", "Tuner proposals", "Owner-approval proposals from the tuning loop.", actor="tuner")["id"]
    ops.set_setting(conn, TUNER_EPIC, eid)
    return eid


def proposals(conn, acc, stats):
    from .ledger.waiting import Waiting, cause_key
    waiting = Waiting(conn, "tuner")
    made = []

    def offer(identity, title, body, options):
        cause = cause_key("tuner", *identity)
        if waiting.state(cause) is not None:
            return None
        return waiting.ask(cause, "epic", _tuner_epic(conn), title, body,
                           [{"label": label, "description": description, "recommended": recommended}
                            for label, description, recommended in options])
    for name, a in sorted(acc.items()):
        tag = f"{name} (criteria {a['version']})"
        if name in NO_RULE:
            if a["n"] >= SPOT_LABEL_MIN:
                how = "spot-label a 30-item sample (need >=85% agreement)" if name == "note_admit" else "A/B on cost per landed task"
                made.append(offer(('validate', name, a['version']), f"Validate {tag}: no rule baseline", f"{a['n']} shadow decisions. Needs: {how}.",
                                     [("Start validation", how, True), ("Not now", "keep shadow mode", False)]))
        elif a["n"] >= PROMOTE_MIN and a["rule_acc"] is not None and a["model_acc"] > a["rule_acc"]:
            made.append(offer(('promote', name, a['version']), f"Promote {tag} from shadow to live?",
                                 f"{a['n']} decisions: model {a['model_acc']:.0%} vs rules {a['rule_acc']:.0%}.",
                                 [("Promote to live", "the decider acts above tau", True), ("Keep shadow", "gather more", False)]))
        if a["n"] >= 20 and a["model_acc"] < 0.7:
            made.append(offer(('criteria', name, a['version']), f"Review criteria text for {tag}?",
                                 f"Accuracy {a['model_acc']:.0%} over {a['n']} decisions. Rewriting needs an LLM; the tuner does not call one.",
                                 [("Run a Sonnet criteria review", "fresh Sonnet session reads misroute clusters", True),
                                  ("Leave as is", "", False)]))
    for key, per in sorted(stats.items()):
        for (backend, model), s in sorted(per.items()):
            if s["first_pass_n"] >= 10 and s["first_pass_ok"] / s["first_pass_n"] < 0.2:
                made.append(offer(('pin', key, backend, model), f"Pin a rule: stop routing {key} to {model}?",
                                     f"First-pass gate rate {s['first_pass_ok']}/{s['first_pass_n']}.",
                                     [("Pin the rule", f"never route {key} to {model}", True), ("Keep routing", "", False)]))
            elif s["first_pass_n"] >= 10 and s["first_pass_ok"] / s["first_pass_n"] < route.MIN_FIRST_PASS:
                made.append(offer(('brief', key, backend, model), f"Review the brief template for {key} on {model}?",
                                     f"First-pass gate rate {s['first_pass_ok']}/{s['first_pass_n']}, below {route.MIN_FIRST_PASS:.0%}.",
                                     [("Run a Sonnet brief review", "propose a template diff, replayed on 5 tasks", True),
                                      ("Leave as is", "", False)]))
    return [q for q in made if q]


def tune_batch(conn, current):
    gates = [contract.parse_gate_event(e["data"]) for e in ops.events(conn, kind=contract.GATE_EVENT)][-50:]
    gates = [(d, red) for d, red in gates if d is not None]
    if len(gates) < GATE_MIN_N:
        return current
    median = statistics.median(d for d, _ in gates)
    if median <= BATCH_OFF_GATE_S:
        return BATCH_RANGE[0]
    red = sum(1 for _, r in gates if r) / len(gates)
    size = _policy()["landing_batch_size"] if current <= BATCH_RANGE[0] else current
    if red > 0.3:
        size -= 1
    elif red < 0.1 and median > 3 * BATCH_OFF_GATE_S:
        size += 1
    return min(BATCH_RANGE[1], max(BATCH_RANGE[0], size))


def landed_per_hour(new_runs):
    if not new_runs:
        return None
    start = min(_dt(r["started"]) for r in new_runs)
    end = max(_dt(r["ended"]) for r in new_runs)
    hours = max(1.0, (end - start).total_seconds() / 3600)
    return len({r["task_id"] for r in new_runs if r["task_status"] == "landed"}) / hours


def tune_cap(cap, lph, state):
    """Hill-climb by one: probe +1 from a cold start, keep moving while landed-per-hour improves, reverse on a drop."""
    prev, last_dir, last_cap = state.get("last_lph"), state.get("last_cap_dir", 0), state.get("last_cap")
    if lph is None or prev is None or (last_cap is not None and last_cap != cap):
        return cap, 0
    if last_dir == 0:
        d = 1
    elif lph > prev * 1.05:
        d = last_dir
    elif lph < prev * 0.95:
        d = -last_dir
    else:
        d = 0
    return min(CAP_RANGE[1], max(CAP_RANGE[0], cap + d)), d
