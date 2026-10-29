"""The tuning loop: Fibonacci cadence, routing weights, escalation audit, shadow accuracy, batch size, cap.

Pure Python, no LLM. Automatic changes become one policy_versions row; everything the owner must approve
becomes a ledger question. Safety rules are never read or written here.
"""
import argparse
import json
import statistics
import sys
from datetime import datetime, timedelta, timezone

from . import contract, route
from .ledger import connect, ops
from .ledger.db import tx
from .ledger.db import HubRequired, default_path, main_error
from .tuner_routing import (PASS_OUTCOMES, _runs, _fit_rates, _cost, _is_worker, _by_task,
                            cost_per_landed, cost_per_landed_pass, route_stats, update_routes)
from .tuner_metrics import (PROMOTE_MIN, SPOT_LABEL_MIN, NO_RULE, BATCH_RANGE, BATCH_OFF_GATE_S,
                            GATE_MIN_N, CAP_RANGE, TUNER_EPIC, _policy, _dt, shadow_accuracy,
                            _tuner_epic, proposals, tune_batch, landed_per_hour, tune_cap)


def __getattr__(name):   # the old constants stay importable; they follow the active config
    if name == "INTERVALS_DAYS":
        return intervals_days()
    if name == "BATCH_DEFAULT":
        return _policy()["landing_batch_size"]
    if name == "CAP_DEFAULT":
        return _cap_default()
    raise AttributeError(name)


def _cap_default():
    from . import config
    return config.get()["capacity"]["parallel_cap"]


def intervals_days():
    """Days between passes by cadence step, then the last one forever (config policy.tuner_cadence_days)."""
    return tuple(_policy()["tuner_cadence_days"])


MIN_NEW_RUNS = 20
WORSE_PCT = 0.15
TAU_RANGE = (0.6, 0.95)
TAU_STEP = 0.02
TAU_QUESTIONS = ("retry_or_escalate", "plan_wrong")
MIN_AUDIT = 3
SMALL_DIFF_LINES = 20
TIER = {"haiku": 0, "luna": 0, "sonnet": 1, "sol": 1, "opus": 2, "fable": 3}
AUTO_KEYS = ("routes", "leaders", "tau", "batch_size", "parallel_cap")  # the only policy keys the tuner writes
STATE_KEY = "tuner_state"


# ---- state and cadence -----------------------------------------------------------------------------------


def new_state():
    return {"step": 0, "last_pass": None, "last_run_id": 0, "new_runs": 0, "last_cpl": None, "models_sig": None,
            "repos": [], "reset": False, "last_lph": None, "last_cap": None, "last_cap_dir": 0}


def load_state(conn):
    raw = ops.get_setting(conn, STATE_KEY)
    return {**new_state(), **(json.loads(raw) if raw else {})}


def save_state(conn, state):
    ops.set_setting(conn, STATE_KEY, json.dumps(state, sort_keys=True))


def interval_days(step):
    days = intervals_days()
    return days[min(step, len(days) - 1)]


def due(now, state):
    """Is a pass due? Fewer than MIN_NEW_RUNS new runs skips it (and the schedule does not advance)."""
    now = _dt(now)
    new = state.get("new_runs", 0)
    last = state.get("last_pass")
    next_due = None if last is None else _dt(last) + timedelta(days=interval_days(state.get("step", 0)))
    nd = next_due.strftime("%Y-%m-%dT%H:%M:%SZ") if next_due else None
    if new < MIN_NEW_RUNS:
        return {"due": False, "reason": f"skip: {new} new runs, need {MIN_NEW_RUNS}", "next_due": nd, "step": state["step"]}
    if next_due is not None and now < next_due:
        return {"due": False, "reason": "not yet", "next_due": nd, "step": state["step"]}
    return {"due": True, "reason": "due", "next_due": nd, "step": state["step"]}


def advance(state, adopted, reset):
    """Step the cadence after a pass: reset to daily, hold when a change was adopted, else advance one."""
    if reset:
        state["step"] = 0
    elif not adopted:
        state["step"] = min(state["step"] + 1, len(intervals_days()) - 1)
    return state


# ---- reading the ledger -----------------------------------------------------------------------------------

def _tier(model):
    m = (model or "").lower()
    return next((v for k, v in TIER.items() if k in m), 1)


# ---- (b) escalation audit ---------------------------------------------------------------------------------

def _diff_lines(r):
    return contract.receipt_diff_lines(r["gate_receipt"])


def _replan_tasks(conn):
    out = set()
    for e in ops.events(conn):
        if e["task_id"] and (e["kind"] == "replan-request" or (e["kind"] == "hold" and e["data"].get("reason") == "replan")):
            out.add(e["task_id"])
    return out


def audit(conn, runs, task_ids, small_lines=SMALL_DIFF_LINES):
    """Missed and needless escalations over the given tasks. Returns counts and the task ids behind them."""
    replans = _replan_tasks(conn)
    missed, needless, escalations = [], [], 0
    for tid, rs in _by_task(runs).items():
        if tid not in task_ids:
            continue
        w = [r for r in rs if _is_worker(r)]
        hold = any((r["review_verdict"] or "").upper() == "HOLD" for r in rs)
        signal = len(w) >= 3 or hold or tid in replans
        escalated = False
        for prev, cur in zip(w, w[1:]):
            if _tier(cur["model"]) > _tier(prev["model"]):
                escalated = True
                escalations += 1
                dl = _diff_lines(cur)
                if (dl is not None and dl <= small_lines) or cur["outcome"] == prev["outcome"]:
                    needless.append(tid)
        if signal and w and not escalated:
            missed.append(tid)
    return {"missed": sorted(missed), "needless": sorted(set(needless)), "escalations": escalations,
            "tasks": len([t for t in task_ids if t in {r["task_id"] for r in runs}]),
            "replan_missed": sorted(t for t in missed if t in replans)}


def _labelled(conn, question):
    miss = need = 0
    for r in conn.execute("SELECT outcome FROM decisions WHERE question_name=? AND outcome IS NOT NULL", (question,)):
        tag = contract.decision_audit(json.loads(r[0]))
        miss += tag == "missed"
        need += tag == "needless"
    return miss, need


def adjust_tau(tau, missed, needless, total_escal, total_tasks):
    """Missed escalations lower tau (escalate sooner); needless ones raise it. Clamped to TAU_RANGE."""
    if missed + needless < MIN_AUDIT:
        return tau
    mr = missed / max(1, total_tasks)
    nr = needless / max(1, total_escal)
    if mr > nr:
        tau -= TAU_STEP
    elif nr > mr:
        tau += TAU_STEP
    return round(min(TAU_RANGE[1], max(TAU_RANGE[0], tau)), 4)


# ---- the pass ---------------------------------------------------------------------------------------------

def current_policy(conn):
    v = ops.current_policy(conn)
    row = conn.execute("SELECT data FROM policy_versions WHERE version=?", (v,)).fetchone() if v else None
    p = json.loads(row[0]) if row else {}
    p.setdefault("routes", {})
    p.setdefault("tau", {})
    for q in TAU_QUESTIONS:
        p["tau"][q] = min(TAU_RANGE[1], max(TAU_RANGE[0], p["tau"].get(q, _policy()["tau"])))
    p.setdefault("batch_size", _policy()["landing_batch_size"])
    p.setdefault("parallel_cap", _cap_default())
    return p


def _new_version(conn):
    """Next pN; call inside the write transaction so MAX+1 cannot race."""
    nums = [int(v[1:]) for (v,) in conn.execute("SELECT version FROM policy_versions") if v[:1] == "p" and v[1:].isdigit()]
    return f"p{max(nums, default=0) + 1}"


def detect_triggers(conn, state, runs=None):
    """Reset reasons visible without running a pass: manual reset, model/price change, new repo."""
    runs = _runs(conn) if runs is None else runs
    why = ["manual reset"] if state.get("reset") else []
    sig = json.dumps([sorted({r["model"] or "" for r in runs}), ops.get_setting(conn, "prices", "")])
    if state["models_sig"] is not None and state["models_sig"] != sig:
        why.append("model or price change")
    progs = sorted({r[0] for r in conn.execute("SELECT DISTINCT repo FROM tasks")})
    if state["repos"] and set(progs) - set(state["repos"]):
        why.append("new repo")
    return why, sig, progs


def refresh(conn, state):
    runs = _runs(conn)
    state["new_runs"] = sum(1 for r in runs if r["id"] > state["last_run_id"])
    why, _, _ = detect_triggers(conn, state, runs)
    if why:
        state["step"] = 0
        state["triggers"] = why
    return runs


def run_pass(conn, now=None, force=False):
    now = _dt(now or datetime.now(timezone.utc))
    state = load_state(conn)
    runs = refresh(conn, state)
    verdict = due(now, state)
    if not verdict["due"] and not (force and state["new_runs"] >= MIN_NEW_RUNS):
        save_state(conn, state)
        return {"ran": False, **verdict}
    with tx(conn):
        result = _pass(conn, now, state, runs)
    if result.get("version"):
        from . import config, decisions
        cfg = config.get()
        policy = current_policy(conn)
        reason = "; ".join(result["changes"]) or "Routing statistics refreshed from recorded run outcomes."
        decisions.record_policy(config.state_dir(cfg), result["version"], policy, reason,
                                sync_settings=cfg["config_sync"])
    return result


def _pass(conn, now, state, runs):
    rates = _fit_rates(runs)
    new = [r for r in runs if r["id"] > state["last_run_id"]]
    policy = current_policy(conn)
    before = json.dumps({k: policy.get(k) for k in AUTO_KEYS}, sort_keys=True)

    stats = route_stats(runs, rates)
    routes, leaders = update_routes(policy, stats)
    material = [f"leader {k}: {policy.get('leaders', {}).get(k)} -> {m}" for k, m in leaders.items()
                if policy.get("leaders", {}).get(k) not in (None, m)]
    aud = audit(conn, runs, {r["task_id"] for r in new})
    tau, tau_new = policy["tau"], {}
    for q in TAU_QUESTIONS:
        lm, ln = _labelled(conn, q)
        m = len(aud["missed"]) + lm if q == "retry_or_escalate" else len(aud["replan_missed"]) + lm
        n = len(aud["needless"]) + ln if q == "retry_or_escalate" else ln
        tau_new[q] = adjust_tau(tau[q], m, n, aud["escalations"] + ln, aud["tasks"])
        if tau_new[q] != tau[q]:
            material.append(f"tau {q}: {tau[q]} -> {tau_new[q]}")
    acc = shadow_accuracy(conn)
    made = proposals(conn, acc, stats)
    batch = tune_batch(conn, policy["batch_size"])
    if batch != policy["batch_size"]:
        material.append(f"batch_size: {policy['batch_size']} -> {batch}")
    lph = landed_per_hour(new)
    cap, cap_dir = tune_cap(policy["parallel_cap"], lph, state)
    if cap != policy["parallel_cap"]:
        material.append(f"parallel_cap: {policy['parallel_cap']} -> {cap}")

    policy.update(routes=routes, leaders=leaders, tau={**tau, **tau_new}, batch_size=batch, parallel_cap=cap)
    version = None
    if json.dumps({k: policy.get(k) for k in AUTO_KEYS}, sort_keys=True) != before:
        version = _new_version(conn)
        ops.add_policy_version(conn, version, policy, note="; ".join(material) or "routing statistics refreshed", actor="tuner")

    cpl = cost_per_landed(new, rates)
    worse = state["last_cpl"] is not None and cpl is not None and cpl > state["last_cpl"] * (1 + WORSE_PCT)
    triggers, sig, progs = detect_triggers(conn, state, runs)
    triggers = list(state.pop("triggers", [])) + triggers
    if worse:
        triggers.append("cost per landed task worsened >15%")
    advance(state, adopted=bool(material), reset=bool(triggers))
    state.update(last_pass=now.strftime("%Y-%m-%dT%H:%M:%SZ"), last_run_id=max(r["id"] for r in runs), new_runs=0,
                 last_cpl=cpl if cpl is not None else state["last_cpl"], models_sig=sig, repos=progs, reset=False,
                 last_lph=lph if lph is not None else state["last_lph"], last_cap=cap, last_cap_dir=cap_dir)
    save_state(conn, state)
    return {"ran": True, "version": version, "changes": material, "audit": aud, "tau": tau_new, "batch_size": batch,
            "parallel_cap": cap, "cost_per_landed": cpl, "proposals": made, "resets": sorted(set(triggers)),
            "step": state["step"], "shadow": acc}


def status(conn, now=None):
    now = _dt(now or datetime.now(timezone.utc))
    state = load_state(conn)
    refresh(conn, state)
    return {"state": state, "due": due(now, state), "policy": ops.current_policy(conn)}


# ---- CLI --------------------------------------------------------------------------------------------------

def _fmt(res):
    if "ran" in res:
        if not res["ran"]:
            return f"no pass: {res['reason']} (next due {res['next_due']}, step {res['step']})"
        return "\n".join([f"pass done, policy {res['version'] or 'unchanged'}, cadence step {res['step']}"]
                         + [f"  change: {c}" for c in res["changes"]]
                         + [f"  resets: {', '.join(res['resets'])}"] * bool(res["resets"])
                         + [f"  proposal: question {q}" for q in res["proposals"]])
    d = res["due"]
    return f"policy {res['policy']}, step {res['state']['step']}, new runs {res['state']['new_runs']}: {d['reason']}"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="drover.tuner")
    ap.add_argument("--db")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--now", help="ISO time, for tests")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--force", action="store_true", help="ignore the schedule (never the 20-run floor)")
    sub.add_parser("due")
    sub.add_parser("status")
    sub.add_parser("reset", help="manual reset to daily")
    a = ap.parse_args(argv)
    try:
        conn = connect(a.db or default_path())
    except HubRequired as e:
        return main_error(str(e))
    try:
        return _dispatch(a, conn)
    finally:
        conn.close()


def _dispatch(a, conn):
    now = _dt(a.now) if a.now else None
    if a.cmd == "run":
        res = run_pass(conn, now, a.force)
    elif a.cmd == "reset":
        s = load_state(conn)
        s.update(reset=True, step=0)
        save_state(conn, s)
        res = status(conn, now)
    else:
        res = status(conn, now)
    if a.cmd == "due":
        print(json.dumps(res["due"]) if a.json else res["due"]["reason"])
        return 0 if res["due"]["due"] else 1
    print(json.dumps(res, indent=2, sort_keys=True, default=str) if a.json else _fmt(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
