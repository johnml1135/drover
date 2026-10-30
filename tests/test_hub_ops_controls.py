"""Owner-UI operations: pause toggle, garden executor, tuner scheduler, WSL discovery, run tail, import filters,
install scripts. TestClient plus a temp ledger; machine facts are injected."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

try:
    from fastapi.testclient import TestClient
    from hub.app import create_app
    from hub.services import DISCOVERY_SCRIPT, Services, parse_discovery, valid_tool_path
    from hub.settings import Settings
except ImportError as exc:  # hub deps are third-party; skip when absent
    raise unittest.SkipTest(f"hub deps missing: {exc}")

from drover import importer, tuner
from drover.ledger import ops
from tests.test_importer import QUESTIONS, sample


from tests.test_hub_ops import SAME, TOKEN, SCRIPTS, FakeWsl, FakeTuner, Base


class TestTunerScheduler(Base):
    def test_runs_only_when_due_and_at_most_daily(self):
        self.assertIsNone(self.svc.check_tuner())
        self.assertEqual(self.tuner.passes, 0)
        self.tuner.due = True
        self.assertIsNone(self.svc.check_tuner())   # checked a moment ago
        self.now[0] += 86401
        self.assertEqual(self.svc.check_tuner()["version"], "p2")
        self.assertEqual(self.tuner.passes, 1)
        self.assertEqual(len(self.store.list_events(kind="tuner.pass")), 1)

    def test_real_tuner_with_too_few_runs_is_not_due(self):
        self.svc.tuner = tuner
        self.assertIsNone(self.svc.check_tuner())
        self.assertEqual(self.store.list_events(kind="tuner.pass"), [])

    def test_tuner_questions_render_as_proposals_once(self):
        tuner.proposals(self.store.conn, {'retry_or_escalate': {
            'version': 'v1', 'n': 100, 'model_acc': .91, 'rule_acc': .8}}, {})
        html = self.c.get("/inbox").text
        self.assertIn("Tuner proposals", html)
        self.assertEqual(html.count("Promote retry_or_escalate (criteria v1) from shadow to live?"), 1)
        self.assertIn("Promote to live", html)
        qid = self.store.list_questions(open_only=True)[0]["id"]
        self.c.post(f"/questions/{qid}/answer", data={"option": "Keep shadow"}, headers=SAME)
        self.assertNotIn("Promote retry_or_escalate (criteria v1) from shadow to live?", self.c.get("/inbox").text)


class TestWslDiscovery(Base):
    def test_validation(self):
        ok = ["/usr/bin/claude", "/home/u/.local/bin/claude", "/opt/tools/bin/codex"]
        for p in ok:
            self.assertEqual(valid_tool_path(p, p.rsplit("/", 1)[1]), p)
        for p, tool in [("claude", "claude"), ("./claude", "claude"), ("/usr/../bin/claude", "claude"),
                        ("/usr/bin/evil", "claude"), ("/usr/bin/claude;rm -rf /", "claude"),
                        ("/usr/bin/claude extra", "claude"), ("/usr//bin/claude", "claude"),
                        ("alias claude='x'", "claude"), ("/usr/bin/$(id)/claude", "claude"), ("/usr/bin/", "claude"),
                        ("/usr/bin/codex", "claude"), ("/mnt/c/Users/x/bin/claude", "claude"), ("", "claude"), ("C:\\bin\\claude", "claude")]:
            self.assertIsNone(valid_tool_path(p, tool), p)

    def test_parse_ignores_noise_and_takes_the_first_valid(self):
        out = ("Welcome to Ubuntu\nDROVER_TOOL|claude|/usr/bin/claude\n"
               "claude is aliased\nDROVER_TOOL|claude|/opt/claude\nDROVER_TOOL|codex|/usr/local/bin/codex\n")
        self.assertEqual(parse_discovery(out), {"claude": "/usr/bin/claude", "codex": "/usr/local/bin/codex"})
        self.assertEqual(parse_discovery("DROVER_TOOL|codex|/usr/bin/codex\n"), {"codex": "/usr/bin/codex"})

    def test_discover_runs_the_fixed_command_and_stores_settings(self):
        found = self.svc.discover_tools()
        self.assertEqual(self.wsl.calls[-1], ["-d", "Ubuntu", "--exec", "bash", "-c", DISCOVERY_SCRIPT])
        s = self.store.get_settings()
        self.assertEqual((s["wsl_bin_claude"], s["wsl_bin_codex"]), ("/home/u/.local/bin/claude", "/usr/bin/codex"))
        self.assertEqual(found, {"claude": s["wsl_bin_claude"], "codex": s["wsl_bin_codex"]})
        health = self.c.get("/health").text
        self.assertIn("/home/u/.local/bin/claude", health)

    def test_bad_output_stores_nothing_and_failure_keeps_old_values(self):
        self.wsl.discovery = "claude\n/bin/sh -c evil\n"
        self.assertEqual(self.svc.discover_tools(), {})
        self.assertNotIn("wsl_bin_claude", self.store.get_settings())
        self.svc.discover_tools.__self__.wsl.discovery = "DROVER_TOOL|claude|/usr/bin/claude\n"
        self.svc.discover_tools()
        self.wsl.rc, self.wsl.discovery = 1, "/usr/bin/evil/claude\n"
        self.svc.discover_tools()
        self.assertEqual(self.store.get_settings()["wsl_bin_claude"], "/usr/bin/claude")


    def test_discovery_when_wsl_first_comes_up(self):
        self.wsl.running = ""
        self.svc.check_wsl()
        self.assertEqual([c for c in self.wsl.calls if c[:1] == ["-d"]], [])
        self.wsl.running = "Ubuntu (Default)"
        self.svc.check_wsl()
        self.assertEqual(len([c for c in self.wsl.calls if c[:1] == ["-d"]]), 1)
        self.svc.check_wsl()  # already up and both tools known: no repeat
        self.assertEqual(len([c for c in self.wsl.calls if c[:1] == ["-d"]]), 1)

    def test_wsl_keepalive_and_dispatcher_state_on_health(self):
        html = self.c.get("/health").text
        for text in ("WSL keepalive", "idle", "paused", "reachable", "61.5", "90.0", "No bench data"):
            self.assertIn(text, html)
        self.store.update_settings({"ollama_bench": json.dumps({"p95_ms": 1420})})
        self.assertIn("1420", self.c.get("/health").text)


class TestRunTail(Base):
    def make_run(self, finish=False):
        t = self.store.create_task("Tail me", scope_files=['fixture-115'])["id"]
        rid = self.store.start_run(t, "worker", "claude", "haiku", "high")["id"]
        if finish:
            self.store.finish_run(rid, "landed", allowance_used={"seven_day_pct": 1.5, "five_hour_pct": 4})
        return rid

    def log(self, rid, text):
        d = self.s.runs_dir / str(rid)
        d.mkdir(parents=True)
        (d / "out.log").write_bytes(text if isinstance(text, bytes) else text.encode())

    def test_tail_is_escaped_and_live_runs_poll(self):
        rid = self.make_run()
        self.log(rid, '<script>alert(1)</script> & <img src=x onerror=y>\n')
        html = self.c.get(f"/runs/{rid}").text
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt; &amp; &lt;img", html)
        self.assertNotIn("<script>alert(1)", html)
        self.assertNotIn("<img src=x", html)
        self.assertIn(f'hx-get="/runs/{rid}/tail"', html)
        frag = self.c.get(f"/runs/{rid}/tail").text
        self.assertIn("&lt;script&gt;", frag)
        self.assertNotIn("<html", frag)

    def test_finished_run_does_not_poll_and_binary_is_safe(self):
        rid = self.make_run(finish=True)
        self.log(rid, b"ok \xff\xfe bytes\n" + b"x" * 100)
        html = self.c.get(f"/runs/{rid}").text
        self.assertNotIn("every 3s", html)
        self.assertIn("ok", html)

    def test_big_log_shows_only_the_end(self):
        rid = self.make_run()
        self.log(rid, "START\n" + "line\n" * 40000 + "THE END\n")
        html = self.c.get(f"/runs/{rid}").text
        self.assertIn("THE END", html)
        self.assertNotIn("START", html)
        self.assertIn("earlier output not shown", html)

    def test_missing_log_and_bad_ids(self):
        rid = self.make_run()
        self.assertIn("no output yet", self.c.get(f"/runs/{rid}").text)
        for bad in ("999", "abc", "..%2f..%2fsecret", "1.5", "-1", "0", "%EF%BC%91"):
            self.assertEqual(self.c.get(f"/runs/{bad}").status_code, 404, bad)
            self.assertEqual(self.c.get(f"/runs/{bad}/tail").status_code, 404, bad)

    def test_runs_tab_formats_allowance_and_links_detail(self):
        rid = self.make_run(finish=True)
        html = self.c.get("/runs").text
        self.assertIn("1.5% week, 4% 5h", html)
        self.assertIn(f'href="/runs/{rid}"', html)


class TestImportControls(Base):
    def setUp(self):
        super().setUp()
        self.do_import()

    def by_title(self):
        return {t["title"]: t for t in self.store.list_tasks()}

    def test_low_confidence_chip_filters_inbox_and_plan(self):
        html = self.c.get("/inbox").text
        self.assertIn("low confidence (2)", html)
        self.assertIn("task 1", html)
        low = self.c.get("/inbox?low=1").text
        self.assertIn("task 2", low)
        self.assertNotIn("task 1<", low)
        self.assertNotIn("task 3", low)
        plan = self.c.get("/plan?low=1").text
        self.assertIn("task 2", plan)
        self.assertNotIn("task 1<", plan)
        self.assertNotIn("task 3", plan)
        self.assertIn("low confidence", self.c.get("/plan").text)

    def test_approve_all_in_epic_is_owner_only_and_scoped(self):
        epics = {e["title"]: e["id"] for e in self.store.list_epics()}
        eid = epics["acme-lib: one"]
        self.assertEqual({t["status"] for t in self.store.list_tasks()}, {"proposed"})
        r = self.c.post(f"/api/v1/epics/{eid}/approve-proposed", headers=TOKEN)
        self.assertEqual(r.status_code, 403)
        r = self.c.post("/imports/approve-epic", data={"epic_id": eid})  # no same-origin proof
        self.assertEqual(r.status_code, 403)
        self.assertEqual({t["status"] for t in self.store.list_tasks()}, {"proposed"})
        html = self.c.get("/inbox").text
        self.assertIn("Approve all in epic", html)
        r = self.c.post("/imports/approve-epic", data={"epic_id": eid}, headers=SAME)
        self.assertEqual(r.status_code, 200)
        t = self.by_title()
        self.assertEqual([t[n]["status"] for n in ("task 1", "task 2")], ["ready", "ready"])
        self.assertTrue(all(t[n]["approved_at"] for n in ("task 1", "task 2")))
        self.assertEqual(t["task 3"]["status"], "proposed")
        self.assertIsNone(t["task 3"]["approved_at"])
        self.assertEqual(self.c.post("/imports/approve-epic", data={}, headers=SAME).status_code, 422)
        self.assertEqual(self.c.post("/imports/approve-epic", data={"epic_id": "E-nope"}, headers=SAME).status_code, 404)

    def test_per_task_and_bulk_controls_remain(self):
        html = self.c.get("/inbox").text
        self.assertIn("Approve selected", html)
        self.assertIn("/items/import/", html)


def powershell():
    for name in ("powershell.exe", "powershell", "pwsh"):
        p = shutil.which(name)
        if p:
            return p
    p = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    return p if os.path.exists(p) else None


def win_path(p: Path) -> str:
    if sys.platform == "win32":
        return str(p)
    out = subprocess.run(["wslpath", "-w", str(p)], capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else str(p)


@unittest.skipUnless(sys.platform == "win32" and powershell(), "needs Windows with PowerShell")
class TestScripts(unittest.TestCase):
    def test_every_script_parses(self):
        scripts = sorted(SCRIPTS.glob("*.ps1"))
        self.assertTrue({"Install-Drover.ps1", "Add-OllamaFirewallRule.ps1"} <= {s.name for s in scripts})
        for s in scripts:
            cmd = ("$e=$null;$t=$null;[void][System.Management.Automation.Language.Parser]::ParseFile("
                   f"'{win_path(s)}',[ref]$t,[ref]$e);if($e){{$e|ForEach-Object{{$_.Message}};exit 1}}")
            r = subprocess.run([powershell(), "-NoProfile", "-Command", cmd], capture_output=True, text=True,
                               timeout=120)
            self.assertEqual(r.returncode, 0, f"{s.name}: {r.stdout}{r.stderr}")

    def test_install_script_lists_its_steps_in_order(self):
        text = (SCRIPTS / "Install-Drover.ps1").read_text(encoding="utf-8")
        order = ["the configuration", "state directory", "hub token", "uv sync", "scheduled task", "tailscale serve"]
        pos = [text.lower().index(o.lower()) for o in order]
        self.assertEqual(pos, sorted(pos))
        self.assertIn("SupportsShouldProcess", text)
        self.assertNotIn("C:\\drover", text)

    def test_install_script_runs_the_wizard_only_when_config_is_missing_and_not_on_whatif(self):
        text = (SCRIPTS / "Install-Drover.ps1").read_text(encoding="utf-8")
        self.assertIn("python -m drover", text)
        self.assertIn("Test-Path $configPath", text)
        self.assertIn("ShouldProcess($configPath", text)

    def test_install_script_follows_a_state_dir_the_owner_chose(self):
        text = (SCRIPTS / "Install-Drover.ps1").read_text(encoding="utf-8")
        self.assertIn("'--state-dir', $Root", text)                       # -Root is the wizard's first default
        self.assertIn("did not write $configPath", text)
        self.assertIn("SetEnvironmentVariable('DROVER_HOME'", text)
        self.assertIn("$env:DROVER_HOME = $configHome", text)
        self.assertIn("-Root $Root", text)                                # Install-DroverHub.ps1 gets the same state dir

    def test_ollama_rule_is_dry_by_default_and_scoped(self):
        text = (SCRIPTS / "Add-OllamaFirewallRule.ps1").read_text(encoding="utf-8")
        self.assertIn("11434", text)
        self.assertIn("-InterfaceAlias", text)
        self.assertIn("[switch]$Apply", text)
        self.assertNotIn("-RemoteAddress Any", text)
