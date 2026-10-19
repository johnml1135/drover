"""Run a task's gate plan inside one runner process: each command through gate.run_gate, stop at the first red.

    python gatejob.py --plan plan.json      (plan: repo, base, sha, commands [argv...], scope, out, priority)

Writes one receipt (gate.py's shape, plus `scope` and `steps`) for the exact SHA and one combined log beside it.
Exit 0 green, 1 red, 2 refused (dirty tree, HEAD is not the planned SHA, unreadable plan): a refusal writes no receipt.
Runs as a plain script so a WSL runner can start it from any cwd.
"""
import argparse
import json
import os
import sys
import threading
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from drover import gate
else:
    from . import gate


HEARTBEAT_S = 30


def _run_step(step_log, name, *args, **kw):
    """gate.run_gate in a thread; every HEARTBEAT_S, while it runs and its step log has grown, print one line to
    stdout (the run's out.log). A silent step prints nothing, so a hung step still trips the runner's idle watchdog."""
    box = {}
    def work():
        try:
            box["r"] = gate.run_gate(*args, log=step_log, **kw)
        except BaseException as e:
            box["e"] = e
    t = threading.Thread(target=work, daemon=True)
    t.start()
    last = -1
    step_log.touch()
    with open(step_log, "rb") as watch:        # an open handle's size is exact on Windows, where stat() can lag
        while True:
            t.join(HEARTBEAT_S)
            if not t.is_alive():
                break
            size = watch.seek(0, 2)
            if size != last:
                last = size
                print(f"gatejob: {name} running, {size} bytes of output", flush=True)
    if "e" in box:
        raise box["e"]
    return box["r"]


def run_plan(plan):
    os.environ.update(gate.GIT_PROMPT_ENV)
    repo, out = Path(plan["repo"]), Path(plan["out"])
    base, sha, priority = plan.get("base") or "origin/main", plan["sha"], plan.get("priority") or "P2"
    if gate.head(repo) != sha:
        raise gate.GateError(f"HEAD {gate.head(repo)[:12]} is not the planned commit {sha[:12]}")
    if gate.dirty(repo):
        raise gate.GateError("refused: working tree has uncommitted changes; commit the task before gating it")
    out.parent.mkdir(parents=True, exist_ok=True)
    log, steps, texts, started = out.with_suffix(".log"), [], [], gate.now()
    for i, command in enumerate(plan.get("commands") or [], 1):
        step_log = out.with_name(f"{out.stem}.step{i}.log")
        r = _run_step(step_log, f"step{i}", repo, out.with_name(f"{out.stem}.step{i}.json"), command, base=base,
                      priority=priority, project_pattern=plan.get("project_pattern", ""))
        steps.append({"command": r["command"], "exit_code": r["exit_code"], "status": r["status"], "log": r["log"]})
        texts.append(f"$ {' '.join(r['command'])}\n" + step_log.read_text(errors="replace"))
        if r["status"] == "failed":
            break
    log.write_text("\n".join(texts), encoding="utf-8")
    failed = next((s for s in steps if s["status"] == "failed"), None)
    base_sha, has_base = gate.contains(repo, base, sha)
    receipt = dict(version=gate.VERSION, sha=sha, repo=str(repo), base_ref=base, base_sha=base_sha,
                   contains_base=has_base, command=[c for s in steps for c in s["command"]], pins=[], started=started,
                   finished=gate.now(), exit_code=failed["exit_code"] if failed else 0, priority=priority,
                   scope=plan.get("scope"), steps=steps, tree_unchanged=gate.head(repo) == sha and not gate.dirty(repo),
                   log=str(log), log_tail=log.read_text(errors="replace").splitlines()[-40:])
    receipt["passed"] = failed is None and receipt["tree_unchanged"]
    receipt["status"] = "passed" if receipt["passed"] else "failed"
    out.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return receipt


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gatejob")
    ap.add_argument("--plan", required=True)
    a = ap.parse_args(argv)
    try:
        plan = json.loads(Path(a.plan).read_text(encoding="utf-8"))
        r = run_plan(plan)
    except (OSError, ValueError, KeyError, gate.GateError) as e:
        print(f"gatejob: {e}", file=sys.stderr)
        return 2
    print(json.dumps({"sha": r["sha"], "status": r["status"], "steps": len(r["steps"])}))
    return 0 if r["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
