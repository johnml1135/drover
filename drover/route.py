"""Pick seat, backend, model, effort and harness for a task: seats by priority then headroom, UCB1 over cost per
landed task, allowance-aware. Seats, ladders, efforts and reserves come from drover.config; the defaults are the old
built-in values (two seats of equal priority, so the old headroom-biased split between Claude and Codex applies)."""
import math
import random

from . import config as config_mod

MIN_FIRST_PASS = 0.5
MIN_GATE_N = 5
EXPLOIT = 0.8
BIAS_CLAMP = (0.2, 0.8)
DIRECTOR_ROLES = ("director", "planner")
PRIOR_FIRST_PASS = 0.5
PRIOR_MTOK = 0.5  # assumed Mtok per run when pricing an untried candidate

_PRIOR_RATIO = {"claude": {"opus": 2.0, "sonnet": 1.0, "haiku": 0.05}, "codex": {"sol": 20.0, "luna": 1.0}}
_PRIOR_BASE = {"claude": "sonnet", "codex": "luna"}


def top_effort(cfg=None):
    """Highest effort per backend for small tasks (verify against the model docs)."""
    e = (cfg or config_mod.get())["models"]["efforts"]
    return {"claude": e["small_claude"], "codex": e["small_codex"]}


def default_routes(cfg=None):
    """{route key: [(backend, model)]} from the ladders: S starts at the cheap rung, M and L one up; review and
    director use the configured models."""
    m = (cfg or config_mod.get())["models"]
    ladders = m["ladders"]

    def rung(backend, size):
        ladder = ladders[backend]
        return ladder[min(m["start"][size], len(ladder) - 1)]
    both = [b for b in ("claude", "codex") if b in ladders]
    return {**{f"worker:{size}": [(b, rung(b, size)) for b in both] for size in ("S", "M", "L")},
            "review": [("claude", m["review"])], "director": [("claude", m["director"])]}


def __getattr__(name):   # TOP_EFFORT and DEFAULT_ROUTES stay importable; they follow the active config
    if name == "TOP_EFFORT":
        return top_effort()
    if name == "DEFAULT_ROUTES":
        return default_routes()
    raise AttributeError(name)


def route_key(role, size):
    if role in ("review", "director"):
        return role
    if role == "planner":
        return "director"
    return f"worker:{size or 'M'}"


def default_candidates(key):
    out, routes = [], default_routes()
    for backend, model in routes.get(key, routes["worker:M"]):
        effort = top_effort()[backend] if key == "worker:S" else config_mod.get()["models"]["efforts"]["mid"]
        out.append({"backend": backend, "model": model, "effort": effort, "harness": "default"})
    return out


def prior_rate(backend, model):
    """Allowance-% per Mtok: the allowance module's fit when importable, else the same ratio prior."""
    try:
        from . import allowance
        return allowance.fit_rate([], plan=backend, model=model)["rate"]
    except ImportError:
        pass
    except ValueError:
        return 1.0
    fam = next((f for f in _PRIOR_RATIO.get(backend, {}) if f in model.lower()), None)
    if fam is None:
        return 1.0
    return _PRIOR_RATIO[backend][fam] / _PRIOR_RATIO[backend][_PRIOR_BASE[backend]]


def reserves(seats):
    """{backend: (reserve_pct, director_reserve_pct)} from the seats: the best-priority seat's reserve, and the
    director seat's extra reserve that workers on that backend must leave untouched."""
    out = {}
    for seat in sorted(seats, key=lambda s: s["priority"]):
        r, d = out.get(seat["backend"], (seat["reserve_pct"], 0.0))
        out[seat["backend"]] = (r, max(d, seat["director_reserve_pct"] if seat["director"] else 0.0))
    return out


def _norm_headroom(h, backend=None, seats=None):
    """Accept {remaining_pct, hours_to_reset, ...} or an observed allowance reading; None means unconstrained."""
    if h is None:
        return None
    if "windows" in h:
        w = h["windows"].get("seven_day") or next(iter(h["windows"].values()), None)
        if w is None:
            return None
        h = {"remaining_pct": w["remaining_pct"], "hours_to_reset": w.get("hours_to_reset"), **h}
    reserve, director = reserves(seats or config_mod.get()["seats"]).get(backend, (10.0, 0.0))
    return {"remaining_pct": float(h.get("remaining_pct", 100.0)), "hours_to_reset": h.get("hours_to_reset"),
            "reserve_pct": float(h.get("reserve_pct", reserve)),
            "director_reserve_pct": h.get("director_reserve_pct", director)}


def _floor(backend, h, privileged):
    return h["reserve_pct"] + (0.0 if privileged else float(h["director_reserve_pct"] or 0.0))


def _est_run_pct(c):
    n = c.get("n", 0)
    if n and c.get("cost_pct") is not None:
        return c["cost_pct"] / n
    return prior_rate(c["backend"], c["model"]) * PRIOR_MTOK


def est_cost_per_landed(c):
    n, landed = c.get("n", 0), c.get("landed", 0)
    if not n:
        return prior_rate(c["backend"], c["model"]) * PRIOR_MTOK / PRIOR_FIRST_PASS
    return c.get("cost_pct", 0.0) / max(landed, 0.5)


def _gated(cands, min_first_pass):
    ok = [c for c in cands
          if not (c.get("first_pass_n", 0) >= MIN_GATE_N
                  and c.get("first_pass_ok", 0) / c["first_pass_n"] < min_first_pass)]
    return ok or cands


def _rewards(cands):
    costs = [max(est_cost_per_landed(c), 1e-9) for c in cands]
    best = min(costs)
    return [best / x for x in costs]


def pick_candidate(cands, rng, min_first_pass=MIN_FIRST_PASS):
    """80% exploit the best mean reward, 20% explore by UCB1; untried candidates are tried first when exploring."""
    cands = _gated(cands, min_first_pass)
    rew = _rewards(cands)
    total = sum(c.get("n", 0) for c in cands)
    if rng.random() < EXPLOIT:
        scores = rew
    else:
        scores = [math.inf if not c.get("n", 0) else r + math.sqrt(2 * math.log(max(total, 2)) / c["n"])
                  for r, c in zip(rew, cands)]
    top = max(scores)
    return cands[[i for i, s in enumerate(scores) if s == top][0]]


def _p_claude(heads, privileged):
    """Share of workers sent to Claude: 50/50, biased by usable headroom per hour to reset."""
    slack = {}
    for b in ("claude", "codex"):
        h = heads.get(b)
        if h is None:
            return 0.5
        hrs = h["hours_to_reset"]
        usable = max(0.0, h["remaining_pct"] - _floor(b, h, privileged))
        slack[b] = usable / max(float(hrs), 1.0) if hrs is not None else usable
    tot = slack["claude"] + slack["codex"]
    if tot <= 0:
        return 0.5
    lo, hi = BIAS_CLAMP
    return min(hi, max(lo, slack["claude"] / tot))


def seat_placements(seat):
    """Where a seat's workers may run: Codex only where its seat is; Claude on its seat's side or, for a Windows
    seat, also in WSL (the old rule: a linux task tag moves a Claude worker there)."""
    if seat["backend"] == "codex" or seat["placement"] == "wsl":
        return (seat["placement"],)
    return ("windows", "wsl")


def eligible_seats(seats, seat_load=None, platforms=None, privileged=False):
    """Seats with a free worker slot that can run somewhere the repo allows; a director or planner run only takes
    a seat of the director chain (config.director_seats): any seat, tried in that order."""
    load = seat_load or {}
    out = []
    for s in seats:
        if s["max_workers"] and load.get(s["name"], 0) >= s["max_workers"]:
            continue
        if platforms is not None and not set(seat_placements(s)) & set(platforms):
            continue
        out.append(s)
    return out


def no_seat_route(seats, platforms=None, privileged=False, seat_load=None):
    """Distinguish a permanent platform mismatch from temporarily full seats."""
    compatible = eligible_seats(seats, platforms=platforms, privileged=privileged)
    reason = ("no seat can take this run (all at max_workers)" if compatible else
              "no platform-compatible seat for " + ", ".join(platforms or config_mod.PLACEMENTS))
    return {"backend": None, "model": None, "effort": None, "harness": None, "seat": None,
            "reason": reason, "needs_owner": not compatible}


def pick_seat(seats, backend, seat_load=None):
    """The best seat of a backend: lowest priority number, then fewest live workers, then name."""
    load = seat_load or {}
    return min((s for s in seats if s["backend"] == backend), key=lambda s: (s["priority"], load.get(s["name"], 0),
                                                                             s["name"]))


def _backends_by_priority(allowed, seats):
    """The backends of `allowed` whose best seat has the lowest priority number among them."""
    best = {}
    for c in allowed:
        pr = min(s["priority"] for s in seats if s["backend"] == c["backend"])
        best[c["backend"]] = pr
    low = min(best.values())
    return sorted(b for b, pr in best.items() if pr == low)


def _seat_admits(seat, h, protected):
    """A seat's reading clears its floors: five_hour by the reset-time formula, seven_day (or a plain remaining
    figure) by the seat's reserve when protected."""
    if not h or h["remaining_pct"] <= 0:
        return False
    windows = h.get("windows") or {}
    for name, w in windows.items():
        if name == "five_hour":
            from .allowance import five_hour_floor
            floor = five_hour_floor(w.get("hours_to_reset"), seat.get("five_hour_base_pct"),
                                    seat.get("five_hour_per_hour_pct"))
        else:
            floor = seat["reserve_pct"] if protected else 0.0
        if w["remaining_pct"] <= floor:
            return False
    return bool(windows) or not protected or h["remaining_pct"] > seat["reserve_pct"]


def _observed_choice(task, policy, seats, seat_load, platforms, heads, ordering, rng, borrow_approved):
    """Choose an account using measured allowance only; model policy stays inside that account."""
    cfg = config_mod.get()
    role, key = task.get("role", "worker"), route_key(task.get("role", "worker"), task.get("size"))
    privileged = role in DIRECTOR_ROLES
    requested = task.get("director_seat") if privileged else None
    default = cfg["director"]["seat"] or config_mod.director_seat(cfg)["name"]
    if requested and requested != default and not borrow_approved:
        return {"backend": None, "seat": None, "reason": "director borrowing requires owner approval",
                "needs_owner": True, "borrow_seat": requested}
    candidates = [dict(c) for c in (policy or {}).get("routes", {}).get(key, [])] or default_candidates(key)
    if privileged:
        candidates = [{"backend": "claude", "model": cfg["director"]["model"] or cfg["models"]["director"],
                       "effort": cfg["director"]["effort"]}]
    allowed = eligible_seats(seats, seat_load, platforms)
    if privileged:
        chain = [s["name"] for s in config_mod.director_seats({"seats": seats})]
        allowed = [s for s in allowed if s["name"] == requested] if requested else \
            sorted(allowed, key=lambda s: chain.index(s["name"]))
    if not allowed and not (privileged and not requested):
        return no_seat_route(seats, platforms, privileged and not requested, seat_load)
    ranks = {r["seat"]: r for r in ordering or []}
    if not privileged or requested:
        allowed.sort(key=lambda s: (not ranks.get(s["name"], {}).get("urgent", False), s["priority"],
                                    (seat_load or {}).get(s["name"], 0), s["name"]))
    for seat in allowed:
        h = heads.get(seat["name"])
        # The default director may use its reserve. Other accounts keep their own floor.
        protected = not (privileged and seat["name"] == default)
        if not _seat_admits(seat, h, protected):
            continue
        pool = [c for c in candidates if c["backend"] == seat["backend"]]
        if not pool and privileged:    # last-resort codex seat: the configured worker model for that backend
            pool = [c for c in default_candidates("worker:M") if c["backend"] == seat["backend"]]
        if not pool:
            continue
        c = pick_candidate(pool, rng, (policy or {}).get("min_first_pass", MIN_FIRST_PASS))
        return {"seat": seat["name"], "backend": seat["backend"], "model": c["model"],
                "effort": c.get("effort") or (top_effort(cfg)[seat["backend"]] if key == "worker:S"
                                               else cfg["models"]["efforts"]["mid"]),
                "harness": c.get("harness", "default"),
                "reason": ranks.get(seat["name"], {}).get("reason", f"priority {seat['priority']}")}
    return {"backend": None, "seat": None, "reason": "no allowance above reserve"}


def choose(task, policy, allowance_headroom=None, seed=None, rng=None, seats=None, seat_load=None, platforms=None,
           seat_headroom=None, seat_order=None, director_borrow_approved=False):
    """Return {seat, backend, model, effort, harness}; backend is None (with a reason) when no seat can take the run.
    seats default to the config's; seat_load is {seat name: live workers}; platforms is the repo's run platforms."""
    rng = rng or random.Random(seed)
    cfg = config_mod.get()
    all_seats = seats if seats is not None else cfg["seats"]
    if seat_headroom is not None:
        return _observed_choice(task, policy, all_seats, seat_load, platforms, seat_headroom, seat_order, rng,
                                director_borrow_approved)
    role = task.get("role", "worker")
    key = route_key(role, task.get("size"))
    privileged = role in DIRECTOR_ROLES
    seats = eligible_seats(all_seats, seat_load, platforms, privileged)
    if not seats:
        return no_seat_route(all_seats, platforms, privileged, seat_load)
    routes = (policy or {}).get("routes", {})
    cands = [dict(c) for c in routes.get(key, [])] or default_candidates(key)
    if key == "director":
        family = cfg["models"]["director"].split("-")[0]
        cands = [c for c in cands if c["model"].startswith(family)] or default_candidates("director")
    have = {s["backend"] for s in seats}
    if privileged:   # codex is the last resort for director work
        cands += [c for c in default_candidates("worker:M") if c["backend"] != "claude"]
    cands = [c for c in cands if c["backend"] in have]
    heads = {b: _norm_headroom((allowance_headroom or {}).get(b), b, all_seats) for b in ("claude", "codex")}
    allowed = []
    for c in cands:
        h = heads.get(c["backend"])
        if h is None:
            if privileged:
                allowed.append(c)  # directors may probe without an observation, as in the allowance module
            continue
        if h["remaining_pct"] - _est_run_pct(c) < _floor(c["backend"], h, privileged):
            continue
        allowed.append(c)
    if not allowed:
        return {"backend": None, "model": None, "effort": None, "harness": None, "seat": None,
                "reason": "no allowance above reserve"}
    backends = _backends_by_priority(allowed, seats)
    if privileged:
        backend = "claude" if any(c["backend"] == "claude" for c in allowed) else allowed[0]["backend"]
    elif len(backends) == 2:
        backend = "claude" if rng.random() < _p_claude(heads, privileged) else "codex"
    else:
        backend = backends[0]
    pool = [c for c in allowed if c["backend"] == backend]
    c = pick_candidate(pool, rng, (policy or {}).get("min_first_pass", MIN_FIRST_PASS))
    effort = c.get("effort") or (top_effort(cfg)[c["backend"]] if key == "worker:S"
                                 else cfg["models"]["efforts"]["mid"])
    seat = (next(x for x in config_mod.director_seats({"seats": seats}) if x["backend"] == backend) if privileged
            else pick_seat(seats, backend, seat_load))
    return {"seat": seat["name"], "backend": c["backend"], "model": c["model"], "effort": effort,
            "harness": c.get("harness", "default")}
