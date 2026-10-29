"""Hub API tests: auth, owner gate, tasks, claim race, import, SSE."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
    from hub.app import create_app
    from hub.settings import Settings
except ImportError as exc:  # hub deps are third-party; skip when absent
    raise unittest.SkipTest(f"hub deps missing: {exc}")


from tests.test_hub_api import AUTH, SAME, fake_tailscale, no_tailscale, make, HubCase


class HostAllowlistTests(unittest.TestCase):
    def client(self, **kw):
        tmp, app = make(trusted_ui_hosts=("testclient",), ui_allowed_hosts=(), **kw)
        self.addCleanup(tmp.cleanup)
        self.addCleanup(app.state.store.close)
        return app, TestClient(app, follow_redirects=False)

    def test_only_loopback_names_and_tailnet_name(self):
        import json
        ts = lambda a: json.dumps({"Self": {"UserID": 7, "DNSName": "Box.tail1234.ts.net."},
                                   "User": {"7": {"LoginName": "owner@example.com"}}})
        app, c = self.client(tailscale=ts)
        self.assertIn("box.tail1234.ts.net", app.state.allowed_hosts)
        for host, code in (("127.0.0.1:8765", 200), ("localhost:8765", 200),
                           ("box.tail1234.ts.net", 200), ("evil.example", 403),
                           ("127.0.0.1:9999", 403), ("localhost", 403)):
            r = c.get("/inbox", headers={"Host": host})
            self.assertEqual(r.status_code, code, host)

    def test_rebound_host_cannot_use_ui_identity_on_api(self):
        app, c = self.client()
        r = c.get("/api/v1/tasks", headers={"Host": "evil.example"})
        self.assertEqual(r.status_code, 401)
        r = c.get("/api/v1/tasks", headers={"Host": "127.0.0.1:8765"})
        self.assertEqual(r.status_code, 200)
        # a bearer caller is unaffected by Host
        r = c.get("/api/v1/tasks", headers={"Host": "evil.example", **AUTH})
        self.assertEqual(r.status_code, 200)


class TokenFileTests(unittest.TestCase):
    def test_created_owner_only_via_icacls_argv(self):
        from hub import auth
        calls = []
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            s = Settings(token_path=Path(d) / "sub" / "hub-token", icacls=lambda a: calls.append(a) or "")
            tok = auth.load_token(s)
            self.assertEqual(s.token_path.read_text().strip(), tok)
            self.assertEqual(auth.load_token(s), tok)
        user = auth.owner_name()
        self.assertEqual(calls[0][1:], ["/inheritance:r", "/grant:r", f"{user}:(OI)(CI)F"])
        self.assertEqual(calls[1][1:], ["/inheritance:r", "/grant:r", f"{user}:F"])
        self.assertTrue(all(isinstance(x, str) for c in calls for x in c))

    def test_startup_warns_when_acl_is_broad(self):
        from hub import auth
        out = ("C:\\drover\\hub-token BUILTIN\\Users:(R)\n"
               "                    Everyone:(R)\n"
               "                    ME\\owner:(F)\n")
        self.assertEqual(len(auth.acl_warnings(lambda a: out, "x")), 2)
        self.assertEqual(auth.acl_warnings(lambda a: "x ME\\owner:(F)\n", "x"), [])
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            p = Path(d) / "hub-token"
            p.write_text("abc\n")
            with self.assertLogs("hub", "WARNING") as cm:
                auth.load_token(Settings(token_path=p, icacls=lambda a: out))
        self.assertTrue(any("broad group" in m for m in cm.output))


class SettingsGuardTests(HubCase):
    def put(self, body, **kw):
        return self.c.put("/api/v1/settings", json=body, headers=AUTH, **kw)

    def test_token_cannot_change_security_keys(self):
        for k in ("wsl_distro", "wsl_user", "ui_allowed_logins", "watchdog_x", "dispatch_enabled", "gate_cmd_acme-tools",
                  "gate_cmd_anything"):
            self.assertEqual(self.put({k: "x"}).status_code, 403, k)
        self.assertEqual(self.put({"note": "fine"}).status_code, 200)

    def test_owner_can_but_values_are_validated(self):
        tmp, app = make(trusted_ui_hosts=("testclient",))
        self.addCleanup(tmp.cleanup)
        self.addCleanup(app.state.store.close)
        c = TestClient(app)
        ok = c.put("/api/v1/settings", json={"wsl_distro": "Ubuntu-24.04"},
                   headers={**SAME, "Host": "testserver"})
        self.assertEqual(ok.status_code, 200)
        for bad in ("--shutdown", "-d", "a b", "x;y", ""):
            r = c.put("/api/v1/settings", json={"wsl_user": bad}, headers=SAME)
            self.assertEqual(r.status_code, 400, bad)

    def test_director_execution_settings_require_owner_and_csrf(self):
        body = {'director_execution_enabled': 'true', 'director_execution': '{}'}
        original = self.get('/settings').json()
        for key, value in body.items():
            self.assertEqual(self.put({key: value}).status_code, 403, key)
        self.assertEqual(self.put(body).status_code, 403)
        self.assertEqual(self.get('/settings').json(), original)
        tmp, app = make(trusted_ui_hosts=('testclient',), use_tailscale=False)
        self.addCleanup(tmp.cleanup)
        self.addCleanup(app.state.store.close)
        client = TestClient(app)
        self.assertEqual(client.put('/api/v1/settings', json=body).status_code, 403)
        response = client.put('/api/v1/settings', json=body, headers=SAME)
        self.assertEqual(response.status_code, 200)
        saved = client.get('/api/v1/settings', headers=AUTH).json()
        self.assertEqual({key: saved[key] for key in body}, body)


class BusBoundTests(unittest.TestCase):
    def test_slow_subscriber_is_bounded_and_told_to_resync(self):
        import asyncio

        from hub import events

        async def go():
            bus = events.Bus()
            q = bus.subscribe()
            for i in range(events.QUEUE_MAX * 3):
                bus.publish("x", {"i": i})
            await asyncio.sleep(0.05)
            items = []
            while not q.empty():
                items.append(q.get_nowait())
            return items

        items = asyncio.run(go())
        self.assertLessEqual(len(items), events.QUEUE_MAX)
        self.assertIn("resync", [e["kind"] for e in items])
        self.assertEqual(items[-1]["data"], {"i": events.QUEUE_MAX * 3 - 1})


class RealListenerTests(unittest.TestCase):
    """serve_api with real uvicorn on loopback ports."""

    def free_port(self):
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def drive(self, app, settings, ips, seconds):
        import asyncio

        from hub import __main__ as m

        seq = iter(ips)
        last = ips[-1]

        async def go():
            task = asyncio.create_task(m.serve_api(app, settings, lambda g: next(seq, last)))
            await asyncio.sleep(seconds)
            alive = not task.done()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return alive

        return asyncio.run(go())

    def test_rebind_leaves_watchdog_and_store_running(self):
        import urllib.request
        tmp, app = make(run_watchdog=True)
        self.addCleanup(tmp.cleanup)
        port = self.free_port()
        s = Settings(token="tok", api_port=port, adapter_retry_s=0.3, run_watchdog=True,
                     watchdog_interval_s=3600)
        app.state.start_background()
        self.addCleanup(app.state.shutdown)
        dog = app.state.watchdog
        self.assertTrue(dog._thread.is_alive())
        alive = self.drive(app, s, ["127.0.0.1", "127.0.0.2"], 2.5)
        self.assertTrue(alive)
        self.assertFalse(dog._stop.is_set())
        self.assertTrue(dog._thread.is_alive())
        app.state.store.list_tasks()  # store still open
        msgs = [e["data"]["message"] for e in app.state.store.list_events(kind="hub.api-listener")]
        self.assertTrue(any("127.0.0.2" in m for m in msgs), msgs)

    def test_occupied_port_never_kills_the_hub_and_is_retried(self):
        import socket
        tmp, app = make()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(app.state.store.close)
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        s = Settings(token="tok", api_port=port, adapter_retry_s=0.2, run_watchdog=False)
        try:
            alive = self.drive(app, s, ["127.0.0.1"], 1.5)
        finally:
            blocker.close()
        self.assertTrue(alive)  # SystemExit(3) from uvicorn was contained
        msgs = [e["data"]["message"] for e in app.state.store.list_events(kind="hub.api-listener")]
        self.assertGreaterEqual(sum("failed" in m for m in msgs), 2, msgs)  # retried
        self.assertTrue(any(e["kind"] == "alert" for e in app.state.bus.recent()))

    def test_listener_recovers_when_port_frees_up(self):
        import socket
        import urllib.request
        tmp, app = make()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(app.state.store.close)
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        s = Settings(token="tok", api_port=port, adapter_retry_s=0.2, run_watchdog=False)
        import asyncio

        from hub import __main__ as m

        async def go():
            task = asyncio.create_task(m.serve_api(app, s, lambda g: "127.0.0.1"))
            await asyncio.sleep(0.8)
            blocker.close()
            await asyncio.sleep(1.0)
            code = await asyncio.to_thread(
                lambda: urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/health").status)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return code

        self.assertEqual(asyncio.run(go()), 200)
