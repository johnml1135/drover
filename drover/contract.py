"""The data contract between the dispatcher (writer) and the tuner (reader).

One place for the run outcome vocabulary and the shapes stored in runs.allowance_used, runs.gate_receipt,
task_events kind 'gate' and decisions.outcome. Parsers accept the legacy shapes older rows may hold.
"""
import json

CI_PASSED, CI_FAILED, CI_REFUSED = "passed", "failed", "refused"
REPLAN_QUEUED, REPLAN_RUNNING, REPLAN_OWNER, REPLAN_DONE = "queued", "running", "owner", "done"

GATED = 'gated'
GATE_FAIL = 'gate-fail'
NO_RECORD = 'no-record'
ERROR = 'error'
LAUNCH_ERROR = 'launch-error'
PROVIDER_ERROR = 'provider-error'
TIMEOUT = 'timeout'
ROTATED = 'rotated'
ORPHANED = 'orphaned'
UNVERIFIED = 'unverified'
BLOCKED = 'blocked'
DONE = 'done'
PASSED = 'passed'
FAILED = 'failed'
LAND = 'land'
HOLD = 'hold'
READ_ONLY_DONE = 'read-only-done'

OUTCOMES = (GATED, GATE_FAIL, NO_RECORD, ERROR, LAUNCH_ERROR, PROVIDER_ERROR, TIMEOUT, ROTATED, ORPHANED,
            UNVERIFIED, BLOCKED, DONE, PASSED, FAILED, LAND, HOLD, READ_ONLY_DONE)
# `done` is a worker run that finished and awaits its gate (amended to gated or gate-fail). Gate runs end `passed` or
# `failed`, review runs `land` or `hold`: none of these is a worker failure, so FAIL_OUTCOMES leaves them out.
# The dispatcher counts a `hold` against the worker run it reviewed.
PIPELINE_OUTCOMES = (DONE, PASSED, FAILED, LAND, HOLD)   # written only when a pipeline gates and reviews
PASS_OUTCOMES = (GATED,)                                     # a worker run that passed its gate
FAIL_OUTCOMES = (GATE_FAIL, NO_RECORD, ERROR, TIMEOUT)  # worker failures; launch-error/provider-error/orphaned never count
GATE_EVENT = "gate"
EXCERPT_LINES = 60


# ---- runs.allowance_used -------------------------------------------------------------------------------
def allowance_used(seven_day_pct=None, five_hour_pct=None):
    """{seven_day_pct, five_hour_pct}: allowance-% one run consumed per window. None when nothing was measured."""
    out = {k: round(float(v), 4) for k, v in (("seven_day_pct", seven_day_pct), ("five_hour_pct", five_hour_pct))
           if isinstance(v, (int, float)) and not isinstance(v, bool)}
    return out or None


def used_from_headroom(h):
    """{seven_day, five_hour} used-% from an allowance.headroom() dict (or a flat {remaining_pct} one)."""
    if not isinstance(h, dict):
        return {}
    if isinstance(h.get("windows"), dict):
        return {n: 100.0 - w["remaining_pct"] for n, w in h["windows"].items()
                if isinstance(w, dict) and isinstance(w.get("remaining_pct"), (int, float))}
    if isinstance(h.get("remaining_pct"), (int, float)):
        return {"seven_day": 100.0 - h["remaining_pct"]}
    return {}


def used_delta(before, after):
    """allowance_used from two used-% readings taken at run start and end; None when either is unknown."""
    if not before or not after:
        return None
    d = {k: max(0.0, after[k] - before[k]) for k in ("seven_day", "five_hour") if k in before and k in after}
    return allowance_used(d.get("seven_day"), d.get("five_hour"))


def parse_allowance_used(v):
    """(seven_day_pct, five_hour_pct) from a stored value: the contract dict, or legacy numbers and dicts."""
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return None, None
    if isinstance(v, bool):
        return None, None
    if isinstance(v, (int, float)):
        return float(v), None

    def num(*keys):
        return next((float(v[k]) for k in keys if isinstance(v.get(k), (int, float)) and not isinstance(v.get(k), bool)),
                    None)
    if isinstance(v, dict):
        seven = num("seven_day_pct", "seven_day", "pct")
        five = num("five_hour_pct", "five_hour")
        if seven is None and five is None:       # older free-form dict: the sum of its numbers
            nums = [x for x in v.values() if isinstance(x, (int, float)) and not isinstance(x, bool)]
            seven = float(sum(nums)) if nums else None
        return seven, five
    return None, None


def cost_pct(v):
    """The weekly allowance-% a run cost, the figure routing prices in; None when unmeasured."""
    return parse_allowance_used(v)[0]


def format_allowance(v):
    seven, five = parse_allowance_used(v)
    parts = [f"{seven:g}% week" if seven is not None else "", f"{five:g}% 5h" if five is not None else ""]
    return ", ".join(p for p in parts if p)


# ---- runs.gate_receipt ---------------------------------------------------------------------------------
def gate_receipt(ok, diff_lines=None, excerpt=None, receipt=None, **extra):
    """The JSON text stored in runs.gate_receipt. diff_lines is the size of the change the gate judged."""
    d = {"ok": bool(ok), "diff_lines": diff_lines if isinstance(diff_lines, (int, float)) else None}
    if excerpt:
        d["excerpt"] = "\n".join(str(excerpt).splitlines()[-EXCERPT_LINES:])
    if receipt is not None:
        d["receipt"] = receipt
    d.update(extra)
    return json.dumps(d, default=str)


def parse_gate_receipt(text):
    """The receipt dict, or None when the column holds plain text (a legacy excerpt) or nothing."""
    try:
        d = json.loads(text or "")
    except (TypeError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def receipt_diff_lines(text):
    g = parse_gate_receipt(text) or {}
    for k in ("diff_lines", "lines_changed"):
        if isinstance(g.get(k), (int, float)) and not isinstance(g.get(k), bool):
            return g[k]
    return None


def receipt_excerpt(text):
    """The failure excerpt to put in a retry brief: from the JSON receipt, else the legacy plain text."""
    if not text:
        return None
    g = parse_gate_receipt(text)
    return (g.get("excerpt") or None) if g is not None else text


# ---- task_events kind 'gate' ---------------------------------------------------------------------------
def gate_event(duration_s, red, run=None):
    return {"duration_s": round(float(duration_s), 3), "red": bool(red), "run": run}


def parse_gate_event(data):
    """(duration_s or None, red) from an event's data."""
    data = data if isinstance(data, dict) else {}
    d = data.get("duration_s")
    d = float(d) if isinstance(d, (int, float)) and not isinstance(d, bool) else None
    return d, bool(data.get("red") or data.get("result") == "red")


# ---- decisions.outcome ---------------------------------------------------------------------------------
# An outcome is one of: a bare value (the true answer); {"label": truth}; {"correct": bool, "rule_correct": bool};
# {"audit": "missed" | "needless"} (the tuner's escalation audit tag). Anything else is unscored.
def decision_outcome(label=None, correct=None, rule_correct=None, audit=None):
    d = {k: v for k, v in (("label", label), ("correct", correct), ("rule_correct", rule_correct),
                           ("audit", audit)) if v is not None}
    return d or None


def _truth_hit(answer, outcome):
    if isinstance(outcome, dict):
        return answer == outcome["label"] if "label" in outcome else None
    return answer == outcome


def decision_hit(answer, outcome):
    """Did the model's answer match the outcome: True, False, or None when the outcome does not say."""
    if isinstance(outcome, dict) and "correct" in outcome:
        return outcome["correct"]
    return _truth_hit(answer, outcome)


def decision_rule_hit(rule_answer, outcome):
    if isinstance(outcome, dict) and "rule_correct" in outcome:
        return outcome["rule_correct"]
    return None if rule_answer is None else _truth_hit(rule_answer, outcome)


def decision_audit(outcome):
    tag = outcome.get("audit") if isinstance(outcome, dict) else None
    return tag if tag in ("missed", "needless") else None


# ---- the reviewer's record -----------------------------------------------------------------------------
def parse_review_record(text):
    """{'verdict', 'findings': [str], 'note', 'sha'} from the last JSON object in `text` that has a `verdict`, else None.
    The runner's own record parser drops verdict and findings, so a review is read from the worker's final message."""
    text = (text or "")[-16000:]
    dec, end = json.JSONDecoder(), len(text)
    for _ in range(20):
        pos = text.rfind('"verdict"', 0, end)
        if pos < 0:
            return None
        end, start = pos, pos
        for _ in range(4):
            start = text.rfind("{", 0, start)
            if start < 0:
                break
            try:
                obj, _n = dec.raw_decode(text[start:])
            except ValueError:
                continue
            if isinstance(obj, dict) and isinstance(obj.get("verdict"), str):
                f = obj.get("findings")
                f = [str(x)[:500] for x in f] if isinstance(f, list) else ([str(f)[:500]] if f else [])
                return {"verdict": obj["verdict"], "findings": f[:20], "note": str(obj.get("note") or "")[:280],
                        "sha": str(obj.get("sha") or "")}
    return None


def _replan_object(text):
    """Read one final planner object from a bounded UTF-8 tail, failing closed."""
    if not isinstance(text, str):
        return None
    tail = text.encode('utf-8')[-32768:].decode('utf-8', errors='replace').rstrip()
    decoder = json.JSONDecoder()
    objects = []
    offset = 0
    while offset < len(tail):
        start = tail.find('{', offset)
        if start < 0:
            break
        try:
            obj, end = decoder.raw_decode(tail, start)
        except ValueError:
            # Do not mistake a nested object inside malformed JSON for a record.
            return None
        objects.append((obj, end))
        offset = end
    if len(objects) != 1 or objects[0][1] != len(tail):
        return None
    return objects[0][0]


def validate_replan_record(record):
    """Validate the task-local action independently of its transport envelope."""
    import re
    if not isinstance(record, dict):
        return None

    def string(value, limit):
        return isinstance(value, str) and 0 < len(value) <= limit and bool(value.strip())

    def spec(obj):
        if not string(obj.get('goal'), 12000):
            return False
        acc, scope = obj.get('acceptance'), obj.get('scope_files')
        if not isinstance(acc, list) or len(acc) > 30 or not all(string(a, 1000) for a in acc):
            return False
        if not isinstance(scope, list) or len(scope) > 100:
            return False
        return all(string(path, 240) and not path.startswith('/') and '\\' not in path
                   and ':' not in path and all(part not in ('', '.', '..') for part in path.split('/'))
                   for path in scope)

    action = record.get('action')
    if action == 'needs-owner':
        valid = set(record) == {'action', 'question'} and string(record['question'], 2000)
    elif action == 'revise':
        valid = set(record) == {'action', 'goal', 'acceptance', 'scope_files'} and spec(record)
    elif action == 'split':
        children = record.get('tasks')
        valid = set(record) == {'action', 'tasks'} and isinstance(children, list) and 2 <= len(children) <= 8
        if valid:
            valid = all(isinstance(t, dict) and set(t) == {'key', 'title', 'goal', 'acceptance', 'scope_files', 'deps'}
                        and isinstance(t['key'], str) and re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', t['key'])
                        and string(t['title'], 200) and spec(t) and isinstance(t['deps'], list)
                        and all(isinstance(d, str) for d in t['deps']) for t in children)
        if valid:
            keys = {t['key'] for t in children}
            valid = len(keys) == len(children) and all(len(t['deps']) == len(set(t['deps']))
                      and set(t['deps']) <= keys - {t['key']} for t in children)
    else:
        valid = False
    return record if valid else None


def parse_replan_record(text) -> dict | None:
    """Preserve the singleton flat final-object contract."""
    return validate_replan_record(_replan_object(text))


def parse_replan_records(text, task_ids) -> dict:
    """Read a bounded envelope; malformed members have no authority over siblings."""
    task_ids = tuple(task_ids)
    obj = _replan_object(text)
    if len(task_ids) == 1:
        record = validate_replan_record(obj)
        return {task_ids[0]: record} if record else {}
    if not isinstance(obj, dict) or set(obj) != {'results'} or not isinstance(obj['results'], list):
        return {}
    known, seen, records = set(task_ids), set(), dict.fromkeys(task_ids)
    for entry in obj['results']:
        if not isinstance(entry, dict) or not isinstance(entry.get('task_id'), str):
            continue
        tid = entry['task_id']
        if tid not in known:
            continue
        if tid in seen:
            records[tid] = None
            continue
        seen.add(tid)
        records[tid] = validate_replan_record(entry.get('result')) if set(entry) == {'task_id', 'result'} else None
    return records
