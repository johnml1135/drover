import io
import json
import os
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

from drover import decide

CHOICES = ["syntax", "type", "test_logic", "build_system", "env_flaky", "scope_violation"]


class Fake:
    """Fake HTTP transport; behaviour per model returns (status, body)."""

    def __init__(self):
        self.requests = []
        self.behaviour = {}
        self.url = "http://acme-lib:11434"
        self.patch = mock.patch("urllib.request.urlopen", side_effect=self.respond)
        self.patch.start()

    def respond(self, request, timeout):
        import urllib.error
        if not request.full_url.startswith(self.url + "/"):
            raise urllib.error.URLError("connection refused")
        req = json.loads(request.data)
        req["_path"] = request.selector
        self.requests.append(req)
        fn = self.behaviour.get(req["model"], self.behaviour.get("*"))
        code, body = fn(req)
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        if code >= 400:
            raise urllib.error.HTTPError(request.full_url, code, "fake HTTP error", {}, io.BytesIO(raw))
        return io.BytesIO(raw)

    def close(self):
        self.patch.stop()


def answer_for(req, **kw):
    name = next(iter(req["questions"]))
    return 200, {"answers": {name: kw}, "usage": {"tokens": 1}}


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = Fake()
        self.addCleanup(self.fake.close)
        self.recs = []
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_decide(self, name, state="x", **kw):
        kw.setdefault("timeout", 2)
        return decide.decide(name, state, base=self.fake.url, recorder=self.recs.append, **kw)

    def criteria_file(self, mode="live", tau=None):
        data = json.loads(decide.CRITERIA_FILE.read_text())
        data["defaults"]["mode"] = mode
        if tau is not None:
            data["defaults"]["tau"] = tau
        p = Path(self.tmp.name) / f"c{time.monotonic_ns()}.json"
        p.write_text(json.dumps(data))
        return p


class TestRegistry(unittest.TestCase):
    def test_all_questions_present_and_shadow(self):
        names = {"gate_failure_class", "retry_or_escalate", "task_size", "task_risk", "harness_pick",
                 "note_admit", "lane_stuck", "plan_wrong", "landed_diff_stale", "batch_culprit",
                 "branch_superseded"}
        self.assertEqual(set(decide.questions()), names)
        for n in names:
            q = decide.get_question(n)
            self.assertEqual((q["mode"], q["tau"], q["model"], q["shadow_compare_model"]),
                             ("shadow", 0.8, "nimble-v3", "nimble"))
            self.assertIn("criteria_version", q)
        self.assertEqual(list(decide.get_question("gate_failure_class")["criteria"]), CHOICES)
        self.assertEqual(decide.get_question("task_size")["criteria"], ["S", "M", "L"])
        self.assertEqual(decide.get_question("task_risk")["criteria"], ["low", "high"])


class TestTypes(Base):
    def test_choice_request_and_answer(self):
        self.fake.behaviour["*"] = lambda r: answer_for(
            r, choice="syntax", probabilities={"syntax": 0.9}, confidence=0.9)
        r = self.run_decide("gate_failure_class", "boom")
        req = next(r for r in self.fake.requests if r["model"] == "nimble-v3")
        self.assertEqual(req["_path"], "/v1/systemone")
        self.assertEqual(req["model"], "nimble-v3")
        self.assertEqual(req["state"], "boom")
        spec = req["questions"]["gate_failure_class"]
        self.assertEqual(spec["type"], "choice")
        self.assertEqual(list(spec["criteria"]), CHOICES)
        self.assertEqual((r["answer"], r["shadow_answer"], r["confidence"], r["fallback"]),
                         (None, "syntax", 0.9, "shadow"))

    def test_noul(self):
        self.fake.behaviour["*"] = lambda r: answer_for(r, noul=0.15)
        r = self.run_decide("retry_or_escalate")
        spec = self.fake.requests[0]["questions"]["retry_or_escalate"]
        self.assertEqual(set(spec), {"type", "instructions"})
        self.assertEqual(spec["type"], "noul")
        self.assertIs(r["shadow_answer"], False)
        self.assertAlmostEqual(r["confidence"], 0.85)

    def test_score(self):
        self.fake.behaviour["*"] = lambda r: answer_for(
            r, score=1.1, legend=["S", "M", "L"], probabilities=[0.1, 0.7, 0.2])
        r = self.run_decide("task_size")
        self.assertEqual(self.fake.requests[0]["questions"]["task_size"]["criteria"], ["S", "M", "L"])
        self.assertEqual((r["shadow_answer"], r["level"], r["legend"]), ("M", "M", ["S", "M", "L"]))
        self.assertEqual((r["score"], r["confidence"]), (1.1, 0.7))  # confidence = max probability

    def test_dynamic_criteria(self):
        self.fake.behaviour["*"] = lambda r: answer_for(r, choice="t2", probabilities={}, confidence=0.6)
        crit = {"t1": "task one", "t2": "task two"}
        r = self.run_decide("batch_culprit", dynamic_criteria=crit)
        self.assertEqual(self.fake.requests[0]["questions"]["batch_culprit"]["criteria"], crit)
        self.assertEqual(r["shadow_answer"], "t2")

    def test_dynamic_criteria_missing_falls_back_without_call(self):
        r = self.run_decide("batch_culprit")
        self.assertEqual(r["fallback"], "missing_criteria")
        self.assertEqual(self.fake.requests, [])


class TestFallback(Base):
    def check(self, prefix, name="retry_or_escalate", **kw):
        r = self.run_decide(name, **kw)
        self.assertIsNone(r["answer"])
        self.assertFalse(r["acted"])
        self.assertTrue(r["fallback"].startswith(prefix), r["fallback"])
        self.assertEqual(len(self.recs), 1)
        return r

    def test_timeout(self):
        def timed_out(r):
            raise TimeoutError("fake HTTP timeout")
        self.fake.behaviour["*"] = timed_out
        r = self.check("timeout", timeout=0.2, rule_answer="retry")
        self.assertEqual(r["rule_answer"], "retry")

    def test_http_500(self):
        self.fake.behaviour["*"] = lambda r: (500, {"error": "boom"})
        self.check("http_500")

    def test_bad_json(self):
        self.fake.behaviour["*"] = lambda r: (200, b"not json{")
        self.check("bad_response")

    def test_missing_answer(self):
        self.fake.behaviour["*"] = lambda r: (200, {"answers": {}})
        self.check("bad_response")

    def test_unknown_choice(self):
        self.fake.behaviour["*"] = lambda r: answer_for(r, choice="nonsense", confidence=0.99)
        self.check("bad_response", name="gate_failure_class")

    def test_unreachable(self):
        r = decide.decide("retry_or_escalate", "x", base="http://127.0.0.1:9", timeout=1,
                          recorder=self.recs.append)
        self.assertIsNone(r["answer"])
        self.assertIsNotNone(r["fallback"])

    def test_unknown_question_does_not_raise(self):
        r = self.run_decide("nope")
        self.assertTrue(r["fallback"].startswith("unknown_question"))

    def test_broken_recorder_does_not_raise(self):
        self.fake.behaviour["*"] = lambda r: answer_for(r, noul=0.9)
        def bad(rec):
            raise RuntimeError("x")
        r = decide.decide("retry_or_escalate", "x", base=self.fake.url, recorder=bad)
        self.assertTrue(r["shadow_answer"])


class TestValidation(Base):
    def crit_file(self, mutate):
        data = json.loads(decide.CRITERIA_FILE.read_text())
        mutate(data["questions"])
        p = Path(self.tmp.name) / f"v{time.monotonic_ns()}.json"
        p.write_text(json.dumps(data))
        return p

    def test_static_criteria_counts_rejected(self):
        self.fake.behaviour["*"] = lambda r: answer_for(r, noul=0.9)
        cases = [
            lambda q: q["gate_failure_class"].update(criteria={"only": "one"}),
            lambda q: q["gate_failure_class"].update(criteria={chr(97 + i): "x" for i in range(27)}),
            lambda q: q["task_size"].update(criteria=["S"]),
            lambda q: q["task_size"].update(criteria=[str(i) for i in range(11)]),
        ]
        for i, m in enumerate(cases):
            name = "gate_failure_class" if i < 2 else "task_size"
            r = self.run_decide(name, criteria_path=self.crit_file(m))
            self.assertEqual(r["fallback"], "missing_criteria")
        self.assertEqual(self.fake.requests, [])

    def test_dynamic_score_list(self):
        self.fake.behaviour["*"] = lambda r: answer_for(r, score=0.6, probabilities=[0.4, 0.6])
        path = self.crit_file(lambda q: q["task_size"].update(dynamic=True, criteria=[]))
        r = self.run_decide("task_size", dynamic_criteria=["a", "b"], criteria_path=path)
        self.assertEqual(r["shadow_answer"], "b")

    def test_confidence_out_of_range(self):
        for bad in (1.5, -0.2, float("inf")):
            self.fake.behaviour["*"] = lambda r, bad=bad: answer_for(
                r, choice="syntax", probabilities={}, confidence=bad)
            r = self.run_decide("gate_failure_class")
            self.assertTrue(r["fallback"].startswith("bad_response"), r["fallback"])
            self.assertIsNone(r["shadow_answer"])

    def test_score_boundaries(self):
        ok = {0: "S", 0.49: "S", 2: "L", 1.5: "L", 1: "M"}
        for score, label in ok.items():
            self.fake.behaviour["*"] = lambda r, sc=score: answer_for(
                r, score=sc, probabilities=[0.2, 0.3, 0.5])
            r = self.run_decide("task_size")
            self.assertEqual((r["level"], r["fallback"]), (label, "shadow"), score)
            self.assertEqual(r["score"], float(score))

    def test_score_out_of_range_or_bad(self):
        for bad in (-0.01, 2.01, float("nan"), float("inf"), "M", True, None):
            self.fake.behaviour["*"] = lambda r, b=bad: answer_for(
                r, score=b, probabilities=[0.2, 0.3, 0.5])
            r = self.run_decide("task_size")
            self.assertTrue(r["fallback"].startswith("bad_response"), (bad, r["fallback"]))
            self.assertIsNone(r["shadow_answer"])

    def test_score_probability_out_of_range(self):
        self.fake.behaviour["*"] = lambda r: answer_for(r, score=1, probabilities=[0.1, 1.7, 0.2])
        self.assertTrue(self.run_decide("task_size")["fallback"].startswith("bad_response"))

    def test_live_score_acts_on_probability(self):
        self.fake.behaviour["*"] = lambda r: answer_for(r, score=1.9, probabilities=[0.05, 0.15, 0.8])
        r = self.run_decide("task_size", criteria_path=self.criteria_file("live"))
        self.assertTrue(r["acted"])
        self.assertEqual(r["answer"], "L")


class TestTau(Base):
    def test_live_above_tau_acts(self):
        self.fake.behaviour["*"] = lambda r: answer_for(r, noul=0.9)
        r = self.run_decide("retry_or_escalate", criteria_path=self.criteria_file("live"))
        self.assertTrue(r["acted"])
        self.assertIs(r["answer"], True)
        self.assertIsNone(r["fallback"])

    def test_live_at_tau_acts(self):
        self.fake.behaviour["*"] = lambda r: answer_for(r, noul=0.8)
        r = self.run_decide("retry_or_escalate", criteria_path=self.criteria_file("live"))
        self.assertTrue(r["acted"])

    def test_live_below_tau_falls_back(self):
        self.fake.behaviour["*"] = lambda r: answer_for(r, noul=0.7)
        r = self.run_decide("retry_or_escalate", criteria_path=self.criteria_file("live"))
        self.assertFalse(r["acted"])
        self.assertEqual(r["fallback"], "low_confidence")
        self.assertIsNone(r["answer"])
        self.assertIs(r["shadow_answer"], True)

    def test_shadow_never_acts(self):
        self.fake.behaviour["*"] = lambda r: answer_for(r, noul=0.99)
        r = self.run_decide("retry_or_escalate")
        self.assertFalse(r["acted"])
        self.assertEqual(r["mode"], "shadow")
        self.assertEqual(r["fallback"], "shadow")
        self.assertIsNone(r["answer"])
        self.assertIs(r["shadow_answer"], True)

    def test_invalid_mode_is_shadow_with_warning(self):
        self.fake.behaviour["*"] = lambda r: answer_for(r, noul=0.99)
        r = self.run_decide("retry_or_escalate", criteria_path=self.criteria_file("Live"))
        self.assertFalse(r["acted"])
        self.assertEqual((r["mode"], r["fallback"]), ("shadow", "shadow"))
        self.assertTrue(any("mode" in w for w in r["warnings"]))
        self.assertTrue(self.recs[0]["warnings"])

    def test_invalid_tau_uses_default(self):
        self.fake.behaviour["*"] = lambda r: answer_for(r, noul=0.7)
        for bad in (1.5, -0.1, "x"):
            r = self.run_decide("retry_or_escalate", criteria_path=self.criteria_file("live", bad))
            self.assertFalse(r["acted"])
            self.assertEqual(r["tau"], 0.8)
            self.assertTrue(any("tau" in w for w in r["warnings"]))

    def test_custom_tau(self):
        self.fake.behaviour["*"] = lambda r: answer_for(r, noul=0.7)
        r = self.run_decide("retry_or_escalate", criteria_path=self.criteria_file("live", 0.6))
        self.assertTrue(r["acted"])


class TestShadowAndRouting(Base):
    def test_shadow_dual_call(self):
        self.fake.behaviour["nimble-v3"] = lambda r: answer_for(r, noul=0.9)
        self.fake.behaviour["nimble"] = lambda r: answer_for(r, noul=0.2)
        r = self.run_decide("retry_or_escalate")
        self.assertEqual(sorted(q["model"] for q in self.fake.requests), ["nimble", "nimble-v3"])
        self.assertIs(r["shadow_answer"], True)
        self.assertEqual(r["compare"]["model"], "nimble")
        self.assertIs(r["compare"]["answer"], False)
        self.assertEqual(self.recs[0]["compare"]["model"], "nimble")

    def test_compare_failure_keeps_primary(self):
        self.fake.behaviour["nimble-v3"] = lambda r: answer_for(r, noul=0.9)
        self.fake.behaviour["nimble"] = lambda r: (500, {})
        r = self.run_decide("retry_or_escalate")
        self.assertIs(r["shadow_answer"], True)
        self.assertEqual(r["compare"]["error"], "http_500")

    def test_compare_runs_concurrently_within_budget(self):
        def slow(r):
            time.sleep(0.5)
            return answer_for(r, noul=0.9)
        self.fake.behaviour["*"] = slow
        t0 = time.monotonic()
        r = self.run_decide("retry_or_escalate", timeout=2)
        self.assertLess(time.monotonic() - t0, 0.95)  # serial would be >= 1.0
        self.assertEqual(r["compare"]["answer"], True)

    def test_slow_compare_cannot_exceed_budget(self):
        self.fake.behaviour["nimble-v3"] = lambda r: answer_for(r, noul=0.9)
        self.fake.behaviour["nimble"] = lambda r: (time.sleep(1.5), answer_for(r, noul=0.9))[1]
        t0 = time.monotonic()
        r = self.run_decide("retry_or_escalate", timeout=0.4)
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertIs(r["shadow_answer"], True)
        self.assertIn("error", r["compare"])

    def test_live_makes_single_call(self):
        self.fake.behaviour["*"] = lambda r: answer_for(r, noul=0.9)
        self.run_decide("retry_or_escalate", criteria_path=self.criteria_file("live"))
        self.assertEqual(len(self.fake.requests), 1)

    def test_long_state_routes_to_nimble(self):
        self.fake.behaviour["*"] = lambda r: answer_for(r, noul=0.9)
        r = self.run_decide("retry_or_escalate", state="a" * (32_000 * 3 + 3))
        self.assertEqual(r["model"], "nimble")
        self.assertEqual({q["model"] for q in self.fake.requests}, {"nimble"})
        self.assertEqual(len(self.fake.requests), 1)  # no self-compare

    def test_state_at_limit_stays_on_v3(self):
        self.fake.behaviour["*"] = lambda r: answer_for(r, noul=0.9)
        r = self.run_decide("retry_or_escalate", state="a" * (32_000 * 3))
        self.assertEqual(r["model"], "nimble-v3")










@unittest.skipUnless(os.environ.get("DROVER_LIVE_OLLAMA") == "1", "needs live Ollama")
class TestLive(unittest.TestCase):
    def test_live_noul(self):
        r = decide.decide("lane_stuck", "pane tail: no output for 40 minutes; timer expired")
        self.assertIsNone(r["fallback"], r)


if __name__ == "__main__":
    unittest.main()
