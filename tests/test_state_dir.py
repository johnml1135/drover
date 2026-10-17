"""The state dir (default ~/.drover) is where the ledger, runs, token and lock live; hub settings follow the config."""
import os
import tempfile
import unittest
from pathlib import Path

from drover import config, launch, lock, pipeline
from drover.ledger import db
from hub.settings import Settings


class StateDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        config.use(config.load(Path(self.tmp.name), Path(self.tmp.name) / "none",
                               {"DROVER_PATHS_STATE_DIR": str(self.state)}))
        self.addCleanup(config.reset)

    def test_every_default_path_is_under_the_state_dir(self):
        self.assertEqual(lock.default_path({}), self.state / "dispatcher.lock")
        self.assertEqual(Path(launch.RUNS_DEFAULT), self.state / "runs")
        self.assertEqual(Path(launch.RunnerLauncher().runs_dir), self.state / "runs")
        self.assertEqual(Path(pipeline.Pipeline(None).runs_dir), self.state / "runs")
        s = Settings()
        self.assertEqual((s.token_path, s.ledger_path, s.runs_dir, s.dispatcher_lock),
                         (self.state / "hub-token", self.state / "ledger.db", self.state / "runs",
                          self.state / "dispatcher.lock"))

    def test_ledger_path(self):
        if os.name == "nt":
            self.assertEqual(db.default_path({}), self.state / "ledger.db")
        else:
            with self.assertRaises(db.HubRequired):
                db.default_path({})
        self.assertEqual(db.default_path({"DROVER_LEDGER": "/x/l.db"}), Path("/x/l.db"))

    def test_env_names_for_the_paths_still_win(self):
        self.assertEqual(lock.default_path({"DROVER_DISPATCHER_LOCK": "/l"}), Path("/l"))

    def test_no_c_drover_default_remains(self):
        for p in (lock.default_path({}), Settings().runs_dir):
            self.assertFalse(str(p).lower().startswith("c:\\drover"))

    def test_the_default_home_is_dot_drover_in_the_profile(self):
        self.assertEqual(config.home({}), Path.home() / ".drover")


class HubSettings(unittest.TestCase):
    def tearDown(self):
        config.reset()

    def use(self, **raw):
        config.use(config.validate(raw))

    def test_ports_distro_user_logins_and_ollama_from_config(self):
        self.use(hub={"ui_port": 9000, "api_port": 9001, "allowed_logins": ["me@x.org"], "api_listen_wsl": True},
                 paths={"wsl_distro": "Debian", "wsl_user": "wuser"}, decider={"url": "http://gw:11434"})
        s = Settings()
        self.assertEqual((s.ui_port, s.api_port, s.distro, s.wsl_user, s.ui_allowed_logins, s.ollama_url, s.api_listen_wsl),
                         (9000, 9001, "Debian", "wuser", ("me@x.org",), "http://gw:11434", True))

    def test_defaults_are_todays(self):
        s = Settings()
        self.assertEqual((s.ui_port, s.api_port, s.distro, s.ollama_url, s.use_tailscale, s.auth_mode),
                         (8765, 8766, "Ubuntu", "http://127.0.0.1:11434", True, "tailscale"))
        self.assertEqual(s.ui_allowed_logins, ())

    def test_explicit_settings_still_win(self):
        self.assertEqual(Settings(ui_port=1234, distro="X").ui_port, 1234)

    def test_tailscale_off_means_loopback_only_and_no_lookups(self):
        try:
            from hub import auth
        except ImportError:
            self.skipTest("hub needs fastapi")
        self.use(hub={"tailscale": False})
        calls = []
        s = Settings(tailscale=lambda a: calls.append(a) or "")
        self.assertEqual(auth.resolve_allowed_logins(s), ())
        self.assertEqual(auth.resolve_allowed_hosts(s), frozenset({"127.0.0.1:8765", "localhost:8765"}))
        self.assertEqual(calls, [])

    def test_reserved_oidc_keys_do_not_change_auth(self):
        self.use(hub={"auth": {"mode": "tailscale", "issuer": "https://accounts.google.com", "client_id": "x"}})
        self.assertEqual(Settings().auth_mode, "tailscale")


if __name__ == "__main__":
    unittest.main()
