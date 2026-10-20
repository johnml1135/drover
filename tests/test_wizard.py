"""python -m drover init, with scripted input and injected detections (nothing real is read or written)."""
import io
import json
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from drover import config, placement, wizard
from drover.tool_discovery import DISCOVERY_SCRIPT

DRIVES = [{"path": "C:\\", "free_gb": 200.0, "total_gb": 500.0}, {"path": "R:\\", "free_gb": 600.0, "total_gb": 1000.0}]
REPOS = [{"name": "alpha", "path": "R:\\repos\\alpha"}, {"name": "beta", "path": "R:\\repos\\beta"}]
TOOLS = {"claude": {"windows": "C:\\bin\\claude.exe", "wsl": "/usr/bin/claude"},
         "codex": {"windows": None, "wsl": "/usr/bin/codex"}}
OLLAMA = {"url": "http://127.0.0.1:11434", "up": True, "models_dir": "C:\\ollama\\models", "env_set": True, "free_gb": 150.0}


def fake(**over):
    benches = {"gpu": {"ok": 10, "p50_ms": 40.0, "p95_ms": 90.0}, "cpu": {"ok": 10, "p50_ms": 400.0, "p95_ms": 3000.0}}
    d = {"windows": lambda: True, "home": lambda: Path("C:\\Users\\me"), "drives": lambda: DRIVES,
         "free_gb": lambda p: 100.0, "repo_candidates": lambda: ["R:\\repos", "D:\\none"],
         "repos": lambda root: REPOS if root == "R:\\repos" else [], "worktree_roots": lambda root: ["alpha.worktrees"],
         "vhdx_drives": lambda: {"Ubuntu": "R:"}, "nvidia": lambda: {"name": "RTX 3090", "used_mb": 1000.0, "total_mb": 24000.0},
         "ensure_cpu_models": lambda models, base=None: {"created": [], "existing": list(models)},
         "ollama": lambda: OLLAMA, "bench": lambda device, url=None, **kw: benches[device], "tools": lambda distro=None: TOOLS,
         "config_dirs": lambda: {}, "ram_gb": lambda: 64.0, "cores": lambda: 16, "wslconfig_cap_gb": lambda: 32.0,
         "distros": lambda: [{"name": "Ubuntu", "state": "Running", "version": 2, "default": True},
                             {"name": "Debian", "state": "Stopped", "version": 2, "default": False}],
         "tailscale": lambda: {"up": True, "login": "me@example.org"}, "gate_hint": lambda repo: None,
         "repo_kind": lambda repo: None, "exists": lambda p: True}
    d.update(over)
    return d


class Script:
    """input() that replays answers and records the prompts; an unscripted prompt is a test failure."""

    def __init__(self, *answers):
        self.answers, self.prompts = list(answers), []

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError("unexpected prompt: " + prompt)
        return self.answers.pop(0)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        self.out = io.StringIO()
        self.now = lambda: 1_800_000_000

    def run_wizard(self, detect=None, inputs=(), **kw):
        script = Script(*inputs)
        code = wizard.run(detect or fake(), script, self.out, state_dir=str(self.state), now=self.now, **kw)
        return code, script

    def config(self):
        return tomllib.loads((self.state / "config.toml").read_text(encoding="utf-8"))


class Defaults(Base):
    def test_defaults_writes_a_valid_config_from_the_detections(self):
        code, script = self.run_wizard(defaults=True)
        self.assertEqual((code, script.prompts), (0, []))
        raw = self.config()
        cfg = config.validate(raw)
        self.assertEqual(raw["paths"]["state_dir"], str(self.state))
        self.assertEqual(raw["paths"]["repos_root"], "R:\\repos")
        self.assertEqual(raw["paths"]["wsl_distro"], "Ubuntu")
        self.assertEqual([s["name"] for s in cfg["seats"]], ["claude", "codex"])
        self.assertEqual([s["director"] for s in cfg["seats"]], [True, False])
        self.assertEqual(cfg["seats"][0]["placement"], "windows")
        self.assertEqual(cfg["seats"][1]["placement"], "wsl")
        self.assertEqual(cfg["decider"]["device"], "auto")           # the GPU is clearly faster here
        self.assertEqual(cfg["decider"]["ollama_models"], "C:\\ollama\\models")
        self.assertTrue(cfg["hub"]["tailscale"])
        self.assertEqual((cfg["hub"]["ui_port"], cfg["hub"]["api_port"]), (8765, 8766))
        self.assertFalse((self.state / "repos").exists())          # no repo is set up by default

    def test_the_expected_toml_text(self):
        self.run_wizard(defaults=True)
        text = (self.state / "config.toml").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# Drover configuration, written by `python -m drover init`"))
        self.assertIn("[[seats]]\nname = \"claude\"\nbackend = \"claude\"", text)
        self.assertIn("repos_root = 'R:\\repos'", text)
        self.assertIn("[hub.auth]\nmode = \"tailscale\"", text)
        self.assertIn("[decider.models]\nprimary = \"nimble-v3\"\nshadow = \"nimble\"", text)

    def test_the_output_walks_the_nine_steps_in_order(self):
        self.run_wizard(defaults=True)
        out = self.out.getvalue()
        marks = ["1. State directory", "C:\\  200 GB free", "2. Repositories", "alpha.worktrees", "3. Ollama model directory",
                 "4. Decider", "GPU: RTX 3090", "p95 90 ms", "recommendation:", "5. Seats", "6. Resource tracker",
                 "7. WSL distro", "8. Hub", "oidc: coming later", "9. Repos", "Preview", "wrote "]
        pos = [out.index(m) for m in marks]
        self.assertEqual(pos, sorted(pos))

    def test_defaults_still_run_the_benchmark_when_ollama_answers(self):
        calls = []
        self.run_wizard(fake(bench=lambda device, url=None, **kw: calls.append(device) or {"ok": 10, "p50_ms": 1.0, "p95_ms": 2.0}),
                        defaults=True)
        self.assertEqual(calls, ["gpu", "cpu"])

    def test_no_ollama_no_benchmark(self):
        self.run_wizard(fake(ollama=lambda: {**OLLAMA, "up": False}, bench=lambda *a, **k: self.fail("no benchmark")),
                        defaults=True)
        self.assertIn("no benchmark was run", self.out.getvalue())
        self.assertEqual(self.config()["decider"]["device"], "auto")

    def test_no_gpu_means_cpu(self):
        self.run_wizard(fake(nvidia=lambda: None), defaults=True)
        self.assertEqual(self.config()["decider"]["device"], "cpu")
        self.assertIn("tev1", self.out.getvalue())

    def test_tailscale_off_when_not_running(self):
        self.run_wizard(fake(tailscale=lambda: {"up": False, "login": None}), defaults=True)
        self.assertIs(self.config()["hub"]["tailscale"], False)

    def test_wsl_only_machine_without_drives(self):
        self.run_wizard(fake(drives=lambda: [], ram_gb=lambda: None, distros=lambda: [], vhdx_drives=lambda: {},
                             wslconfig_cap_gb=lambda: None, ollama=lambda: {**OLLAMA, "up": False}), defaults=True)
        cfg = config.validate(self.config())
        self.assertEqual(cfg["paths"]["wsl_distro"], "Ubuntu")


class Questions(Base):

    def test_bad_answers_are_asked_again(self):
        out = io.StringIO()
        a = wizard.Asker(Script("abc", "0", "3"), out)
        self.assertEqual(a.ask("n", "Number", 1, wizard.to_int(1, 5)), 3)
        self.assertIn("a whole number, please", out.getvalue())
        self.assertIn("between 1 and 5, please", out.getvalue())

    def test_empty_input_takes_the_default_and_shows_it(self):
        s = Script("")
        a = wizard.Asker(s, io.StringIO())
        self.assertEqual(a.ask("k", "Ports", 8765, wizard.to_int()), 8765)
        self.assertEqual(s.prompts, ["Ports [8765]: "])

    def test_input_ending_early_is_a_clear_error(self):
        def eof(prompt=""):
            raise EOFError
        with self.assertRaisesRegex(wizard.WizardError, "use --defaults or --answers"):
            wizard.run(fake(), eof, self.out, state_dir=str(self.state))

    def test_declining_the_preview_writes_nothing(self):
        code, _ = self.run_wizard(defaults=True, answers={"confirm": False})
        self.assertEqual(code, 1)
        self.assertFalse(self.state.exists())
        self.assertIn("Nothing written", self.out.getvalue())

    def test_a_director_needs_a_claude_seat(self):
        d = fake(tools=lambda distro=None: {"claude": {"windows": None, "wsl": None}, "codex": TOOLS["codex"]})
        # state dir, repos root, worktrees, ollama dir, primary, shadow, device; then "add a claude seat anyway?" no
        inputs = [""] * 7 + ["n"] + [""] * 5
        with self.assertRaisesRegex(wizard.WizardError, "director must be a Claude seat"):
            wizard.run(d, Script(*inputs), self.out, state_dir=str(self.state))

    def test_a_missing_tool_asks_whether_to_add_the_seat_and_yes_is_the_default(self):
        d = fake(tools=lambda distro=None: {"claude": TOOLS["claude"], "codex": {"windows": None, "wsl": None}})
        inputs = [""] * 7 + [""] * 5 + ["n"] + [""] * 3      # claude seat questions; "add a codex seat anyway?" no; director...
        code = wizard.run(d, Script(*inputs[:12], "n", *([""] * 20)), self.out, state_dir=str(self.state))
        self.assertEqual(code, 0)
        self.assertEqual([s["name"] for s in config.validate(self.config())["seats"]], ["claude"])


class Answers(Base):

    def test_a_bad_answer_names_the_key(self):
        with self.assertRaisesRegex(wizard.WizardError, "answers file, decider_device: one of auto, gpu, cpu"):
            self.run_wizard(answers={"decider_device": "tpu"}, defaults=True)
        with self.assertRaisesRegex(wizard.WizardError, "answers file, ui_port"):
            self.run_wizard(answers={"ui_port": 0}, defaults=True)


    def test_main_reads_an_answers_file_and_the_defaults_flag(self):
        f = Path(self.tmp.name) / "a.toml"
        f.write_text("ui_port = 7777\n[[repos]]\nname = 'x'\nrepo = 'R:\\x'\n", encoding="utf-8")
        code = wizard.main(["--defaults", "--answers", str(f), "--state-dir", str(self.state)], detect=fake(), out=self.out)
        self.assertEqual(code, 0)
        self.assertEqual(self.config()["hub"]["ui_port"], 7777)
        self.assertTrue((self.state / "repos" / "x.toml").exists())

    def test_main_reports_errors_without_a_traceback(self):
        err = io.StringIO()
        import contextlib
        with contextlib.redirect_stderr(err):
            code = wizard.main(["--answers", str(Path(self.tmp.name) / "missing.toml")], detect=fake(), out=self.out)
        self.assertEqual(code, 2)
        self.assertIn("drover init:", err.getvalue())

    def test_python_dash_m_drover_dispatches(self):
        import contextlib
        from drover import __main__ as cli
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["nonsense"]), 2)
            self.assertEqual(cli.main(["--help"]), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["config", "--get", "hub.ui_port"]), 0)


class Writing(Base):
    def test_existing_codex_home_survives_init_defaults_and_serialization(self):
        account = '/home/acme/.codex-acme-lib'
        old = config.validate({'seats': [config.DEFAULT_SEATS[0],
                                       {**config.DEFAULT_SEATS[1], 'codex_home': account}]})
        self.state.mkdir()
        (self.state / 'config.toml').write_text(config.dumps(old), encoding='utf-8')
        detected = fake(config_dirs=lambda: {'CODEX_HOME': '/home/acme/.codex-acme-app'})
        for _ in range(2):
            self.assertEqual(self.run_wizard(detect=detected, defaults=True)[0], 0)
            raw = self.config()
            self.assertEqual(raw['seats'][1]['codex_home'], account)
            seat = config.validate(raw)['seats'][1]
            self.assertEqual(config.seat_env(seat), {'CODEX_HOME': account})
            self.assertEqual(seat['config_dir'], '')

    def test_an_existing_config_is_backed_up_not_lost(self):
        self.state.mkdir()
        (self.state / "config.toml").write_text("# old\n[hub]\nui_port = 1234\n")
        self.run_wizard(defaults=True)
        backups = sorted(self.state.glob("config.toml.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "# old\n[hub]\nui_port = 1234\n")
        self.run_wizard(defaults=True)                                 # a second run does not overwrite the first backup
        self.assertEqual(len(sorted(self.state.glob("config.toml.bak-*"))), 2)

    def test_a_rerun_prefills_from_the_existing_config(self):
        self.run_wizard(defaults=True, answers={"ui_port": 7001, "repos_root": "D:\\code", "wsl_distro": "Debian",
                                                "seats": [{"name": "main", "backend": "claude", "director": True,
                                                           "priority": 4}]})
        self.run_wizard(defaults=True)
        cfg = config.validate(self.config())
        self.assertEqual((cfg["hub"]["ui_port"], cfg["paths"]["repos_root"], cfg["paths"]["wsl_distro"]),
                         (7001, "D:\\code", "Debian"))
        self.assertEqual(cfg["seats"][0]["priority"], 4)

    def test_rerun_merges_answers_and_preserves_unprompted_settings_and_extra_entries(self):
        old = config.validate({
            "paths": {"state_dir": str(self.state)}, "capacity": {"parallel_cap": 1},
            "repos_dir": "R:\\acme-app\\repos", "policy": {"tau": 0.92},
            "models": {"efforts": {"mid": "max"}},
            "decider": {"url": "http://acme-lib:11434", "mode": "live", "timeout_s": 9,
                        "models": {"long_state": "large"}},
            "hub": {"api_listen_wsl": True, "allowed_logins": ["owner@acme.test", "helper@acme.test"]},
            "seats": [{**config.DEFAULT_SEATS[0], "director_reserve_pct": 0, "placement": "wsl"},
                      {**config.DEFAULT_SEATS[0], "name": "extra", "director": False,
                       "director_reserve_pct": 7, "binary": "claude-extra", "priority": 9},
                      config.DEFAULT_SEATS[1]],
            "trackers": [{"kind": "vram", "name": "vram", "fallback_pct": 66, "enabled": False},
                         {"kind": "ram", "name": "ram", "queue_per_worker_gb": 2},
                         {"kind": "disk", "name": "archive", "path": "D:\\acme-lib", "queue_gb": 20,
                          "hold_gb": 10, "enabled": False}]})
        self.state.mkdir()
        (self.state / "config.toml").write_text(config.dumps(old))
        self.run_wizard(defaults=True, answers={"ui_port": 7002, "seat_claude_priority": 3})
        got = config.validate(self.config())
        old["hub"]["ui_port"] = 7002
        old["seats"][0]["priority"] = 3
        self.assertEqual(got, old)

    def test_nothing_is_written_outside_the_state_dir(self):
        with self.assertRaisesRegex(wizard.WizardError, "outside the state dir"):
            wizard.write_file(self.state, self.state.parent / "evil.toml", "x", self.now)
        with self.assertRaisesRegex(wizard.WizardError, "outside the state dir"):
            wizard.write_file(self.state, self.state / ".." / "evil.toml", "x", self.now)
        wizard.write_file(self.state, self.state / "repos" / "ok.toml", "x", self.now)
        self.assertTrue((self.state / "repos" / "ok.toml").exists())

    def test_every_file_written_is_inside_the_state_dir(self):
        before = {p for p in Path(self.tmp.name).rglob("*")}
        self.run_wizard(defaults=True, answers={"repos": [{"name": "alpha", "repo": "R:\\a"}]})
        new = {p for p in Path(self.tmp.name).rglob("*")} - before
        self.assertTrue(new)
        for p in new:
            self.assertTrue(self.state in p.parents or p == self.state, p)

    def test_next_steps_print_commands_but_run_nothing(self):
        self.run_wizard(defaults=True)
        out = self.out.getvalue()
        self.assertIn("setx DROVER_HOME", out)
        self.assertIn("python -m drover config --show", out)


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
        self.assertEqual((t["c"]["queue_gb"], t["c"]["hold_gb"], t["c"]["wsl_source"]), (50.0, 25.0, "path"))
        self.assertEqual((t["r"]["queue_gb"], t["r"]["hold_gb"], t["r"]["wsl_source"]), (75.0, 25.0, "vhdx"))
        self.assertEqual((t["wsl-up"]["queue_mem_pct"], t["wsl-up"]["hold_mem_pct"]), (85.0, 95.0))
        self.assertTrue(any(".wslconfig" in n for n in notes))
        config.validate({"trackers": ts})

    def test_levels_scale_with_ram_and_drive_size(self):
        ts, _ = wizard.scale_levels(128.0, [{"path": "C:\\", "free_gb": 400.0, "total_gb": 952.0},
                                           {"path": "D:\\", "free_gb": 900.0, "total_gb": 1862.0}], None, set())
        t = {x["name"]: x for x in ts}
        self.assertEqual((t["ram"]["queue_base_gb"], t["ram"]["hold_gb"]), (12.0, 8.0))
        self.assertEqual((t["c"]["queue_gb"], t["c"]["hold_gb"]), (100.0, 50.0))
        self.assertEqual((t["d"]["queue_gb"], t["d"]["hold_gb"]), (150.0, 50.0))
        self.assertEqual(t["wsl-up"]["queue_mem_pct"], 0.0)                  # no .wslconfig cap known: memory check off

    def test_a_nearly_full_drive_is_warned_about_and_not_given_lower_levels(self):
        ts, notes = wizard.scale_levels(64.0, [{"path": "R:\\", "free_gb": 40.0, "total_gb": 931.0}], None, {"R"})
        h = next(x for x in ts if x["name"] == "r" and x.get("queue_gb"))
        self.assertEqual((h["queue_gb"], h["hold_gb"]), (75.0, 25.0))
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
        with mock.patch.object(wizard, "is_windows", return_value=False), \
                mock.patch("pathlib.Path.exists", return_value=True), \
                mock.patch.object(wizard, "_run", return_value=done(b"C:\\Users\\me\\claude.exe\r\nC:\\n\\claude.cmd\r\n")):
            self.assertEqual(wizard._windows_exe("claude"), ("C:\\Users\\me\\claude.exe", True))
        with mock.patch.object(wizard, "is_windows", return_value=False), \
                mock.patch("pathlib.Path.exists", return_value=True), \
                mock.patch.object(wizard, "_run", return_value=done(b"C:\\n\\claude.cmd\r\n")):
            self.assertEqual(wizard._windows_exe("claude"), (None, True))
        with mock.patch.object(wizard, "is_windows", return_value=False), mock.patch("pathlib.Path.exists", return_value=False):
            self.assertEqual(wizard._windows_exe("claude"), (None, False))

    def test_wizard_reports_wsl_native_paths_from_the_shared_resolver(self):
        from unittest import mock
        native = "/opt/agents/bin/codex"
        claude = "/opt/agents/bin/claude"
        done = mock.Mock(stdout=f"DROVER_TOOL|codex|{native}\nDROVER_TOOL|claude|{claude}\n".encode(), returncode=0)
        with mock.patch.object(wizard, "is_windows", return_value=True), \
                mock.patch.object(wizard, "_windows_exe", return_value=(None, True)), \
                mock.patch.object(wizard.placement, "detect_distros", return_value=[
                    {"name": "Ubuntu", "state": "Running", "version": 2, "default": True}
                ]), \
                mock.patch.object(wizard, "_run", return_value=done) as run:
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
                mock.patch.object(wizard, "_model_in_vram", return_value=False):
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


if __name__ == "__main__":
    unittest.main()


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
