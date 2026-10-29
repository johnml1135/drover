"""python -m drover init, with scripted input and injected detections (nothing real is read or written)."""
import io
import json
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from drover import config, placement, repos, wizard, wizard_detect
from drover.tool_discovery import DISCOVERY_SCRIPT

from tests.test_wizard import Base, DRIVES, REPOS, TOOLS, OLLAMA, fake, Script


class Detections(Base):
    def test_wsl_list_output_is_parsed_from_utf16(self):
        raw = "  NAME      STATE           VERSION\r\n* Ubuntu    Running         2\r\n  Debian    Stopped         2\r\n"
        got = wizard.placement.parse_distros(raw.encode("utf-16-le").decode("utf-16-le"))
        self.assertEqual([(d["name"], d["state"], d["default"], d["version"]) for d in got],
                         [("Ubuntu", "Running", True, 2), ("Debian", "Stopped", False, 2)])
        self.assertEqual(placement.decode_output(raw.encode("utf-16-le")).splitlines()[1].split()[1], "Ubuntu")

    def test_recommend_device(self):
        gpu = {"name": "g"}
        fast, slow = {"p50_ms": 10, "p95_ms": 50}, {"p50_ms": 500, "p95_ms": 3000}
        self.assertEqual(wizard.recommend_device(None, None, None)[0], "cpu")
        self.assertEqual(wizard.recommend_device(gpu, fast, slow)[0], "auto")
        self.assertEqual(wizard.recommend_device(gpu, fast, {"p50_ms": 20, "p95_ms": 60})[0], "cpu")
        self.assertEqual(wizard.recommend_device(gpu, None, None)[0], "auto")
        self.assertEqual(wizard.recommend_device(gpu, fast, {"p50_ms": 90, "p95_ms": 400})[0], "auto")   # 8x slower

    def test_levels_on_a_reference_machine_match_the_thresholds(self):
        """64 GB RAM, C: of 476 GB, R: of 931 GB (hosting the WSL disk): plan section 12's numbers, exactly."""
        ts, notes = wizard.scale_levels(64.0, [{"path": "C:\\", "free_gb": 160.0, "total_gb": 476.0},
                                              {"path": "R:\\", "free_gb": 500.0, "total_gb": 931.3}], 40.0, {"R"})
        t = {x["name"]: x for x in ts}
        self.assertEqual((t["ram"]["queue_base_gb"], t["ram"]["queue_per_worker_gb"], t["ram"]["hold_gb"]), (6.0, 0.5, 4.0))
        self.assertEqual((t["c"]["queue_gb"], t["c"]["hold_gb"]), (40.0, 25.0))
        self.assertEqual((t["r"]["queue_gb"], t["r"]["hold_gb"]), (100.0, 50.0))
        self.assertEqual((t["wsl-up"]["queue_mem_pct"], t["wsl-up"]["hold_mem_pct"]), (85.0, 95.0))
        self.assertTrue(any(".wslconfig" in n for n in notes))
        config.validate({"trackers": ts})

    def test_levels_scale_with_ram_and_drive_size(self):
        ts, _ = wizard.scale_levels(128.0, [{"path": "C:\\", "free_gb": 400.0, "total_gb": 952.0},
                                           {"path": "D:\\", "free_gb": 900.0, "total_gb": 1862.0}], None, set())
        t = {x["name"]: x for x in ts}
        self.assertEqual((t["ram"]["queue_base_gb"], t["ram"]["hold_gb"]), (12.0, 8.0))
        self.assertEqual((t["c"]["queue_gb"], t["c"]["hold_gb"]), (40.0, 25.0))
        self.assertEqual((t["d"]["queue_gb"], t["d"]["hold_gb"]), (100.0, 50.0))
        self.assertEqual(t["wsl-up"]["queue_mem_pct"], 0.0)                  # no .wslconfig cap known: memory check off

    def test_a_nearly_full_drive_is_warned_about_and_not_given_lower_levels(self):
        ts, notes = wizard.scale_levels(64.0, [{"path": "R:\\", "free_gb": 40.0, "total_gb": 931.0}], None, {"R"})
        h = next(x for x in ts if x["name"] == "r" and x.get("queue_gb"))
        self.assertEqual((h["queue_gb"], h["hold_gb"]), (100.0, 50.0))
        self.assertTrue(any(n.startswith("WARNING: R:\\ has only 40 GB free") for n in notes))

    def test_only_the_drives_drover_uses_are_tracked(self):
        drives = [{"path": x + ":\\", "free_gb": 500.0, "total_gb": 931.0} for x in "CGH"]
        ts, _ = wizard.scale_levels(64.0, drives, None, {"H"}, wanted={"C", "H"})
        names = [t["name"] for t in ts if t["kind"] == "disk"]
        self.assertEqual(sorted(names), ["c", "h"])
        s = {"state_dir": "D:\\drover", "repos_root": "E:\\repos", "worktrees": ""}
        self.assertEqual(wizard.wanted_drives(s, {"Ubuntu": "R:"}), {"C", "D", "E", "R"})
        self.assertEqual(wizard.wanted_drives({**s, "state_dir": "/home/x", "repos_root": "/mnt/r/repos"}, {}), {"C"})

    def test_a_full_unrelated_drive_does_not_get_a_tracker_in_the_wizard(self):
        drives = DRIVES + [{"path": "G:\\", "free_gb": 1.0, "total_gb": 900.0}]
        d = fake(drives=lambda: drives)
        wizard.run(d, Script(), self.out, state_dir=str(self.state), defaults=True)
        self.assertIn("tracked drives: C:, R:", self.out.getvalue())
        self.assertNotIn("G:\\ 900 GB", self.out.getvalue())

    def test_a_missing_h_drive_disables_the_default_h_tracker(self):
        ts, _ = wizard.scale_levels(32.0, [{"path": "C:\\", "free_gb": 100.0, "total_gb": 500.0}], None, set())
        cfg = config.validate({"trackers": ts})
        self.assertIs(next(t for t in cfg["trackers"] if t["name"] == "h")["enabled"], False)


    def test_real_detections_do_not_crash_on_this_host(self):
        d = wizard.real_detect()
        for name in ("drives", "distros", "config_dirs", "ram_gb", "cores", "vhdx_drives", "repo_candidates"):
            d[name]()



class Tools(Base):
    def tools(self, windows, wsl, probed=None):
        out = {"claude": {"windows": windows, "wsl": wsl}, "codex": {"windows": None, "wsl": "/usr/bin/codex"}}
        if probed is not None:
            out["_probed"] = probed
        return lambda distro=None: out

    def claude_seat(self, tools):
        self.run_wizard(fake(tools=tools), defaults=True)
        return next(s for s in self.config()["seats"] if s["name"] == "claude")

    def test_claude_runs_on_windows_when_found_there(self):
        self.assertEqual(self.claude_seat(self.tools("C:\\c\\claude.exe", "/usr/bin/claude"))["placement"], "windows")

    def test_claude_goes_to_wsl_only_when_windows_was_asked_and_has_none(self):
        both = {"windows": True, "wsl": True}
        self.assertEqual(self.claude_seat(self.tools(None, "/usr/bin/claude", both))["placement"], "wsl")
        self.assertIn("only in WSL", self.out.getvalue())

    def test_an_unprobed_windows_side_is_not_not_found(self):
        seat = self.claude_seat(self.tools(None, "/usr/bin/claude", {"windows": False, "wsl": True}))
        self.assertEqual(seat["placement"], "windows")
        self.assertIn("Windows not checked", self.out.getvalue())

    def test_wsl_probes_windows_through_cmd_exe_and_accepts_only_native_executables(self):
        from unittest import mock
        done = lambda out, rc=0: mock.Mock(stdout=out, returncode=rc)
        with mock.patch.object(wizard_detect, "is_windows", return_value=False), \
                mock.patch("pathlib.Path.exists", return_value=True), \
                mock.patch.object(wizard_detect, "_run", return_value=done(b"C:\\Users\\me\\claude.exe\r\nC:\\n\\claude.cmd\r\n")):
            self.assertEqual(wizard._windows_exe("claude"), ("C:\\Users\\me\\claude.exe", True))
        with mock.patch.object(wizard_detect, "is_windows", return_value=False), \
                mock.patch("pathlib.Path.exists", return_value=True), \
                mock.patch.object(wizard_detect, "_run", return_value=done(b"C:\\n\\claude.cmd\r\n")):
            self.assertEqual(wizard._windows_exe("claude"), (None, True))
        with mock.patch.object(wizard_detect, "is_windows", return_value=False), mock.patch("pathlib.Path.exists", return_value=False):
            self.assertEqual(wizard._windows_exe("claude"), (None, False))

    def test_wizard_reports_wsl_native_paths_from_the_shared_resolver(self):
        from unittest import mock
        native = "/opt/agents/bin/codex"
        claude = "/opt/agents/bin/claude"
        done = mock.Mock(stdout=f"DROVER_TOOL|codex|{native}\nDROVER_TOOL|claude|{claude}\n".encode(), returncode=0)
        with mock.patch.object(wizard_detect, "is_windows", return_value=True), \
                mock.patch.object(wizard_detect, "_windows_exe", return_value=(None, True)), \
                mock.patch.object(wizard.placement, "detect_distros", return_value=[
                    {"name": "Ubuntu", "state": "Running", "version": 2, "default": True}
                ]), \
                mock.patch.object(wizard_detect, "_run", return_value=done) as run:
            found = wizard.detect_tools("Ubuntu")
        self.assertEqual(found["codex"]["wsl"], native)
        self.assertEqual(found["claude"]["wsl"], claude)
        self.assertEqual(run.call_args.args[0],
                         ["wsl.exe", "-d", "Ubuntu", "--exec", "bash", "-c", DISCOVERY_SCRIPT])



class Benchmark(Base):
    def bench(self, device, in_vram):
        return {"ok": 10, "p50_ms": 100.0, "p95_ms": 120.0, **({"honoured": not in_vram} if device == "cpu" else {})}

    def test_a_cpu_run_that_stayed_in_vram_is_not_trusted(self):
        gpu = {"p50_ms": 100.0, "p95_ms": 120.0}
        cpu = {"p50_ms": 100.0, "p95_ms": 125.0, "honoured": False}
        rec, why = wizard.recommend_device({"name": "g"}, gpu, cpu)
        self.assertEqual(rec, "gpu")
        self.assertIn("CPU model variant stayed in VRAM", why)
        self.assertEqual(wizard.recommend_device({"name": "g"}, gpu, {**cpu, "honoured": True})[0], "cpu")

    def test_the_wizard_says_so_when_the_endpoint_ignored_the_option(self):
        self.run_wizard(fake(bench=lambda device, url=None, **kw: self.bench(device, True)), defaults=True)
        out = self.out.getvalue()
        self.assertIn("NOTE: the CPU model variant is still in VRAM", out)
        self.assertEqual(self.config()["decider"]["device"], "gpu")

    def test_a_warm_up_call_is_made_and_not_timed(self):
        from unittest import mock
        seen = []
        with mock.patch("drover.decide.bench", side_effect=lambda n, **kw: seen.append(n) or
                        {"n": n, "ok": n, "failed": 0, "p50_ms": 5.0, "p95_ms": 6.0}), \
                mock.patch.object(wizard_detect, "_model_in_vram", return_value=False):
            out = wizard.bench_decider("cpu", "http://x")
        self.assertEqual(seen, [1, wizard.BENCH_CALLS])
        self.assertEqual((out["p50_ms"], out["honoured"]), (5.0, True))

    def test_model_in_vram_reads_api_ps(self):
        from unittest import mock
        def ps(models):
            r = mock.MagicMock()
            r.__enter__.return_value.read.return_value = json.dumps({"models": models}).encode()
            r.__enter__.return_value.__iter__ = lambda s: iter(())
            return r
        with mock.patch("urllib.request.urlopen", return_value=ps([{"name": "nimble-v3-cpu:latest", "size": 9, "size_vram": 9}])):
            self.assertIs(wizard._model_in_vram("http://x"), True)
        with mock.patch("urllib.request.urlopen", return_value=ps([{"name": "nimble-v3-cpu:latest", "size": 9, "size_vram": 0}])):
            self.assertIs(wizard._model_in_vram("http://x"), False)
        with mock.patch("urllib.request.urlopen", return_value=ps([])):
            self.assertIsNone(wizard._model_in_vram("http://x"))

    def test_cpu_residency_ignores_unrelated_gpu_models(self):
        from unittest import mock
        body = {"models": [{"name": "acme-app:latest", "size_vram": 999},
                           {"name": "nimble-v3-cpu:latest", "size_vram": 0}]}
        with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(body).encode())):
            self.assertIs(wizard._model_in_vram("http://acme-lib"), False)

    def test_benchmark_uses_selected_models_and_restores_config(self):
        from unittest import mock
        previous = config.get()
        selected = {"primary": "acme-lib", "shadow": "acme-app", "long_state": "acme-app"}
        seen = []
        def bench(n, **kw):
            seen.append(dict(config.get()["decider"]["models"]))
            return {"ok": n, "p50_ms": 1, "p95_ms": 2}
        with mock.patch("drover.decide.bench", side_effect=bench):
            wizard.bench_decider("gpu", "http://acme-lib", models=selected)
        self.assertEqual(seen, [selected, selected])
        self.assertIs(config.get(), previous)



class CPUModels(Base):
    def test_cpu_and_auto_answers_ensure_selected_models_even_without_benchmark(self):
        for device in ("cpu", "auto"):
            calls = []
            self.run_wizard(fake(ensure_cpu_models=lambda models, base=None: calls.append((list(models), base)),
                                 bench=lambda *a, **kw: self.fail("explicit device skips benchmarking")),
                            defaults=True, answers={"decider_device": device, "decider_primary": "acme-lib",
                                                    "decider_shadow": "acme-app"})
            self.assertEqual(calls, [(["acme-lib", "acme-app", "nimble"], OLLAMA["url"])])

    def test_gpu_answer_does_not_provision_cpu_variants(self):
        self.run_wizard(fake(ensure_cpu_models=lambda *a, **kw: self.fail("GPU needs no CPU variants")),
                        defaults=True, answers={"decider_device": "gpu"})

    def test_provisioning_precedes_the_cpu_benchmark(self):
        calls = []
        def bench(device, url=None, **kw):
            calls.append(device)
            return {"ok": 10, "p50_ms": 1, "p95_ms": 2}
        self.run_wizard(fake(ensure_cpu_models=lambda *a, **kw: calls.append("ensure"), bench=bench), defaults=True)
        self.assertEqual(calls, ["gpu", "ensure", "cpu"])

    def test_offline_setup_names_the_ensure_command(self):
        self.run_wizard(fake(ollama=lambda: {**OLLAMA, "up": False},
                             ensure_cpu_models=lambda *a, **kw: self.fail("offline")), defaults=True,
                        answers={"decider_device": "cpu"})
        self.assertIn("python -m drover.decide ensure-cpu-models", self.out.getvalue())

    def test_create_failure_is_reported_and_cpu_benchmark_skipped(self):
        def fail(*a, **kw):
            raise OSError("model not found")
        devices = []
        self.run_wizard(fake(ensure_cpu_models=fail, bench=lambda device, *a, **kw: devices.append(device)), defaults=True)
        self.assertEqual(devices, ["gpu"])
        self.assertIn("Could not prepare CPU model variants", self.out.getvalue())
        self.assertIn("python -m drover.decide ensure-cpu-models", self.out.getvalue())



class ExampleRepos(Base):


    def test_the_note_about_unselected_examples_is_printed(self):
        self.run_wizard(defaults=True, answers={"repos": [{"name": "zzz", "repo": "R:\\z"}]})
        self.assertIn("Packaged examples require DROVER_EXAMPLES=1", self.out.getvalue())



class Vhdx(Base):
    def test_a_model_dir_on_the_vhdx_drive_is_warned_about(self):
        self.run_wizard(fake(ollama=lambda: {**OLLAMA, "models_dir": "R:\\ollama", "up": False}), defaults=True)
        self.assertIn("WARNING: R:\\ollama is on R:, the drive holding the WSL disk of Ubuntu", self.out.getvalue())

    def test_a_model_dir_elsewhere_is_not(self):
        self.run_wizard(defaults=True)
        self.assertNotIn("WARNING: C:\\ollama", self.out.getvalue())

    def test_a_low_free_state_dir_is_warned_about(self):
        self.run_wizard(fake(free_gb=lambda p: 3.0), defaults=True)
        self.assertIn("WARNING: only 3 GB free", self.out.getvalue())



class DecisionHistory(Base):
    def test_default_setup_commits_private_config_and_can_opt_out(self):
        self.run_wizard(defaults=True)
        self.assertTrue((self.state / '.git').is_dir())
        self.assertIn('ledger.db*', (self.state / '.gitignore').read_text())
        self.assertEqual(self.config()['config_sync'], {'remote': '', 'auto_push': True})
        self.assertEqual(len(list((self.state / 'decisions').glob('*.md'))), 1)
        other = self.state / 'other'
        wizard.run(fake(), out=io.StringIO(), answers={'state_dir': str(other), 'state_git': False,
                                                     'commercial_use': True}, defaults=True)
        self.assertFalse((other / '.git').exists())
        raw = tomllib.loads((other / 'config.toml').read_text())
        self.assertTrue(raw['decider']['commercial_use'])
