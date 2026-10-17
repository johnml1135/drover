"""Client for Ollama decisions through /v1/systemone.

CPU placement selects a derived -cpu model; ensure-cpu-models provisions these explicitly.
decide() never raises: on any error it returns a fallback result so callers use their rules.
"""
import argparse
import hashlib
import math
import threading
import json
import os
import platform
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from . import config

TIMEOUT = 5.0   # the built-in default of decider.timeout_s
LONG_STATE_TOKENS = 32_000
PORT = 11434
VRAM_TTL_S = 30.0
CRITERIA_FILE = Path(__file__).resolve().parent.parent / "decide-criteria.json"
_cache: dict = {}
_bases: dict = {}


class DecideError(Exception):
    pass


def load_criteria(path=None) -> dict:
    path = Path(path or os.environ.get("DROVER_CRITERIA") or CRITERIA_FILE)
    key = str(path)
    if key not in _cache:
        _cache[key] = json.loads(path.read_text(encoding="utf-8"))
    return _cache[key]


def get_question(name: str, criteria_path=None) -> dict:
    """Return the validated question merged over the file defaults; KeyError if unknown.

    Bad mode becomes shadow and bad tau becomes 0.8, each noted in q['warnings'].
    Bad static criteria raise DecideError.
    """
    data = load_criteria(criteria_path)
    q = {**_config_defaults(data["defaults"]), "name": name, **data["questions"][name]}
    q["warnings"] = []
    if q.get("mode") not in ("shadow", "live"):
        q["warnings"].append(f"invalid mode {q.get('mode')!r}, using shadow")
        q["mode"] = "shadow"
    tau = q.get("tau")
    if isinstance(tau, bool) or not isinstance(tau, (int, float)) or not 0 <= tau <= 1:
        start = config.get()["policy"]["tau"]
        q["warnings"].append(f"invalid tau {tau!r}, using {start:g}")
        q["tau"] = start
    if q["type"] in ("choice", "score") and not q.get("dynamic"):
        check_criteria(q["type"], q["criteria"])
    return q


def _config_defaults(defaults):
    """The criteria file's defaults, with any [decider] / [policy] value the owner changed laid over them (config
    values still at their built-in default leave the file alone)."""
    cfg, built = config.get(), config.DEFAULTS
    out = dict(defaults)
    for key, value, base in (("model", cfg["decider"]["models"]["primary"], built["decider"]["models"]["primary"]),
                             ("shadow_compare_model", cfg["decider"]["models"]["shadow"],
                              built["decider"]["models"]["shadow"]),
                             ("mode", cfg["decider"]["mode"], built["decider"]["mode"]),
                             ("tau", cfg["policy"]["tau"], built["policy"]["tau"])):
        if value != base:
            out[key] = value
    return out


_vram = {"at": None, "value": None}


def vram_percent(read=None, now=time.monotonic):
    """GPU VRAM used, percent (None when unknown), cached for VRAM_TTL_S so a decision never waits on nvidia-smi."""
    t = now()
    if _vram["at"] is None or t - _vram["at"] > VRAM_TTL_S or read is not None:
        from . import resources
        v = (read or resources.read_vram)()
        _vram.update(at=t, value=100.0 * v[0] / v[1] if v and v[1] > 0 else None)
    return _vram["value"]


def placement(device=None, vram=None):
    """Resolve auto to cpu while VRAM is above the configured fallback percentage."""
    cfg = config.get()
    device = device or cfg["decider"]["device"]
    if device == "auto":
        used = vram() if callable(vram) else vram if vram is not None else vram_percent()
        limit = (config.tracker("vram", cfg) or {"fallback_pct": 80.0})["fallback_pct"]
        return "cpu" if used is not None and used > limit else "gpu"
    return device


def cpu_model(model):
    return model if model.endswith("-cpu") else model + "-cpu"


def _tagged(model):
    return model if ":" in model.rsplit("/", 1)[-1] else model + ":latest"


def ensure_cpu_models(models=None, base=None, timeout=120.0):
    """Create missing CPU variants of the configured primary, shadow and long-state models."""
    models = config.get()["decider"]["models"].values() if models is None else models
    base = (base or base_url()).rstrip("/")
    with urllib.request.urlopen(base + "/api/tags", timeout=timeout) as resp:
        tags = json.load(resp)
    present = {_tagged(m.get("name") or m["model"]) for m in tags.get("models", [])}
    out = {"created": [], "existing": []}
    for model in dict.fromkeys(models):
        target = cpu_model(model)
        if _tagged(target) in present:
            if target not in out["existing"] and target not in out["created"]:
                out["existing"].append(target)
            continue
        payload = {"model": target, "from": model, "parameters": {"num_gpu": 0}, "stream": False}
        req = urllib.request.Request(base + "/api/create", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.load(resp)
        if body.get("error"):
            raise DecideError(f"creating CPU model {target!r}: {body['error']}")
        present.add(_tagged(target))
        out["created"].append(target)
    return out


def check_criteria(kind, criteria):
    """choice: dict of 2-26 labels; score: list of 2-10 levels."""
    if kind == "choice":
        ok = isinstance(criteria, dict) and 2 <= len(criteria) <= 26
    else:
        ok = isinstance(criteria, list) and 2 <= len(criteria) <= 10
    if not ok:
        raise DecideError("missing_criteria")


def questions() -> list:
    return list(load_criteria()["questions"])


def _gateway_from_route(text: str):
    for line in text.splitlines():
        parts = line.split()
        if parts[:1] == ["default"] and "via" in parts:
            return parts[parts.index("via") + 1]
    return None


def _is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def base_url(env=None, is_wsl=None, route=None) -> str:
    """DROVER_OLLAMA_URL or decider.url, else the Windows host (WSL gateway) or localhost."""
    env = os.environ if env is None else env
    url = env.get("DROVER_OLLAMA_URL") or config.get()["decider"]["url"]
    if url:
        return url.rstrip("/")
    if platform.system() == "Windows":
        return f"http://127.0.0.1:{PORT}"
    if (_is_wsl() if is_wsl is None else is_wsl):
        try:
            out = route() if route else subprocess.run(
                ["ip", "route"], capture_output=True, text=True, timeout=2).stdout
            gw = _gateway_from_route(out)
        except (OSError, subprocess.SubprocessError):
            gw = None
        if gw:
            return f"http://{gw}:{PORT}"
    return f"http://127.0.0.1:{PORT}"


def base_candidates(env=None, is_wsl=None, route=None) -> list:
    """Base URLs to try in order. On WSL the gateway first, then localhost."""
    env = os.environ if env is None else env
    first = base_url(env, is_wsl, route)
    local = f"http://127.0.0.1:{PORT}"
    if env.get("DROVER_OLLAMA_URL") or config.get()["decider"]["url"] or first == local:
        return [first]
    return [first, local]


def cached_bases() -> list:
    key = os.environ.get("DROVER_OLLAMA_URL", "") or config.get()["decider"]["url"]
    if key not in _bases:
        _bases[key] = base_candidates()
    return _bases[key]


def estimate_tokens(state: str) -> int:
    return len(state) // 3  # code and JSON run near 3 chars/token


def pick_model(q: dict, state: str) -> str:
    long_model = config.get()["decider"]["models"]["long_state"]
    return long_model if estimate_tokens(state) > LONG_STATE_TOKENS else q["model"]


def build_request(q: dict, model: str, state: str, criteria=None) -> dict:
    spec = {"type": q["type"], "instructions": q["instructions"]}
    if q["type"] in ("choice", "score"):
        spec["criteria"] = criteria if criteria is not None else q["criteria"]
    payload = {"model": model, "state": state, "questions": {q["name"]: spec}}
    return payload


def call_systemone(base: str, payload: dict, timeout: float = TIMEOUT) -> dict:
    import urllib.error
    req = urllib.request.Request(
        base.rstrip("/") + "/v1/systemone", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404 and payload["model"].endswith("-cpu"):
            e.close()
            raise DecideError(f"CPU model {payload['model']!r} is missing; "
                              "run python -m drover.decide ensure-cpu-models") from e
        raise


def parse_answer(q: dict, body: dict, criteria=None) -> dict:
    """Normalise one answer to {answer, confidence, probabilities, ...} or raise DecideError."""
    try:
        a = body["answers"][q["name"]]
        kind = q["type"]
        if kind == "noul":
            p = float(a["noul"])
            if not math.isfinite(p) or not 0.0 <= p <= 1.0:
                raise DecideError("noul out of range")
            return {"answer": p >= 0.5, "probability": p, "confidence": max(p, 1 - p),
                    "probabilities": {"true": p, "false": 1 - p}}
        probs = a.get("probabilities")
        if kind == "choice":
            conf = _unit(a["confidence"], "confidence")
            choice = a["choice"]
            if choice not in (criteria if criteria is not None else q["criteria"]):
                raise DecideError(f"unknown choice {choice!r}")
            return {"answer": choice, "confidence": conf, "probabilities": probs}
        # score: a probability-weighted mean of zero-based level indexes; labels are levels
        levels = criteria if criteria is not None else q["criteria"]
        score = a["score"]
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) \
                or not 0 <= score <= len(levels) - 1:
            raise DecideError(f"score out of range: {score!r}")
        values = list(probs.values()) if isinstance(probs, dict) else probs
        if values:
            conf = _unit(max(values), "probability")
        else:
            conf = _unit(a["confidence"], "confidence")
        level = levels[int(score + 0.5)]
        return {"answer": level, "level": level, "score": float(score), "confidence": conf,
                "probabilities": probs, "legend": a.get("legend")}
    except DecideError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise DecideError(f"bad response: {e!r}") from e


def _unit(v, what):
    v = float(v)
    if not math.isfinite(v) or not 0.0 <= v <= 1.0:
        raise DecideError(f"{what} out of range")
    return v


def _ask(base, q, model, state, criteria, timeout):
    t0 = time.monotonic()
    body = call_systemone(base, build_request(q, model, state, criteria), timeout)
    out = parse_answer(q, body, criteria)
    out["model"] = model
    out["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
    out["usage"] = body.get("usage")
    return out


def jsonl_recorder(path=None):
    """Recorder that appends one JSON line per decision; no-op without a path."""
    def record(rec: dict) -> None:
        p = path or os.environ.get("DROVER_DECISIONS_LOG")
        if not p:
            return
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    return record


def _connection_failure(e: Exception) -> bool:
    import urllib.error
    return isinstance(e, (OSError, TimeoutError)) and not isinstance(e, urllib.error.HTTPError)


def _ask_any(bases, q, model, state, criteria, deadline):
    """Try each base in turn within the deadline; connection failures move to the next."""
    last = None
    for i, base in enumerate(bases):
        remaining = deadline - time.monotonic()
        if remaining <= 0.2 and i:
            break
        try:
            out = _ask(base, q, model, state, criteria, max(remaining, 0.2))
            if i:
                _bases[os.environ.get("DROVER_OLLAMA_URL", "") or config.get()["decider"]["url"]] = [base]
            return out
        except Exception as e:
            last = e
            if not _connection_failure(e):
                break
    raise last


def decide(name, state, rule_answer=None, dynamic_criteria=None, *, recorder=None,
           base=None, timeout=None, criteria_path=None, device=None, vram=None) -> dict:
    """Ask the decision model. Never raises.

    'answer' is set only when acted (live and confidence >= tau). Otherwise fallback says why
    ('shadow', 'low_confidence', 'timeout', ...) and the caller uses rule_answer. The model's
    unacted answer is kept in 'shadow_answer'. In shadow mode the compare model is called
    concurrently, within the same total budget.
    """
    result = {"name": name, "answer": None, "shadow_answer": None, "confidence": None,
              "acted": False, "fallback": None, "rule_answer": rule_answer, "mode": None,
              "model": None, "compare": None, "criteria_version": None, "warnings": [],
              "state_hash": hashlib.sha256(state.encode("utf-8", "replace")).hexdigest()[:16]
              if isinstance(state, str) else None}
    holder = {}
    timeout = config.get()["decider"]["timeout_s"] if timeout is None else timeout
    try:
        try:
            q = get_question(name, criteria_path)
        except KeyError as e:
            raise KeyError(name) from e
        selected_device = placement(device, vram)
        result["device"] = selected_device
        result.update(mode=q["mode"], criteria_version=q.get("criteria_version"),
                      warnings=q["warnings"])
        criteria = dynamic_criteria if (q.get("dynamic") or dynamic_criteria) else None
        if q.get("dynamic"):
            check_criteria(q["type"], criteria)
        bases = [base] if base else cached_bases()
        deadline = time.monotonic() + timeout
        model = pick_model(q, state)
        worker = None
        cmp_model = q.get("shadow_compare_model")
        if selected_device == "cpu":
            model = cpu_model(model)
            cmp_model = cpu_model(cmp_model) if cmp_model else None
        result["model"] = model
        if q["mode"] == "shadow" and cmp_model and cmp_model != model:
            worker = threading.Thread(target=_shadow_compare, daemon=True, args=(
                holder, bases, q, cmp_model, state, criteria, deadline))
            worker.start()
        try:
            primary = _ask_any(bases, q, model, state, criteria, deadline)
        finally:
            if worker:
                worker.join(max(deadline - time.monotonic(), 0))
                result["compare"] = holder.get("compare") or {"model": cmp_model, "error": "timeout"}
        result.update(primary, tau=q["tau"])
        if q["mode"] == "shadow":
            result.update(answer=None, shadow_answer=primary["answer"], fallback="shadow")
        elif primary["confidence"] >= q["tau"]:
            result["acted"] = True
        else:
            result.update(answer=None, shadow_answer=primary["answer"], fallback="low_confidence")
    except KeyError as e:
        result["fallback"] = f"unknown_question: {e}"
    except DecideError as e:
        result["fallback"] = "missing_criteria" if str(e) == "missing_criteria" else f"bad_response: {e}"
    except Exception as e:  # network, HTTP, JSON: callers use their rules
        result["fallback"] = _classify_error(e)
    _record(result, recorder)
    return result


def _classify_error(e: Exception) -> str:
    import socket
    import urllib.error
    if isinstance(e, urllib.error.HTTPError):
        e.close()
        return f"http_{e.code}"
    if isinstance(e, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(e, urllib.error.URLError):
        return "timeout" if isinstance(e.reason, (TimeoutError, socket.timeout)) else f"unreachable: {e.reason}"
    if isinstance(e, ValueError):
        return f"bad_response: {e}"
    return f"error: {e!r}"


def _shadow_compare(holder, bases, q, model, state, criteria, deadline):
    try:
        c = _ask_any(bases, q, model, state, criteria, deadline)
        holder["compare"] = {k: c.get(k) for k in ("model", "answer", "confidence", "latency_ms")}
    except Exception as e:
        holder["compare"] = {"model": model, "error": _classify_error(e)}


def _record(result, recorder):
    try:
        (recorder or jsonl_recorder())({**result, "ts": time.time()})
    except Exception:
        pass  # recording must never break a decision


def percentile(values, pct):
    s = sorted(values)
    if not s:
        return None
    return s[min(len(s) - 1, round(pct / 100 * (len(s) - 1)))]


def bench(n=20, question="retry_or_escalate", base=None, timeout=None, device=None) -> dict:
    state = "failure class: test_logic; attempt 2 of 3; diff +40/-12 across 3 files"
    lat, fails = [], 0
    for _ in range(n):
        t0 = time.monotonic()
        r = decide(question, state, base=base, timeout=timeout, recorder=lambda rec: None, device=device)
        if r["shadow_answer"] is None and r["answer"] is None:
            fails += 1
        else:
            lat.append((time.monotonic() - t0) * 1000)
    return {"n": n, "ok": len(lat), "failed": fails,
            "p50_ms": percentile(lat, 50), "p95_ms": percentile(lat, 95)}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    p = argparse.ArgumentParser(prog="python -m drover.decide")
    p.add_argument("name", help="question name, 'bench', or 'ensure-cpu-models'")
    p.add_argument("--state-file")
    p.add_argument("--n", type=int, default=20)
    p.add_argument("--rule-answer")
    p.add_argument("--criteria-json", help="JSON file with dynamic criteria (label: description)")
    p.add_argument("--url")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    if a.name == "ensure-cpu-models":
        try:
            out = ensure_cpu_models(base=a.url)
        except Exception as e:
            message = f"ensure-cpu-models failed: {e}"
            print(json.dumps({"error": message}) if a.json else message)
            return 1
        print(json.dumps(out) if a.json else
              f"created: {', '.join(out['created']) or 'none'}; existing: {', '.join(out['existing']) or 'none'}")
        return 0
    if a.name == "bench":
        out = bench(a.n, base=a.url)
        print(json.dumps(out) if a.json else
              f"n={out['n']} ok={out['ok']} failed={out['failed']} "
              f"p50={out['p50_ms']}ms p95={out['p95_ms']}ms")
        return 0 if out["ok"] else 1
    if not a.state_file:
        p.error("--state-file is required")
    state = Path(a.state_file).read_text(encoding="utf-8")
    dyn = json.loads(Path(a.criteria_json).read_text(encoding="utf-8")) if a.criteria_json else None
    r = decide(a.name, state, rule_answer=a.rule_answer, dynamic_criteria=dyn, base=a.url)
    if a.json:
        print(json.dumps(r, default=str))
    else:
        print(f"{r['name']}: answer={r['answer']} shadow_answer={r['shadow_answer']} confidence={r['confidence']} "
              f"acted={r['acted']} fallback={r['fallback']} model={r['model']}")
        if r["compare"]:
            print(f"  compare: {r['compare']}")
    return 0 if r["answer"] is not None or r["shadow_answer"] is not None else 1


if __name__ == "__main__":
    sys.exit(main())
