import io
import json
import os
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

from drover import decide
from tests.test_decide import Base, Fake, answer_for, CHOICES


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
