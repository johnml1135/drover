import json
import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from drover import route, tuner
from drover.ledger import connect, ops

OK_H = {"claude": {"remaining_pct": 90, "hours_to_reset": 50}, "codex": {"remaining_pct": 90, "hours_to_reset": 50}}
T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.conn = connect(Path(self.tmp.name) / "l.db")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.conn.close)
        self.clock = 0

    def task(self, size="S", status="landed", repo="p"):
        t = ops.create_task(self.conn, "t", repo, size=size)
        self.conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, t["id"]))
        return t["id"]

    def add_run(self, tid, model="haiku-5.5", backend="claude", outcome="pass", role="implementer", pct=1.0,
            verdict=None, receipt=None):
        self.clock += 60
        s = T0 + timedelta(seconds=self.clock)
        cur = self.conn.execute(
            "INSERT INTO runs(task_id,role,backend,model,started,ended,allowance_used,outcome,review_verdict,gate_receipt)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)", (tid, role, backend, model, iso(s), iso(s + timedelta(seconds=30)),
                                              json.dumps(pct) if pct is not None else None, outcome, verdict, receipt))
        return cur.lastrowid

    def fill(self, n=25, **kw):
        for _ in range(n):
            self.add_run(self.task(**kw))


class Cadence(Base):
    def st(self, **kw):
        return {**tuner.new_state(), **kw}

    def test_first_pass_due_when_enough_runs(self):
        self.assertTrue(tuner.due(T0, self.st(new_runs=20))["due"])

    def test_intervals(self):
        out = []
        for step in range(8):
            s = self.st(step=step, last_pass=iso(T0), new_runs=50)
            d = tuner.due(T0 + timedelta(days=tuner.interval_days(step), seconds=-1), s)
            self.assertFalse(d["due"])
            self.assertTrue(tuner.due(T0 + timedelta(days=tuner.interval_days(step)), s)["due"])
            out.append(tuner.interval_days(step))
        self.assertEqual(out, [1, 1, 2, 3, 5, 7, 7, 7])

    def test_skip_under_20_runs_does_not_advance(self):
        self.fill(5)
        res = tuner.run_pass(self.conn, T0 + timedelta(days=30))
        self.assertFalse(res["ran"])
        self.assertIn("skip", res["reason"])
        self.assertEqual(tuner.load_state(self.conn)["step"], 0)
        self.assertEqual(tuner.load_state(self.conn)["last_pass"], None)

    def test_advance_only_without_adopted_change(self):
        s = tuner.advance(self.st(step=2), adopted=False, reset=False)
        self.assertEqual(s["step"], 3)
        s = tuner.advance(self.st(step=2), adopted=True, reset=False)
        self.assertEqual(s["step"], 2)

    def test_reset_to_daily(self):
        self.assertEqual(tuner.advance(self.st(step=4), adopted=False, reset=True)["step"], 0)

    def test_pass_advances_then_manual_reset(self):
        self.fill(25)
        r = tuner.run_pass(self.conn, T0 + timedelta(days=1))
        self.assertTrue(r["ran"])
        self.assertEqual(tuner.load_state(self.conn)["step"], 1)
        self.assertFalse(tuner.run_pass(self.conn, T0 + timedelta(days=1))["ran"])
        s = tuner.load_state(self.conn)
        s["reset"] = True
        tuner.save_state(self.conn, s)
        self.fill(25)
        r = tuner.run_pass(self.conn, T0 + timedelta(days=3))
        self.assertIn("manual reset", r["resets"])
        self.assertEqual(tuner.load_state(self.conn)["step"], 0)

    def test_reset_on_new_model_and_new_repo(self):
        self.fill(25)
        tuner.run_pass(self.conn, T0 + timedelta(days=1))
        self.add_run(self.task(), model="gpt-6-luna", backend="codex")
        self.assertEqual(tuner.status(self.conn, T0)["state"]["step"], 0)
        self.fill(25)
        tuner.run_pass(self.conn, T0 + timedelta(days=4))
        tuner.run_pass(self.conn, T0 + timedelta(days=4))
        self.task(repo="new-prog")
        self.assertIn("new repo", tuner.detect_triggers(self.conn, tuner.load_state(self.conn))[0])

    def test_reset_when_cost_per_landed_worsens(self):
        self.fill(25, size="S")
        tuner.run_pass(self.conn, T0 + timedelta(days=1))
        for _ in range(25):
            self.add_run(self.task(), pct=2.0)
        r = tuner.run_pass(self.conn, T0 + timedelta(days=3))
        self.assertIn("cost per landed task worsened >15%", r["resets"])
        self.assertEqual(r["step"], 0)


class Routing(Base):
    def pol(self):
        return {"routes": {"worker:S": [
            {"backend": "claude", "model": "haiku-5.5", "effort": "max", "n": 10, "landed": 8, "cost_pct": 8.0},
            {"backend": "codex", "model": "gpt-6-luna", "effort": "xhigh", "n": 10, "landed": 8, "cost_pct": 20.0}]}}

    def test_deterministic_for_seed(self):
        t = {"role": "implementer", "size": "S"}
        a = [route.choose(t, self.pol(), seat_headroom=OK_H, seed=7) for _ in range(5)]
        self.assertEqual(a, [route.choose(t, self.pol(), seat_headroom=OK_H, seed=7) for _ in range(5)])
        r1 = [route.choose(t, self.pol(), seat_headroom=OK_H, rng=random.Random(3)) for _ in range(1)]
        self.assertEqual(r1, [route.choose(t, self.pol(), seat_headroom=OK_H, rng=random.Random(3))])

    def test_exploit_mostly_picks_cheaper(self):
        rng = random.Random(1)
        pool = self.pol()["routes"]["worker:S"]
        pool[1].update(n=2, landed=1, cost_pct=5.0)
        picks = [route.pick_candidate(pool, rng)["model"] for _ in range(500)]
        self.assertGreater(picks.count("haiku-5.5") / 500, 0.75)
        self.assertGreater(picks.count("gpt-6-luna"), 0)

    def test_first_pass_gate_excludes(self):
        pool = self.pol()["routes"]["worker:S"]
        pool[0].update(first_pass_n=10, first_pass_ok=2)
        rng = random.Random(1)
        self.assertEqual({route.pick_candidate(pool, rng)["model"] for _ in range(100)}, {"gpt-6-luna"})

    def test_defaults_and_effort(self):
        r = route.choose({"role": "implementer", "size": "S"}, {}, seat_headroom=OK_H, seed=1)
        self.assertIn(r["model"], ("haiku-5.5", "gpt-6-luna"))
        self.assertEqual(r["effort"], route.TOP_EFFORT[r["backend"]])
        self.assertEqual(route.choose({"role": "review"}, {}, seat_headroom=OK_H, seed=1)["model"], "sonnet-5.5")
        self.assertEqual(route.choose({"role": "director"}, {}, seat_headroom=OK_H, seed=1)["model"], "opus-5.5")
        self.assertEqual(route.choose({"role": "implementer", "size": "M"}, {}, seat_headroom=OK_H, seed=2)["model"] in
                         ("sonnet-5.5", "gpt-6.1-sol"), True)

    def test_unknown_allowance_requires_an_observation(self):
        t = {"role": "implementer", "size": "M"}
        self.assertIsNone(route.choose(t, {}, seat_headroom=None, seed=1)["backend"])
        self.assertEqual(route.choose(t, {}, seat_headroom={"codex": OK_H["codex"]}, seed=1)["backend"], "codex")
        self.assertIsNone(route.choose({"role": "review"}, {}, seat_headroom={"codex": OK_H["codex"]}, seed=1)["backend"])
        self.assertIsNone(route.choose({"role": "director"}, {}, seat_headroom=None, seed=1)["backend"])


    def test_update_routes_fills_stats(self):
        for _ in range(3):
            self.add_run(self.task("S"), pct=1.0)
        runs = tuner._runs(self.conn)
        routes, leaders = tuner.update_routes({}, tuner.route_stats(runs, {}))
        c = [c for c in routes["worker:S"] if c["model"] == "haiku-5.5"][0]
        self.assertEqual((c["n"], c["landed"], c["first_pass_ok"]), (3, 3, 3))
        self.assertEqual(leaders["worker:S"], "haiku-5.5")


class Audit(Base):
    def ids(self):
        return {r["task_id"] for r in tuner._runs(self.conn)}

    def test_missed_after_three_attempts_no_escalation(self):
        t = self.task("S", "failed")
        for _ in range(3):
            self.add_run(t, outcome="gate-fail")
        a = tuner.audit(self.conn, tuner._runs(self.conn), self.ids())
        self.assertEqual(a["missed"], [t])
        self.assertEqual(a["needless"], [])

    def test_missed_on_review_hold_and_replan(self):
        t = self.task()
        self.add_run(t, verdict="HOLD")
        t2 = self.task(status="ready")
        self.add_run(t2)
        ops.event(self.conn, "replan-request", "owner", task_id=t2)
        a = tuner.audit(self.conn, tuner._runs(self.conn), self.ids())
        self.assertEqual(sorted(a["missed"]), sorted([t, t2]))

    def test_escalated_no_miss(self):
        t = self.task()
        self.add_run(t, outcome="gate-fail")
        self.add_run(t, outcome="gate-fail")
        self.add_run(t, model="sonnet-5.5", outcome="pass")
        a = tuner.audit(self.conn, tuner._runs(self.conn), self.ids())
        self.assertEqual(a["missed"], [])

    def test_needless_small_diff_and_same_outcome(self):
        t = self.task()
        self.add_run(t, outcome="gate-fail")
        self.add_run(t, model="sonnet-5.5", outcome="pass", receipt=json.dumps({"diff_lines": 3}))
        t2 = self.task()
        self.add_run(t2, outcome="gate-fail")
        self.add_run(t2, model="sonnet-5.5", outcome="gate-fail")
        t3 = self.task()
        self.add_run(t3, outcome="gate-fail")
        self.add_run(t3, model="sonnet-5.5", outcome="pass", receipt=json.dumps({"diff_lines": 400}))
        a = tuner.audit(self.conn, tuner._runs(self.conn), self.ids())
        self.assertEqual(sorted(a["needless"]), sorted([t, t2]))
        self.assertEqual(a["escalations"], 3)

    def test_tau_direction_and_clamp(self):
        self.assertEqual(tuner.adjust_tau(0.8, 4, 0, 5, 10), 0.78)
        self.assertEqual(tuner.adjust_tau(0.8, 0, 4, 5, 10), 0.82)
        self.assertEqual(tuner.adjust_tau(0.8, 1, 1, 5, 10), 0.8)  # too little evidence
        self.assertEqual(tuner.adjust_tau(0.6, 9, 0, 5, 10), 0.6)
        self.assertEqual(tuner.adjust_tau(0.95, 0, 9, 5, 10), 0.95)

    def test_pass_adjusts_tau_in_new_policy(self):
        for _ in range(8):
            t = self.task("S", "failed")
            for _ in range(3):
                self.add_run(t, outcome="gate-fail")
        r = tuner.run_pass(self.conn, T0 + timedelta(days=1))
        self.assertEqual(r["tau"]["retry_or_escalate"], 0.78)
        pol = tuner.current_policy(self.conn)
        self.assertEqual(pol["tau"]["retry_or_escalate"], 0.78)
        self.assertEqual(ops.current_policy(self.conn), r["version"])


class Proposals(Base):
    def decisions(self, q, n, model_ok, rule_ok, shadow=True):
        for i in range(n):
            d = ops.record_decision(self.conn, q, "v1", "nimble-v3", f"h{i}", "yes", shadow=shadow,
                                    rule_answer="no" if q not in tuner.NO_RULE else None)
            truth = {"label": "yes" if i < model_ok else "no"} if True else None
            ops.record_outcome(self.conn, d, {"correct": i < model_ok, "rule_correct": i < rule_ok})

    def test_promotion_proposed_not_applied(self):
        self.decisions("task_size", 100, 90, 70)
        self.fill(25)
        r = tuner.run_pass(self.conn, T0 + timedelta(days=1))
        qs = [ops.get_question(self.conn, q) for q in r["proposals"]]
        promo = [q for q in qs if q["title"].startswith("Promote task_size")]
        self.assertEqual(len(promo), 1)
        self.assertEqual(promo[0]["status"], "open")
        self.assertTrue(any(o["recommended"] for o in promo[0]["options"]))
        self.assertIsNone(promo[0]["answer_choice"])

    def test_no_promotion_under_100_or_when_rules_win(self):
        self.decisions("a", 99, 99, 0)
        self.decisions("b", 100, 75, 80)
        acc = tuner.shadow_accuracy(self.conn)
        self.assertEqual(tuner.proposals(self.conn, acc, {}), [])

    def test_no_rule_questions_ask_for_labels(self):
        self.decisions("note_admit", 30, 30, 0)
        self.decisions("harness_pick", 30, 30, 0)
        qs = [ops.get_question(self.conn, q) for q in tuner.proposals(self.conn, tuner.shadow_accuracy(self.conn), {})]
        self.assertEqual(len(qs), 2)
        self.assertTrue(all(q["title"].startswith("Validate") for q in qs))
        self.assertIn("spot-label", qs[1]["body"] + qs[0]["body"])

    def test_criteria_review_stub_and_dedupe(self):
        self.decisions("gate_class", 25, 5, 20)
        acc = tuner.shadow_accuracy(self.conn)
        first = tuner.proposals(self.conn, acc, {})
        self.assertEqual(len(first), 1)
        self.assertIn("Sonnet criteria review", [o["label"] for o in ops.get_question(self.conn, first[0])["options"]][0])
        self.assertEqual(tuner.proposals(self.conn, acc, {}), [])

    def test_nothing_auto_applied_and_no_task_approved(self):
        self.decisions("task_size", 100, 90, 70)
        t = ops.create_task(self.conn, "x", "p")
        self.fill(25)
        tuner.run_pass(self.conn, T0 + timedelta(days=1))
        self.assertEqual(ops.get_task(self.conn, t["id"])["status"], "proposed")
        self.assertIsNone(ops.get_task(self.conn, t["id"])["approved_at"])
        self.assertTrue(all(q["status"] == "open" and q["answer_choice"] is None for q in ops.open_questions(self.conn)))
        pol = json.loads(self.conn.execute("SELECT data FROM policy_versions ORDER BY created DESC").fetchone()[0])
        self.assertTrue(set(pol) <= set(tuner.AUTO_KEYS) | {"min_first_pass"})
        self.assertNotIn("promoted", json.dumps(pol))


class BatchAndCap(Base):
    def gates(self, secs, red_every=0, n=10):
        for i in range(n):
            red = bool(red_every) and i % red_every == 0
            ops.event(self.conn, "gate", "x", {"duration_s": secs, "red": red})

    def test_batching_off_when_median_gate_short(self):
        self.gates(600)
        self.assertEqual(tuner.tune_batch(self.conn, 3), 1)

    def test_batch_turns_on_and_adjusts(self):
        self.gates(1000, red_every=2)
        self.assertEqual(tuner.tune_batch(self.conn, 1), 2)
        self.assertEqual(tuner.tune_batch(self.conn, 5), 4)

    def test_batch_grows_when_green_and_slow_and_clamped(self):
        self.gates(2000)
        self.assertEqual(tuner.tune_batch(self.conn, 3), 4)
        self.assertEqual(tuner.tune_batch(self.conn, 5), 5)

    def test_batch_unchanged_with_few_gates(self):
        self.gates(2000, n=2)
        self.assertEqual(tuner.tune_batch(self.conn, 3), 3)

    def test_cap_hill_climb(self):
        s = {"last_lph": 2.0, "last_cap": 4, "last_cap_dir": 1}
        self.assertEqual(tuner.tune_cap(4, 3.0, s), (5, 1))
        self.assertEqual(tuner.tune_cap(4, 1.0, s), (3, -1))
        self.assertEqual(tuner.tune_cap(4, 2.0, s), (4, 0))
        self.assertEqual(tuner.tune_cap(4, 3.0, {"last_lph": None}), (4, 0))
        self.assertEqual(tuner.tune_cap(4, 3.0, {"last_lph": 2.0, "last_cap": 4, "last_cap_dir": 0}), (5, 1))
        self.assertEqual(tuner.tune_cap(8, 3.0, {"last_lph": 2.0, "last_cap": 8, "last_cap_dir": 1}), (8, 1))
        self.assertEqual(tuner.tune_cap(1, 3.0, {"last_lph": 2.0, "last_cap": 1, "last_cap_dir": -1}), (1, -1))


class ColdStartAndPolicy(Base):
    def test_cap_probes_from_cold_start(self):
        st = tuner.new_state()
        cap, d = tuner.tune_cap(4, 1.0, st)  # first sighting: record only
        self.assertEqual((cap, d), (4, 0))
        st.update(last_lph=1.0, last_cap=cap, last_cap_dir=d)
        self.assertEqual(tuner.tune_cap(cap, 1.0, st), (5, 1))

    def test_cap_moves_across_real_passes(self):
        self.fill(25)
        tuner.run_pass(self.conn, T0 + timedelta(days=1))
        self.assertEqual(tuner.current_policy(self.conn)["parallel_cap"], 4)
        self.fill(25)
        r = tuner.run_pass(self.conn, T0 + timedelta(days=2))
        self.assertEqual(r["parallel_cap"], 5)

    def seed_policy(self, **extra):
        data = {"routes": {}, "tau": {"retry_or_escalate": 0.8, "plan_wrong": 0.8}, **extra}
        ops.add_policy_version(self.conn, "p1", data, "owner")
        return data

    def test_extra_keys_round_trip_byte_identical(self):
        extra = {"safety": ["never push", {"a": 1}], "pinned": ["rule x"], "criteria": {"gate_class": "v3"},
                 "tau": {"retry_or_escalate": 0.8, "plan_wrong": 0.8, "other_q": 0.7}, "brief": "b1"}
        self.seed_policy(**{k: v for k, v in extra.items() if k != "tau"})
        pol = json.loads(self.conn.execute("SELECT data FROM policy_versions WHERE version='p1'").fetchone()[0])
        pol["tau"]["other_q"] = 0.7
        self.conn.execute("UPDATE policy_versions SET data=? WHERE version='p1'", (json.dumps(pol),))
        self.fill(25)
        r = tuner.run_pass(self.conn, T0 + timedelta(days=1))
        self.assertEqual(r["version"], "p2")
        new = json.loads(self.conn.execute("SELECT data FROM policy_versions WHERE version='p2'").fetchone()[0])
        for k in ("safety", "pinned", "criteria", "brief"):
            self.assertEqual(json.dumps(new[k], sort_keys=True), json.dumps(extra[k], sort_keys=True))
        self.assertEqual(new["tau"]["other_q"], 0.7)

    def test_out_of_range_tau_clamped_on_read(self):
        self.seed_policy()
        self.conn.execute("UPDATE policy_versions SET data=? WHERE version='p1'",
                          (json.dumps({"tau": {"retry_or_escalate": 0.2, "plan_wrong": 0.99}}),))
        p = tuner.current_policy(self.conn)
        self.assertEqual((p["tau"]["retry_or_escalate"], p["tau"]["plan_wrong"]), (0.6, 0.95))

    def test_version_ids_follow_max_and_state_saved_together(self):
        ops.add_policy_version(self.conn, "p7", {"routes": {}}, "owner")
        self.fill(25)
        r = tuner.run_pass(self.conn, T0 + timedelta(days=1))
        self.assertEqual(r["version"], "p8")
        self.assertEqual(tuner.load_state(self.conn)["last_pass"], iso(T0 + timedelta(days=1)))

    def test_failed_pass_rolls_back_version_and_state(self):
        self.fill(25)
        orig = tuner.advance
        tuner.advance = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            with self.assertRaises(RuntimeError):
                tuner.run_pass(self.conn, T0 + timedelta(days=1))
        finally:
            tuner.advance = orig
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM policy_versions").fetchone()[0], 0)
        self.assertIsNone(tuner.load_state(self.conn)["last_pass"])


class Cli(Base):
    def test_cli(self):
        import io
        from contextlib import redirect_stdout
        db = str(Path(self.tmp.name) / "l.db")
        self.fill(25)
        for cmd in (["due"], ["status"], ["run", "--force"]):
            out = io.StringIO()
            with redirect_stdout(out):
                tuner.main(["--db", db, "--json", *cmd])
            json.loads(out.getvalue())


if __name__ == "__main__":
    unittest.main()


class DecisionHistory(Base):
    def test_changed_policy_has_a_committed_record_and_snapshot(self):
        from drover import config, decisions
        from unittest.mock import patch
        state = Path(self.tmp.name) / 'state'
        decisions.initialize(state)
        cfg = config.validate({'paths': {'state_dir': str(state)}})
        self.fill()
        with patch.object(config, 'get', return_value=cfg):
            result = tuner.run_pass(self.conn, T0 + timedelta(days=1))
        self.assertIsNotNone(result['version'])
        policy = json.loads((state / 'policy.json').read_text())
        self.assertEqual(policy['version'], result['version'])
        records = list((state / 'decisions').glob('*.md'))
        self.assertEqual(len(records), 1)
        self.assertIn('author: tuner', records[0].read_text())
        count = len(records)
        with patch.object(config, 'get', return_value=cfg):
            self.assertFalse(tuner.run_pass(self.conn, T0 + timedelta(days=1))['ran'])
        self.assertEqual(len(list((state / 'decisions').glob('*.md'))), count)
