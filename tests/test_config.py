import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from drover import config, placement

REPO = Path(__file__).resolve().parents[1]


def load(toml="", env=None):
    """Load an isolated machine config without real files or environment."""
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        if toml:
            (home / "config.toml").write_text(toml, encoding="utf-8")
        return config.load(home, env or {})


class Defaults(unittest.TestCase):
    """The literals below are today's built-in values, written out so a change to a default shows up here."""

    def test_codex_sandbox_is_safe_and_configurable_per_seat(self):
        self.assertEqual(load()["seats"][1]["sandbox"], "workspace-write")
        text = ("[[seats]]\nname='a'\nbackend='claude'\ndirector=true\n"
                "[[seats]]\nname='b'\nbackend='codex'\nsandbox='read-only'\n")
        self.assertEqual(load(text)["seats"][1]["sandbox"], "read-only")
        with self.assertRaises(config.ConfigError):
            load(text.replace("read-only", "danger-full-access"))

    def test_defaults_equal_todays_values(self):
        c = load()
        self.assertEqual(c["capacity"]["parallel_cap"], 4)
        self.assertEqual(c["policy"], {"tau": 0.8, "landing_batch_size": 3, "tuner_cadence_days": [1, 1, 2, 3, 5, 7],
                                       "push": False})
        self.assertEqual(c["models"]["ids"], {"haiku-5.5": "claude-haiku-5-5", "sonnet-5.5": "claude-sonnet-5-5",
                                              "opus-5.5": "claude-opus-5-5"})
        self.assertEqual(c["models"]["ladders"], {"claude": ["haiku-5.5", "sonnet-5.5", "opus-5.5"],
                                                  "codex": ["gpt-6-luna", "gpt-6.1-sol"]})
        self.assertEqual((c["models"]["review"], c["models"]["director"]), ("sonnet-5.5", "opus-5.5"))
        self.assertEqual(c["models"]["efforts"], {"cheap": "xhigh", "mid": "high", "top": "high",
                                                  "small_claude": "max", "small_codex": "xhigh"})
        self.assertEqual(c["models"]["start"], {"S": 0, "M": 1, "L": 1})
        self.assertEqual((c["hub"]["ui_port"], c["hub"]["api_port"], c["hub"]["api_listen_wsl"]), (8765, 8766, False))
        self.assertEqual(c["hub"]["auth"]["mode"], "tailscale")
        self.assertEqual(c["decider"]["models"], {"primary": "nimble-v3", "shadow": "nimble", "long_state": "nimble"})
        self.assertEqual((c["decider"]["url"], c["decider"]["timeout_s"], c["decider"]["mode"]), ("", 5.0, "shadow"))
        self.assertEqual((c["paths"]["wsl_distro"], c["paths"]["repos_root"]), ("Ubuntu", "~/repos"))

    def test_default_trackers_are_the_old_resources_toml(self):
        t = {x["name"]: x for x in load()["trackers"]}
        self.assertEqual((t["load"]["queue_per_core"], t["load"]["hold_per_core"]), (2.5, 3.5))
        self.assertEqual((t["ram"]["queue_base_gb"], t["ram"]["queue_per_worker_gb"], t["ram"]["hold_gb"]), (6.0, 0.5, 4.0))
        self.assertEqual([x for x in t.values() if x["kind"] == "disk"], placement.default_disk_trackers())
        self.assertEqual(t["vram"]["fallback_pct"], 80.0)
        self.assertEqual((t["vram"]["queue_pct"], t["vram"]["hold_pct"]), (0.0, 0.0))     # VRAM never queues or holds
        self.assertEqual((t["wsl-up"]["queue_mem_pct"], t["wsl-up"]["hold_mem_pct"]), (0.0, 0.0))

    def test_default_seats(self):
        s = {x["name"]: x for x in load()["seats"]}
        self.assertEqual(s["claude"]["priority"], s["codex"]["priority"])           # equal: the old headroom split applies
        self.assertEqual((s["claude"]["placement"], s["codex"]["placement"]), ("windows", "wsl"))
        self.assertEqual((s["claude"]["reserve_pct"], s["claude"]["director_reserve_pct"], s["codex"]["reserve_pct"]),
                         (10.0, 25.0, 10.0))
        self.assertEqual((s["claude"]["max_workers"], s["codex"]["max_workers"]), (0, 0))
        self.assertTrue(s["claude"]["director"])

    def test_state_dir_defaults_to_the_home_dir(self):
        with tempfile.TemporaryDirectory() as d:
            c = config.load(Path(d), {})
            self.assertEqual(c["paths"]["state_dir"], "")
            self.assertEqual(config.state_dir(c), config.home())
        self.assertEqual(config.home({}), Path.home() / ".drover")
        self.assertEqual(config.home({"DROVER_HOME": "/x/y"}), Path("/x/y"))

    def test_state_dir_layout(self):
        c = load("[paths]\nstate_dir = '/s/state'\n")
        self.assertEqual(config.state_dir(c), Path("/s/state"))
        self.assertEqual(config.repos_dir(c), Path("/s/state/repos"))
        self.assertEqual(config.repos_dir(load("repos_dir = '/p'\n")), Path("/p"))


class Layers(unittest.TestCase):
    def test_file_overrides_defaults_and_keeps_the_rest(self):
        c = load("[capacity]\nparallel_cap = 6\n[hub]\nui_port = 9000\n")
        self.assertEqual((c["capacity"]["parallel_cap"], c["hub"]["ui_port"], c["hub"]["api_port"]), (6, 9000, 8766))

    def test_env_overrides_everything(self):
        c = load("[capacity]\nparallel_cap = 6\n",
                 {"DROVER_CAPACITY_PARALLEL_CAP": "7", "DROVER_HUB_TAILSCALE": "no", "DROVER_HUB_ALLOWED_LOGINS": "a@x, b@y",
                  "DROVER_DECIDER_MODELS_PRIMARY": "tev1", "DROVER_POLICY_TAU": "0.7"})
        self.assertEqual(c["capacity"]["parallel_cap"], 7)
        self.assertIs(c["hub"]["tailscale"], False)
        self.assertEqual(c["hub"]["allowed_logins"], ["a@x", "b@y"])
        self.assertEqual((c["decider"]["models"]["primary"], c["policy"]["tau"]), ("tev1", 0.7))

    def test_older_env_names_still_work(self):
        c = load(env={"DROVER_OLLAMA_URL": "http://h:1", "DROVER_REPOS_DIR": "D:\\r", "DROVER_WSL_USER": "me",
                      "DROVER_WSL_DISTRO": "Debian"})
        self.assertEqual((c["decider"]["url"], c["paths"]["repos_root"], c["paths"]["wsl_user"], c["paths"]["wsl_distro"]),
                         ("http://h:1", "D:\\r", "me", "Debian"))

    def test_runtime_variables_are_not_configuration(self):
        env = {"DROVER_RUN_ID": "3", "DROVER_RUN_NONCE": "x", "DROVER_RESOURCES_FAKE": "{}", "DROVER_ADAPTER": "a",
               "DROVER_STATE_DIR": "/garden", "DROVER_STATE": "/allowance", "DROVER_LEDGER": "/db", "DROVER_NOPE": "1"}
        self.assertEqual(load(env=env), load())
        for name in env:
            self.assertNotIn(name, config.env_names())

    def test_bad_env_value_is_a_clear_error(self):
        with self.assertRaisesRegex(config.ConfigError, "DROVER_HUB_UI_PORT must be a number"):
            load(env={"DROVER_HUB_UI_PORT": "eighty"})
        with self.assertRaisesRegex(config.ConfigError, "DROVER_HUB_TAILSCALE must be true or false"):
            load(env={"DROVER_HUB_TAILSCALE": "maybe"})

    def test_trackers_merge_by_name(self):
        c = load("[[trackers]]\nkind='load'\nqueue_per_core = 3.0\n[[trackers]]\nkind='disk'\nname='d'\npath='D:\\\\'\n"
                 "queue_gb=10\nhold_gb=5\ncritical_gb=1\n[[trackers]]\nkind='disk'\nname='h'\nenabled=false\n")
        t = {x["name"]: x for x in c["trackers"]}
        self.assertEqual((t["load"]["queue_per_core"], t["load"]["hold_per_core"]), (3.0, 3.5))
        self.assertEqual((t["d"]["queue_gb"], t["d"]["hold_gb"]), (10.0, 5.0))
        self.assertIs(t["h"]["enabled"], False)
        self.assertIn("ram", t)

    def test_seats_replace_the_defaults(self):
        c = load("[[seats]]\nname='work'\nbackend='claude'\ndirector=true\nconfig_dir='C:\\acct2'\n")
        self.assertEqual([s["name"] for s in c["seats"]], ["work"])
        self.assertEqual(config.seat_env(c["seats"][0]), {"CLAUDE_CONFIG_DIR": "C:\\acct2"})
        self.assertEqual(c["seats"][0]["director_reserve_pct"], 25.0)

    def test_codex_seat_env_is_codex_home(self):
        c = load("[[seats]]\nname='a'\nbackend='claude'\ndirector=true\n[[seats]]\nname='b'\nbackend='codex'\n"
                 "config_dir='/home/me/.codex-b'\n")
        self.assertEqual(config.seat_env(c["seats"][1]), {"CODEX_HOME": "/home/me/.codex-b"})
        with patch('os.path.expanduser', return_value='/home/acme/.claude'):
            self.assertEqual(config.seat_env(c["seats"][0]), {'CLAUDE_CONFIG_DIR': '/home/acme/.claude'})

    def test_wsl_default_accounts_expand_on_the_seats_os(self):
        seats = load()['seats']
        with patch('os.path.expanduser', side_effect=AssertionError('must expand inside WSL')):
            self.assertEqual(config.seat_env({**seats[0], 'placement': 'wsl'}),
                             {'CLAUDE_CONFIG_DIR': '~/.claude'})
            self.assertEqual(config.seat_env(seats[1]), {'CODEX_HOME': '~/.codex'})
            self.assertEqual(config.seat_env(None), {})

    def test_reserved_oidc_keys_are_accepted_and_ignored(self):
        c = load("[hub.auth]\nmode='tailscale'\nissuer='https://accounts.google.com'\nclient_id='x'\n"
                 "allowed_emails=['a@b.org']\n")
        self.assertEqual(c["hub"]["auth"]["issuer"], "https://accounts.google.com")


class Validation(unittest.TestCase):
    def bad(self, toml, pattern):
        with self.assertRaisesRegex(config.ConfigError, pattern):
            load(toml)

    def test_not_toml(self):
        self.bad("[hub\nui_port=1", "not valid TOML")

    def test_unknown_keys_name_the_known_ones(self):
        self.bad("[hub]\nui_prot = 1\n", r"unknown key hub\.ui_prot; known keys: .*ui_port")
        self.bad("[bogus]\nx = 1\n", "unknown key bogus")
        self.bad("[[seats]]\nname='a'\nbackend='claude'\ndirector=true\nspeed=3\n", "unknown key seats.0.speed")

    def test_types_and_ranges(self):
        self.bad("[capacity]\nparallel_cap = 'many'\n", r"capacity\.parallel_cap must be a whole number")
        self.bad("[capacity]\nparallel_cap = 0\n", r"capacity\.parallel_cap must be between 1 and 64")
        self.bad("[hub]\nui_port = 70000\n", r"hub\.ui_port must be between")
        self.bad("[hub]\nui_port = 5\napi_port = 5\n", "must differ")
        self.bad("[policy]\ntau = 1.5\n", r"policy\.tau must be between 0 and 1")
        self.bad("[policy]\nlanding_batch_size = 9\n", "landing_batch_size must be between 1 and 5")
        self.bad("[policy]\ntuner_cadence_days = [0]\n", "tuner_cadence_days")
        self.bad("[decider]\ndevice = 'tpu'\n", "decider.device must be one of auto, gpu, cpu")
        self.bad("[hub.auth]\nmode = 'oidc'\n", "hub.auth.mode must be one of tailscale")
        self.bad("[paths]\nwsl_distro = 5\n", r"paths\.wsl_distro must be a string")

    def test_seats(self):
        seat = "[[seats]]\nname='{n}'\nbackend='{b}'\n{extra}"
        self.bad("[[seats]]\nbackend='claude'\n", "needs a name")
        self.bad("[[seats]]\nname='a'\ndirector=true\n", "needs a backend")
        self.bad(seat.format(n="a", b="gemini", extra="director=true\n"), "seats.0.backend must be one of claude, codex")
        self.bad(seat.format(n="a", b="claude", extra=""), "exactly one seat must have director = true \\(found none\\)")
        self.bad(seat.format(n="a", b="claude", extra="director=true\n") * 2, "used twice")
        two = (seat.format(n="a", b="claude", extra="director=true\n") + seat.format(n="b", b="claude", extra="director=true\n"))
        self.bad(two, "found a, b")
        self.bad(seat.format(n="a", b="codex", extra="director=true\n"), "only a claude seat can be the director")
        self.bad(seat.format(n="a", b="claude", extra="director=true\n") +
                 seat.format(n="c", b="codex", extra="placement='windows'\n"), "codex must run in wsl")
        self.bad(seat.format(n="a", b="claude", extra="director=true\nreserve_pct=120\n"), "reserve_pct must be between")
        self.bad(seat.format(n="a", b="claude", extra="five_hour_per_hour_pct=-1\n"), "five_hour_per_hour_pct must be between")
        self.bad(seat.format(n="a", b="claude", extra="director=true\nmax_workers=-1\n"), "max_workers must be between")
        self.bad(seat.format(n="a b", b="claude", extra="director=true\n"), "letters, digits")
        self.bad(seat.format(n="a", b="claude", extra="director=true\npriority='high'\n"), "priority must be a whole number")

    def test_trackers(self):
        self.bad("[[trackers]]\nname='x'\n", "needs kind")
        self.bad("[[trackers]]\nkind='fan'\n", "kind must be one of ram, disk, load, vram, wsl")
        self.bad("[[trackers]]\nkind='disk'\nname='d'\nqueue_gb=5\nhold_gb=9\n", "hold_gb must not exceed queue_gb")
        self.bad("[[trackers]]\nkind='load'\nqueue_per_core=4\n", "hold_per_core must be at least")
        self.bad("[[trackers]]\nkind='ram'\nhold_gb=7\n", "hold_gb must not exceed queue_base_gb")
        self.bad("[[trackers]]\nkind='wsl'\nname='wsl-up'\nqueue_mem_pct=90\nhold_mem_pct=80\n", "hold_mem_pct must be at least")
        self.bad("[[trackers]]\nkind='disk'\nname='d'\nwsl_source='nfs'\n", "migrate-machine")
        self.bad("[[trackers]]\nkind='load'\nbogus=1\n", "unknown key trackers")

    def test_models(self):
        self.bad("[models]\nreview = 'gpt-9'\n", "not on any ladder")
        self.bad("[models]\ndirector = 'gpt-6.1-sol'\n", "must be on the claude ladder")
        self.bad("[models.ladders]\nclaude = []\n", "must not be empty")
        self.bad("[models.ladders]\ngemini = ['x']\n", "named for a backend")
        self.bad("[models.start]\nQ = 1\n", "unknown key models.start.Q")

    def test_errors_name_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "config.toml").write_text("[hub]\nui_port = 'x'\n")
            with self.assertRaisesRegex(config.ConfigError, "config.toml"):
                config.load(Path(d), {})


class Writer(unittest.TestCase):
    def test_round_trip(self):
        c = load("[paths]\nrepos_root = 'R:\\repos'\nworktrees = \"it's\"\n[[seats]]\nname='a'\nbackend='claude'\ndirector=true\n")
        self.assertEqual(tomllib.loads(config.dumps(c)), c)

    def test_backslash_paths_are_literal_strings(self):
        self.assertIn("'C:\\Users\\me'", config.dumps({"p": "C:\\Users\\me"}))

    def test_example_file_loads_with_account_seats(self):
        raw = tomllib.loads((REPO / "drover.example.toml").read_text(encoding="utf-8"))
        cfg = config.validate(raw)
        self.assertEqual([(s["name"], s["priority"]) for s in cfg["seats"]],
                         [("codex", 1), ("claude", 2), ("claude-personal", 3)])
        self.assertEqual(cfg["director"], {"seat": "claude", "model": "opus-5.5", "effort": "high"})
        self.assertEqual(config.seat_env(cfg["seats"][0]), {"CODEX_HOME": "~/.codex"})
        for key in set(cfg) - {"seats", "director"}:
            self.assertEqual(cfg[key], load()[key])

    def test_example_documents_every_key(self):
        text = (REPO / "drover.example.toml").read_text(encoding="utf-8")
        for key in ("repos_dir", "state_dir", "repos_root", "worktrees", "wsl_distro", "wsl_user", "priority",
                    "max_workers", "reserve_pct", "director_reserve_pct", "director", "placement", "binary", "config_dir",
                    "parallel_cap", "queue_per_core", "hold_per_core", "queue_base_gb", "queue_per_worker_gb", "hold_gb",
                    "warn_gb", "critical_gb", "queue_gb", "fallback_pct", "queue_pct", "hold_pct", "distro", "queue_mem_pct",
                    "hold_mem_pct", "tau", "landing_batch_size", "tuner_cadence_days", "push", "url", "device", "mode",
                    "timeout_s", "ollama_models", "primary", "shadow", "long_state", "ui_port", "api_port", "tailscale",
                    "allowed_logins", "api_listen_wsl", "issuer", "client_id", "allowed_emails", "small_claude",
                    "small_codex", "cheap", "mid", "top", "review", "ladders", "ids", "start"):
            self.assertRegex(text, rf"(?m)^\"?{key}\"?\s*=|^\[[a-z.]*{key}\]", key)

    def test_cli_get_and_check(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(config.main(["--get", "hub.ui_port"]), 0)
        self.assertEqual(buf.getvalue().strip(), "8765")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(config.main(["--get", "hub.nope"]), 2)
            self.assertEqual(config.main(["--check", str(REPO / "drover.example.toml")]), 0)


if __name__ == "__main__":
    unittest.main()
