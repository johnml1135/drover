"""Ledger run measurements and routing statistics for the tuner."""
from . import contract, route


PASS_OUTCOMES = contract.PASS_OUTCOMES + ("pass", "passed", "gate-pass", "ok", "landed")  # the rest: reviewer rows


def _runs(conn):
    rows = conn.execute("SELECT r.*, t.size AS size, t.status AS task_status, t.repo AS repo "
                        "FROM runs r JOIN tasks t ON t.id=r.task_id WHERE r.ended IS NOT NULL ORDER BY r.id")
    out = []
    for r in rows:
        d = dict(r)
        d["pct"] = contract.cost_pct(d["allowance_used"]) if d["allowance_used"] else None
        out.append(d)
    return out


def _fit_rates(runs):
    """Allowance-% per Mtok per (backend, model): least squares through the origin with >=10 samples, else prior."""
    groups = {}
    for r in runs:
        tok = (r["tokens_in"] or 0) + (r["tokens_out"] or 0)
        if r["pct"] is not None and tok > 0:
            groups.setdefault((r["backend"], r["model"]), []).append((tok / 1e6, r["pct"]))
    rates = {}
    for k, pts in groups.items():
        sxx = sum(x * x for x, _ in pts)
        if len(pts) >= 10 and sxx > 0:
            rates[k] = sum(x * y for x, y in pts) / sxx
    return rates


def _cost(r, rates):
    if r["backend"] == "local":
        return 0.0
    if r["pct"] is not None:
        return r["pct"]
    tok = ((r["tokens_in"] or 0) + (r["tokens_out"] or 0)) / 1e6
    rate = rates.get((r["backend"], r["model"])) or route.prior_rate(r["backend"], r["model"] or "")
    return rate * tok


def _is_worker(r):
    return r["role"] not in ("review", "director", "planner", "gate")


def _by_task(runs):
    tasks = {}
    for r in runs:
        tasks.setdefault(r["task_id"], []).append(r)
    return tasks


def cost_per_landed(runs, rates):
    landed = {r["task_id"] for r in runs if r["task_status"] == "landed"}
    if not landed:
        return None
    return sum(_cost(r, rates) for r in runs) / len(landed)


def cost_per_landed_pass(runs, rates):
    """Same measure over a window of runs; None when nothing landed."""
    return cost_per_landed(runs, rates)


def route_stats(runs, rates):
    """{key: {(backend, model): n, cost_pct, landed, first_pass_n, first_pass_ok}} from all finished runs."""
    stats = {}

    def slot(r):
        key = route.route_key(r["role"], r["size"])
        return stats.setdefault(key, {}).setdefault(
            (r["backend"], r["model"]), {"n": 0, "cost_pct": 0.0, "landed": 0, "first_pass_n": 0, "first_pass_ok": 0})
    for tid, rs in _by_task(runs).items():
        workers = [r for r in rs if _is_worker(r)]
        for r in rs:
            s = slot(r)
            s["n"] += 1
            s["cost_pct"] += _cost(r, rates)
        if workers:
            if workers[-1]["task_status"] == "landed":
                slot(workers[-1])["landed"] += 1
            s = slot(workers[0])
            s["first_pass_n"] += 1
            s["first_pass_ok"] += 1 if workers[0]["outcome"] in PASS_OUTCOMES else 0
        for r in rs:
            if not _is_worker(r) and r["task_status"] == "landed" and r["outcome"] in PASS_OUTCOMES:
                slot(r)["landed"] += 1
    return stats


def update_routes(policy, stats):
    routes = {}
    keys = set(policy.get("routes", {})) | set(route.DEFAULT_ROUTES)
    for key in sorted(keys):
        cands = [dict(c) for c in policy.get("routes", {}).get(key, [])] or route.default_candidates(key)
        for c in cands:
            s = stats.get(key, {}).get((c["backend"], c["model"]))
            if s:
                c.update({k: round(v, 6) if isinstance(v, float) else v for k, v in s.items()})
        routes[key] = cands
    leaders = {}
    for key, cands in routes.items():
        measured = [c for c in route._gated(cands, policy.get("min_first_pass", route.MIN_FIRST_PASS)) if c.get("n")]
        if measured:
            rew = route._rewards(measured)
            leaders[key] = measured[max(range(len(measured)), key=lambda i: (rew[i], -i))]["model"]
    return routes, leaders
