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


class TestCPU(unittest.TestCase):
    """Exercise real decision parsing and HTTP serialization without binding local sockets."""

    def setUp(self):
        from drover import config
        config.use(config.validate({"decider": {"device": "cpu", "mode": "live"}}))
        self.addCleanup(config.reset)
        self.requests = []

    def request(self, answer, **kw):
        name = kw.pop("name", "retry_or_escalate")

        def respond(req, timeout):
            payload = json.loads(req.data)
            self.requests.append((req.full_url, payload))
            body = {"answers": {name: answer}}
            return io.BytesIO(json.dumps(body).encode())

        with mock.patch("urllib.request.urlopen", side_effect=respond):
            return decide.decide(name, "task state", base="http://acme-lib:11434/", recorder=lambda r: None, **kw)

    def test_cpu_uses_systemone_and_parses_each_decision_kind(self):
        cases = [("retry_or_escalate", {"noul": 0.9}, True),
                 ("gate_failure_class", {"choice": "syntax", "confidence": 0.95}, "syntax"),
                 ("task_size", {"score": 1.0, "confidence": 0.9}, "M")]
        for name, fields, answer in cases:
            with self.subTest(name=name):
                result = self.request(fields, name=name)
                self.assertTrue(result["acted"], result)
                self.assertEqual(result["answer"], answer)
                url, payload = self.requests[-1]
                self.assertEqual(url, "http://acme-lib:11434/v1/systemone")
                self.assertEqual(set(payload), {"model", "state", "questions"})
                self.assertEqual(payload["model"], "nimble-v3-cpu")
                self.assertEqual(payload["state"], "task state")
                self.assertIn(name, payload["questions"])

    def test_auto_uses_cpu_variant_only_above_vram_threshold(self):
        for vram, model in ((80.0, "nimble-v3"), (80.1, "nimble-v3-cpu")):
            result = self.request({"noul": 0.9}, device="auto", vram=vram)
            self.assertTrue(result["acted"], result)
            self.assertTrue(self.requests[-1][0].endswith("/v1/systemone"))
            self.assertEqual(self.requests[-1][1]["model"], model)

    def test_cpu_shadow_compare_also_uses_cpu_variants(self):
        from drover import config
        config.use(config.validate({"decider": {"device": "cpu", "mode": "shadow"}}))
        result = self.request({"noul": 0.9})
        self.assertFalse(result["acted"])
        self.assertIs(result["shadow_answer"], True)
        self.assertIs(result["compare"]["answer"], True)
        self.assertEqual({p["model"] for _, p in self.requests}, {"nimble-cpu", "nimble-v3-cpu"})
        self.assertTrue(all(url.endswith("/v1/systemone") for url, _ in self.requests))

    def test_gpu_keeps_systemone_without_cpu_options(self):
        result = self.request({"noul": 0.9}, device="gpu", vram=99)
        self.assertTrue(result["acted"])
        url, payload = self.requests[-1]
        self.assertTrue(url.endswith("/v1/systemone"))
        self.assertNotIn("options", payload)

    def test_missing_cpu_variant_reports_ensure_command_without_creating(self):
        import urllib.error
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError(
                "http://acme-lib/v1/systemone", 404, "Not Found", {}, io.BytesIO(b'{"error":"model missing"}'))) as http:
            result = decide.decide("retry_or_escalate", "x", base="http://acme-lib", recorder=lambda r: None)
        self.assertIn("nimble-v3-cpu", result["fallback"])
        self.assertIn("python -m drover.decide ensure-cpu-models", result["fallback"])
        self.assertEqual(http.call_count, 1)
        self.assertTrue(http.call_args.args[0].full_url.endswith("/v1/systemone"))

    def test_cpu_long_state_uses_long_state_variant_without_self_compare(self):
        from drover import config
        config.use(config.validate({"decider": {"device": "cpu", "mode": "shadow"}}))
        seen = []
        def respond(req, timeout):
            seen.append(json.loads(req.data))
            return io.BytesIO(json.dumps({"answers": {"retry_or_escalate": {"noul": 0.9}}}).encode())
        with mock.patch("urllib.request.urlopen", side_effect=respond):
            result = decide.decide("retry_or_escalate", "a" * 96003, base="http://acme-lib", recorder=lambda r: None)
        self.assertEqual(result["model"], "nimble-cpu")
        self.assertEqual([r["model"] for r in seen], ["nimble-cpu"])


class TestEnsureCPU(unittest.TestCase):
    def setUp(self):
        from drover import config
        config.use(config.validate({"decider": {"models": {"primary": "acme-lib", "shadow": "acme-app",
                                                           "long_state": "acme-app"}}}))
        self.addCleanup(config.reset)
        self.models = ["acme-lib-cpu:latest"]
        self.requests = []
        self.http = mock.patch("urllib.request.urlopen", side_effect=self.respond)
        self.http.start()
        self.addCleanup(self.http.stop)

    def respond(self, req, timeout):
        if isinstance(req, str):
            self.requests.append((req, None))
            return io.BytesIO(json.dumps({"models": [{"name": m} for m in self.models]}).encode())
        payload = json.loads(req.data)
        self.requests.append((req.full_url, payload))
        self.models.append(payload["model"] + ":latest")
        return io.BytesIO(b'{"status":"success"}')

    def test_creates_only_missing_variants_and_is_idempotent(self):
        out = decide.ensure_cpu_models(base="http://acme-lib:11434/")
        self.assertEqual(out, {"created": ["acme-app-cpu"], "existing": ["acme-lib-cpu"]})
        self.assertEqual(self.requests, [("http://acme-lib:11434/api/tags", None),
                         ("http://acme-lib:11434/api/create", {"model": "acme-app-cpu", "from": "acme-app",
                          "parameters": {"num_gpu": 0}, "stream": False})])
        self.requests.clear()
        self.assertEqual(decide.ensure_cpu_models(base="http://acme-lib")["created"], [])
        self.assertEqual(self.requests, [("http://acme-lib/api/tags", None)])

    def test_explicit_tag_names_and_duplicate_models(self):
        out = decide.ensure_cpu_models(["acme-lib:small", "acme-lib:small"], base="http://acme-lib")
        self.assertEqual(out["created"], ["acme-lib:small-cpu"])
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[-1][1]["from"], "acme-lib:small")

    def test_cli_json_and_url(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = decide.main(["ensure-cpu-models", "--url", "http://acme-lib", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["created"], ["acme-app-cpu"])
        self.assertTrue(all(url.startswith("http://acme-lib/") for url, _ in self.requests))

    def test_cli_reports_create_failure(self):
        import urllib.error
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = decide.main(["ensure-cpu-models", "--json"])
        self.assertEqual(code, 1)
        self.assertIn("ensure-cpu-models failed", json.loads(out.getvalue())["error"])


class TestDevice(Base):
    """Device selection changes model names and preserves the decision request."""

    def use(self, **decider):
        from drover import config
        raw = {"decider": decider}
        config.use(config.validate(raw))
        self.addCleanup(config.reset)

    def models(self, **kw):
        self.fake.behaviour["*"] = lambda r: answer_for(r, noul=0.9)
        self.run_decide("retry_or_escalate", criteria_path=self.criteria_file("live"), **kw)
        return [r["model"] for r in self.fake.requests]

    def test_cpu_selects_variant(self):
        self.use(device="cpu")
        self.assertEqual(self.models(), ["nimble-v3-cpu"])

    def test_cpu_applies_to_the_shadow_compare_call_too(self):
        self.use(device="cpu")
        self.fake.behaviour["*"] = lambda r: answer_for(r, noul=0.9)
        r = self.run_decide("retry_or_escalate")
        self.assertEqual(sorted(q["model"] for q in self.fake.requests), ["nimble-cpu", "nimble-v3-cpu"])
        self.assertTrue(all("options" not in q for q in self.fake.requests))
        self.assertEqual(r["device"], "cpu")

    def test_gpu_sends_no_options(self):
        self.use(device="gpu")
        self.assertEqual(self.models(vram=99.0), ["nimble-v3"])

    def test_auto_is_cpu_only_when_vram_is_over_the_fallback(self):
        self.use(device="auto")
        self.assertEqual(self.models(vram=80.0), ["nimble-v3"])
        self.fake.requests.clear()
        self.assertEqual(self.models(vram=80.1), ["nimble-v3-cpu"])
        self.fake.requests.clear()
        self.assertEqual(self.models(vram=lambda: None), ["nimble-v3"])   # unknown VRAM never forces the CPU

    def test_auto_follows_the_vram_trackers_fallback_pct(self):
        self.use(device="auto")
        from drover import config
        config.use(config.validate({"decider": {"device": "auto"},
                                    "trackers": [{"kind": "vram", "name": "vram", "fallback_pct": 50}]}))
        self.assertEqual(decide.placement(vram=60), "cpu")
        self.assertEqual(decide.placement(vram=40), "gpu")

    def test_vram_reading_is_cached(self):
        calls = []
        decide._vram.update(at=None, value=None)
        t = [0.0]
        read = lambda: calls.append(1) or [50, 100]
        self.assertEqual(decide.vram_percent(read, now=lambda: t[0]), 50.0)
        self.assertEqual(decide.vram_percent(None, now=lambda: t[0] + 5), 50.0)
        self.assertEqual(len(calls), 1)
        decide._vram.update(at=None, value=None)

    def test_config_models_and_url(self):
        self.use(models={"primary": "tev1", "shadow": "nimble-v3", "long_state": "big"}, url="http://h:9", mode="live")
        q = decide.get_question("retry_or_escalate")
        self.assertEqual((q["model"], q["shadow_compare_model"], q["mode"]), ("tev1", "nimble-v3", "live"))
        self.assertEqual(decide.pick_model(q, "a" * (32_000 * 3 + 3)), "big")
        self.assertEqual(decide.base_url({}), "http://h:9")
        self.assertEqual(decide.base_url({"DROVER_OLLAMA_URL": "http://e:1"}), "http://e:1")

    def test_defaults_leave_the_criteria_file_alone(self):
        for mode, model in (("live", "nimble-v3"),):
            q = decide.get_question("task_size", self.criteria_file(mode))
            self.assertEqual((q["mode"], q["model"]), (mode, model))


class TestRecorderAndBase(Base):
    def test_default_recorder_writes_jsonl(self):
        log = Path(self.tmp.name) / "d.jsonl"
        self.fake.behaviour["*"] = lambda r: answer_for(r, noul=0.9)
        old = os.environ.get("DROVER_DECISIONS_LOG")
        os.environ["DROVER_DECISIONS_LOG"] = str(log)
        self.addCleanup(lambda: os.environ.pop("DROVER_DECISIONS_LOG", None) if old is None
                        else os.environ.__setitem__("DROVER_DECISIONS_LOG", old))
        decide.decide("retry_or_escalate", "x", base=self.fake.url, timeout=2)
        rec = json.loads(log.read_text().splitlines()[0])
        self.assertEqual(rec["name"], "retry_or_escalate")
        self.assertEqual(rec["criteria_version"], 1)
        self.assertIn("state_hash", rec)

    def test_base_url(self):
        self.assertEqual(decide.base_url({"DROVER_OLLAMA_URL": "http://h:1/"}), "http://h:1")
        route = lambda: "default via 172.22.208.1 dev eth0 proto kernel onlink\n"
        import platform
        if platform.system() != "Windows":
            self.assertEqual(decide.base_url({}, is_wsl=True, route=route), "http://172.22.208.1:11434")
            self.assertEqual(decide.base_url({}, is_wsl=False), "http://127.0.0.1:11434")

    @unittest.skipIf(os.name == "nt", "gateway lookup is WSL-only")
    def test_wsl_candidates_gateway_then_localhost(self):
        route = lambda: "default via 172.22.208.1 dev eth0\n"
        self.assertEqual(decide.base_candidates({}, is_wsl=True, route=route),
                         ["http://172.22.208.1:11434", "http://127.0.0.1:11434"])
        self.assertEqual(decide.base_candidates({"DROVER_OLLAMA_URL": "http://h:1"}), ["http://h:1"])

    def test_localhost_retry_after_gateway_fails(self):
        self.fake.behaviour["*"] = lambda r: answer_for(r, noul=0.9)
        key = "test-key"
        decide._bases.clear()
        old = os.environ.get("DROVER_OLLAMA_URL")
        os.environ["DROVER_OLLAMA_URL"] = key
        self.addCleanup(lambda: (os.environ.pop("DROVER_OLLAMA_URL", None) if old is None
                                 else os.environ.__setitem__("DROVER_OLLAMA_URL", old),
                                 decide._bases.clear()))
        decide._bases[key] = ["http://127.0.0.1:9", self.fake.url]
        r = decide.decide("retry_or_escalate", "x", timeout=6, recorder=self.recs.append)  # Windows refuses slowly
        self.assertIs(r["shadow_answer"], True)
        self.assertEqual(decide._bases[key], [self.fake.url])  # remembered

    def test_base_cached_per_process(self):
        decide._bases.clear()
        calls = []
        orig = decide.base_candidates
        decide.base_candidates = lambda *a: calls.append(1) or ["http://x:1"]
        self.addCleanup(lambda: (setattr(decide, "base_candidates", orig), decide._bases.clear()))
        decide.cached_bases(); decide.cached_bases()
        self.assertEqual(len(calls), 1)

    def test_cli_and_bench(self):
        self.fake.behaviour["*"] = lambda r: answer_for(r, noul=0.9)
        sf = Path(self.tmp.name) / "s.txt"
        sf.write_text("state")
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = decide.main(["retry_or_escalate", "--state-file", str(sf), "--json", "--url", self.fake.url])
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(buf.getvalue())["shadow_answer"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = decide.main(["bench", "--n", "5", "--url", self.fake.url, "--json"])
        out = json.loads(buf.getvalue())
        self.assertEqual((rc, out["n"], out["ok"]), (0, 5, 5))
        self.assertIsNotNone(out["p95_ms"])


@unittest.skipUnless(os.environ.get("DROVER_LIVE_OLLAMA") == "1", "needs live Ollama")
class TestLive(unittest.TestCase):
    def test_live_noul(self):
        r = decide.decide("lane_stuck", "pane tail: no output for 40 minutes; timer expired")
        self.assertIsNone(r["fallback"], r)


if __name__ == "__main__":
    unittest.main()
