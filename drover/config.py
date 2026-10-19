"""Drover configuration: one TOML file, defaults that reproduce the built-in behaviour, validation with clear errors.

    python -m drover.config [--show] [--get paths.state_dir] [--check FILE]

Layers, lowest first: built-in DEFAULTS, <home>/config.toml, the overlay <drover checkout>/.drover/config.toml, then
DROVER_* environment overrides. The home is $DROVER_HOME or ~/.drover (%USERPROFILE%\\.drover on Windows). The overlay
is read from the drover checkout (or the file named by DROVER_OVERLAY), never from the current directory: a worker
writing inside a worktree cannot plant one.
Tables merge key by key; [[trackers]] merge by name; [[seats]] replace the default seats as a whole.
Environment: DROVER_<SECTION>_<KEY> for every scalar key (DROVER_HUB_UI_PORT, DROVER_PATHS_STATE_DIR,
DROVER_DECIDER_MODELS_PRIMARY, DROVER_CAPACITY_PARALLEL_CAP, ...), lists comma-separated, plus the older names in
ENV_ALIASES. Other DROVER_* variables (run ids, fakes) are not configuration and are ignored.
Config is the bottom layer for values the tuner or the ledger also own (parallel cap, tau, batch size): it sets the
starting value only.
"""
import copy
import json
import logging
import os
import re
import sys
import tomllib
from pathlib import Path

CHECKOUT = Path(__file__).resolve().parents[1]
BACKENDS = ("claude", "codex")
PLACEMENTS = ("windows", "wsl")
TRACKER_KINDS = ("ram", "disk", "load", "vram", "wsl")
DEVICES = ("auto", "gpu", "cpu")
AUTH_MODES = ("tailscale",)
SIZES = ("S", "M", "L")
WIN_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$", re.S)
MNT_RE = re.compile(r"^/mnt/([a-zA-Z])(?:/(.*))?$")
NAME_RE = re.compile(r"[A-Za-z0-9._-]+")
ENV_ALIASES = {"DROVER_OLLAMA_URL": "decider.url", "DROVER_REPOS_DIR": "paths.repos_root",
               "DROVER_WSL_USER": "paths.wsl_user", "DROVER_WSL_DISTRO": "paths.wsl_distro"}

DEFAULT_SEATS = [
    {"name": "claude", "backend": "claude", "priority": 1, "max_workers": 0, "reserve_pct": 10.0,
     "director_reserve_pct": 25.0, "director": True, "placement": "windows", "binary": "claude", "config_dir": ""},
    {"name": "codex", "backend": "codex", "priority": 1, "max_workers": 0, "reserve_pct": 10.0,
     "director_reserve_pct": 0.0, "director": False, "placement": "wsl", "binary": "codex", "config_dir": ""},
]
SEAT_DEFAULTS = {"priority": 1, "max_workers": 0, "reserve_pct": 10.0, "director_reserve_pct": 0.0,
                 "five_hour_base_pct": 5.0, "five_hour_per_hour_pct": 5.0, "director": False, "config_dir": "", "codex_home": "", "sandbox": "workspace-write"}
DEFAULT_TRACKERS = [
    {"kind": "load", "name": "load", "enabled": True, "queue_per_core": 2.5, "hold_per_core": 3.5},
    {"kind": "ram", "name": "ram", "enabled": True, "queue_base_gb": 6.0, "queue_per_worker_gb": 0.5, "hold_gb": 4.0},
    {"kind": "disk", "name": "h", "enabled": True, "path": "~/repos", "wsl_source": "vhdx", "queue_gb": 75.0, "hold_gb": 25.0},
    {"kind": "disk", "name": "wsl", "enabled": True, "path": "", "wsl_source": "home", "queue_gb": 75.0, "hold_gb": 25.0},
    {"kind": "disk", "name": "c", "enabled": True, "path": "C:\\", "wsl_source": "path", "queue_gb": 50.0, "hold_gb": 25.0},
    {"kind": "vram", "name": "vram", "enabled": True, "fallback_pct": 80.0, "queue_pct": 0.0, "hold_pct": 0.0},
    {"kind": "wsl", "name": "wsl-up", "enabled": True, "distro": "", "queue_mem_pct": 0.0, "hold_mem_pct": 0.0},
]
DEFAULTS = {
    "repos_dir": "",
    "config_sync": {"remote": "", "auto_push": True},
    "paths": {"state_dir": "", "repos_root": "~/repos", "worktrees": "", "wsl_distro": "Ubuntu", "wsl_user": ""},
    "seats": DEFAULT_SEATS,
    "seats_policy": {"waste_threshold_pct": 15.0},
    "director": {"seat": "", "model": "", "effort": "high"},
    "models": {
        "ids": {"haiku-5.5": "claude-haiku-5-5", "sonnet-5.5": "claude-sonnet-5-5", "opus-5.5": "claude-opus-5-5"},
        "efforts": {"cheap": "xhigh", "mid": "high", "top": "high", "small_claude": "max", "small_codex": "xhigh"},
        "ladders": {"claude": ["haiku-5.5", "sonnet-5.5", "opus-5.5"], "codex": ["gpt-6-luna", "gpt-6.1-sol"]},
        "start": {"S": 0, "M": 1, "L": 1},
        "review": "sonnet-5.5",
        "director": "opus-5.5",
    },
    "capacity": {"parallel_cap": 4},
    "trackers": DEFAULT_TRACKERS,
    "policy": {"tau": 0.8, "landing_batch_size": 3, "tuner_cadence_days": [1, 1, 2, 3, 5, 7], "push": False},
    "decider": {"commercial_use": False,"url": "", "models": {"primary": "nimble-v3", "shadow": "nimble", "long_state": "nimble"},
                "device": "gpu", "mode": "shadow", "timeout_s": 5.0, "ollama_models": ""},
    "hub": {"ui_port": 8765, "api_port": 8766, "tailscale": True, "allowed_logins": [], "api_listen_wsl": False,
            "auth": {"mode": "tailscale", "issuer": "", "client_id": "", "allowed_emails": []}},
}
RESERVED = {("hub", "auth", "issuer"), ("hub", "auth", "client_id"), ("hub", "auth", "allowed_emails")}


class ConfigError(ValueError):
    pass


# ---- paths ----------------------------------------------------------------------------------------------------

def native(path):
    """A config path in the form this OS can open: R:\\x on Windows, /mnt/r/x on Linux; anything else unchanged."""
    path = os.path.expanduser(str(path))
    if os.name == "nt":
        m = MNT_RE.match(path)
        return f"{m.group(1).upper()}:\\" + (m.group(2) or "").replace("/", "\\") if m else path
    m = WIN_RE.match(path)
    return f"/mnt/{m.group(1).lower()}/" + m.group(2).replace("\\", "/") if m else path


def home(env=None):
    """Where config.toml is found: $DROVER_HOME, else ~/.drover."""
    env = os.environ if env is None else env
    return Path(native(env["DROVER_HOME"])) if env.get("DROVER_HOME") else Path.home() / ".drover"


def state_dir(cfg=None):
    """The state dir: ledger.db, runs/, rescue/, hub-token, repos/ (and config.toml unless DROVER_HOME differs)."""
    cfg = cfg or get()
    return Path(native(cfg["paths"]["state_dir"])) if cfg["paths"]["state_dir"] else home()


def state_path(name, cfg=None):
    return state_dir(cfg) / name


def repos_dir(cfg=None):
    cfg = cfg or get()
    return Path(native(cfg["repos_dir"])) if cfg["repos_dir"] else state_dir(cfg) / "repos"


def repos_root(cfg=None):
    return Path(native((cfg or get())["paths"]["repos_root"])).expanduser()


# ---- validation -----------------------------------------------------------------------------------------------

def _where(path):
    return ".".join(str(p) for p in path)


def _type_name(v):
    return {str: "a string", bool: "true or false", int: "a whole number", float: "a number",
            list: "a list", dict: "a table"}.get(v, v.__name__)


def _scalar(value, want, path):
    if want is float:
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        value = float(value) if ok else value
    elif want is int:
        ok = isinstance(value, int) and not isinstance(value, bool)
    else:
        ok = isinstance(value, want)
    if not ok:
        raise ConfigError(f"{_where(path)} must be {_type_name(want)}, not {value!r}")
    return value


def _range(value, lo, hi, path):
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        raise ConfigError(f"{_where(path)} must be between {lo} and {hi}, not {value!r}")
    return value


def _choice(value, options, path):
    if value not in options:
        raise ConfigError(f"{_where(path)} must be one of {', '.join(options)}, not {value!r}")
    return value


def _strs(value, path):
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ConfigError(f"{_where(path)} must be a list of strings")
    return list(value)


def _unknown(table, known, path):
    if not isinstance(table, dict):
        raise ConfigError(f"{_where(path)} must be a table")
    extra = sorted(set(table) - set(known))
    if extra:
        raise ConfigError(f"unknown key {_where(path + [extra[0]])}; known keys: {', '.join(sorted(known))}")


def _check_table(table, defaults, path, ranges=None):
    """A copy of table with every default filled in and every value type-checked against its default."""
    ranges = ranges or {}
    _unknown(table, defaults, path)
    out = {}
    for key, default in defaults.items():
        value = table.get(key, default)
        where = path + [key]
        if isinstance(default, dict):
            out[key] = _check_flat_map(value, default, where)
        elif isinstance(default, list):
            out[key] = _check_list(value, default, where)
        else:
            want = type(default)
            out[key] = _scalar(value, want, where)
            if key in ranges:
                _range(out[key], *ranges[key], where)
    return out


def _check_flat_map(value, default, path):
    """A table whose keys are fixed (efforts, start) or free (ids, ladders): values keep the type of their default."""
    if not isinstance(value, dict):
        raise ConfigError(f"{_where(path)} must be a table")
    if path[-1] in ("ids",):
        return {str(k): _scalar(v, str, path + [k]) for k, v in {**default, **value}.items()}
    if path[-1] == "ladders":
        merged = {**default, **value}
        for k, v in merged.items():
            if k not in BACKENDS:
                raise ConfigError(f"{_where(path + [k])}: ladders are named for a backend ({', '.join(BACKENDS)})")
            if not _strs(v, path + [k]):
                raise ConfigError(f"{_where(path + [k])} must not be empty")
        return {k: list(v) for k, v in merged.items()}
    _unknown(value, default, path)
    out = {}
    for k, d in default.items():
        out[k] = _scalar(value.get(k, d), type(d), path + [k])
        if path[-1] == "start":
            _range(out[k], 0, 9, path + [k])
    return out


def _check_list(value, default, path):
    if path[-1] in ("seats", "trackers"):
        return value
    if not isinstance(value, list):
        raise ConfigError(f"{_where(path)} must be a list")
    if default:
        want = type(default[0])
        return [_scalar(x, want, path + [i]) for i, x in enumerate(value)]
    return _strs(value, path)


def _seat(raw, index):
    path = ["seats", index]
    base = SEAT_DEFAULTS
    _unknown(raw, {"name", "backend", "placement", "binary", *base}, path)
    seat = {k: raw.get(k, base[k]) for k in base}
    if "name" not in raw:
        raise ConfigError(f"{_where(path)} needs a name")
    seat["name"] = _scalar(raw["name"], str, path + ["name"])
    if not NAME_RE.fullmatch(seat["name"]):
        raise ConfigError(f"{_where(path + ['name'])} may hold letters, digits, . _ - only: {seat['name']!r}")
    if "backend" not in raw:
        raise ConfigError(f"seat {seat['name']!r} needs a backend ({', '.join(BACKENDS)})")
    seat["backend"] = _choice(raw["backend"], BACKENDS, path + ["backend"])
    seat["priority"] = _range(_scalar(seat["priority"], int, path + ["priority"]), 0, 999, path + ["priority"])
    seat["max_workers"] = _range(_scalar(seat["max_workers"], int, path + ["max_workers"]), 0, 64,
                                 path + ["max_workers"])
    seat["reserve_pct"] = _range(_scalar(seat["reserve_pct"], float, path + ["reserve_pct"]), 0, 100,
                                 path + ["reserve_pct"])
    for k in ("five_hour_base_pct", "five_hour_per_hour_pct"):
        seat[k] = _range(_scalar(seat[k], float, path + [k]), 0, 100, path + [k])
    seat["director_reserve_pct"] = _range(_scalar(seat["director_reserve_pct"], float,
                                                  path + ["director_reserve_pct"]), 0, 100,
                                          path + ["director_reserve_pct"])
    seat["director"] = _scalar(seat["director"], bool, path + ["director"])
    seat["placement"] = _choice(raw.get("placement", "wsl" if seat["backend"] == "codex" else "windows"),
                                PLACEMENTS, path + ["placement"])
    seat["binary"] = _scalar(raw.get("binary", seat["backend"]), str, path + ["binary"])
    if not seat["binary"]:
        raise ConfigError(f"{_where(path + ['binary'])} must not be empty")
    seat["config_dir"] = _scalar(seat["config_dir"], str, path + ["config_dir"])
    seat["codex_home"] = _scalar(seat["codex_home"], str, path + ["codex_home"])
    seat["sandbox"] = _choice(seat["sandbox"], ("read-only", "workspace-write"), path + ["sandbox"])
    if "director_reserve_pct" not in raw:
        seat["director_reserve_pct"] = 25.0 if seat["backend"] == "claude" and seat["director"] else 0.0
    if seat["director"] and seat["backend"] != "claude":
        raise ConfigError(f"seat {seat['name']!r}: only a claude seat can be the director")
    if seat["backend"] == "codex" and seat["placement"] == "windows":
        raise ConfigError(f"seat {seat['name']!r}: codex must run in wsl (on Windows it corrupts the login registry; "
                          "ADR 0002)")
    return seat


def _seats(raw_seats):
    if not isinstance(raw_seats, list) or not raw_seats or not all(isinstance(s, dict) for s in raw_seats):
        raise ConfigError("seats must be a non-empty list of [[seats]] tables")
    seats = [_seat(s, i) for i, s in enumerate(raw_seats)]
    names = [s["name"] for s in seats]
    for n in names:
        if names.count(n) > 1:
            raise ConfigError(f"seat name {n!r} is used twice")
    directors = [s["name"] for s in seats if s["director"]]
    if len(directors) != 1:
        raise ConfigError("exactly one seat must have director = true "
                          + (f"(found {', '.join(directors)})" if directors else "(found none)"))
    return seats


TRACKER_FIELDS = {
    "load": {"queue_per_core": 2.5, "hold_per_core": 3.5},
    "ram": {"queue_base_gb": 6.0, "queue_per_worker_gb": 0.5, "hold_gb": 4.0},
    "disk": {"path": "", "wsl_source": "path", "queue_gb": 75.0, "hold_gb": 25.0},
    "vram": {"fallback_pct": 80.0, "queue_pct": 0.0, "hold_pct": 0.0},
    "wsl": {"distro": "", "queue_mem_pct": 0.0, "hold_mem_pct": 0.0},
}
WSL_SOURCES = ("path", "vhdx", "home")


def _tracker(raw, index):
    path = ["trackers", index]
    if not isinstance(raw, dict):
        raise ConfigError(f"{_where(path)} must be a table")
    kind = _choice(raw.get("kind"), TRACKER_KINDS, path + ["kind"]) if "kind" in raw else None
    if kind is None:
        raise ConfigError(f"{_where(path)} needs kind = one of {', '.join(TRACKER_KINDS)}")
    fields = TRACKER_FIELDS[kind]
    _unknown(raw, {"kind", "name", "enabled", *fields}, path)
    out = {"kind": kind, "name": _scalar(raw.get("name", kind), str, path + ["name"]),
           "enabled": _scalar(raw.get("enabled", True), bool, path + ["enabled"])}
    for key, default in fields.items():
        out[key] = _scalar(raw.get(key, default), type(default), path + [key])
        if isinstance(default, float) and out[key] < 0:
            raise ConfigError(f"{_where(path + [key])} must not be negative, not {out[key]!r}")
    if kind == "disk":
        _choice(out["wsl_source"], WSL_SOURCES, path + ["wsl_source"])
        if out["hold_gb"] > out["queue_gb"]:
            raise ConfigError(f"tracker {out['name']!r}: hold_gb ({out['hold_gb']:g}) must not exceed queue_gb "
                              f"({out['queue_gb']:g})")
    if kind == "load" and out["hold_per_core"] < out["queue_per_core"]:
        raise ConfigError(f"tracker {out['name']!r}: hold_per_core must be at least queue_per_core")
    if kind == "ram" and out["hold_gb"] > out["queue_base_gb"]:
        raise ConfigError(f"tracker {out['name']!r}: hold_gb must not exceed queue_base_gb")
    if kind in ("vram", "wsl"):
        for lo, hi in (("queue_pct", "hold_pct"), ("queue_mem_pct", "hold_mem_pct")):
            if lo in out:
                _range(out[lo], 0, 100, path + [lo])
                _range(out[hi], 0, 100, path + [hi])
                if out[lo] and out[hi] and out[hi] < out[lo]:
                    raise ConfigError(f"tracker {out['name']!r}: {hi} must be at least {lo}")
    if kind == "vram":
        _range(out["fallback_pct"], 0, 100, path + ["fallback_pct"])
    return out


def _trackers(raw_trackers):
    if not isinstance(raw_trackers, list):
        raise ConfigError("trackers must be a list of [[trackers]] tables")
    merged = {t["name"]: dict(t) for t in DEFAULT_TRACKERS}
    for i, raw in enumerate(raw_trackers):
        name = raw.get("name", raw.get("kind")) if isinstance(raw, dict) else None
        if name in merged and raw.get("kind", merged[name]["kind"]) == merged[name]["kind"]:
            raw = {**merged[name], **raw}
        merged[name if name is not None else f"#{i}"] = raw
    out = [_tracker(t, i) for i, t in enumerate(merged.values())]
    names = [t["name"] for t in out]
    for n in names:
        if names.count(n) > 1:
            raise ConfigError(f"tracker name {n!r} is used twice")
    return out


def _legacy(raw):
    if "programs_dir" not in raw:
        return raw
    logging.getLogger(__name__).warning("programs_dir is deprecated; use repos_dir")
    raw = dict(raw)
    value = raw.pop("programs_dir")
    raw.setdefault("repos_dir", value)
    return raw


def validate(raw):
    """The full config for a merged raw dict: every default filled in, every value checked. Raises ConfigError."""
    raw = _legacy(raw)
    _unknown(raw, DEFAULTS, [])
    cfg = {"repos_dir": _scalar(raw.get("repos_dir", ""), str, ["repos_dir"])}
    cfg["config_sync"] = _check_table(raw.get("config_sync", {}), DEFAULTS["config_sync"], ["config_sync"])
    cfg["paths"] = _check_table(raw.get("paths", {}), DEFAULTS["paths"], ["paths"])
    cfg["seats"] = _seats(raw.get("seats", DEFAULT_SEATS))
    cfg["seats_policy"] = _check_table(raw.get("seats_policy", {}), DEFAULTS["seats_policy"], ["seats_policy"],
                                         {"waste_threshold_pct": (0, 100)})
    cfg["director"] = _check_table(raw.get("director", {}), DEFAULTS["director"], ["director"])
    if cfg["director"]["seat"] and cfg["director"]["seat"] != next(s["name"] for s in cfg["seats"] if s["director"]):
        raise ConfigError("director.seat must name the director seat")
    if not cfg["director"]["effort"]:
        raise ConfigError("director.effort must not be empty")
    cfg["models"] = _check_table(raw.get("models", {}), DEFAULTS["models"], ["models"])
    cfg["capacity"] = _check_table(raw.get("capacity", {}), DEFAULTS["capacity"], ["capacity"],
                                   {"parallel_cap": (1, 64)})
    cfg["trackers"] = _trackers(raw.get("trackers", []))
    cfg["policy"] = _check_table(raw.get("policy", {}), DEFAULTS["policy"], ["policy"],
                                 {"tau": (0, 1), "landing_batch_size": (1, 5)})
    cfg["decider"] = _check_table(raw.get("decider", {}), DEFAULTS["decider"], ["decider"], {"timeout_s": (0.1, 600)})
    _choice(cfg["decider"]["device"], DEVICES, ["decider", "device"])
    _choice(cfg["decider"]["mode"], ("shadow", "live"), ["decider", "mode"])
    cfg["hub"] = _check_table(raw.get("hub", {}), DEFAULTS["hub"], ["hub"], {"ui_port": (1, 65535),
                                                                             "api_port": (1, 65535)})
    _choice(cfg["hub"]["auth"]["mode"], AUTH_MODES, ["hub", "auth", "mode"])
    if cfg["hub"]["ui_port"] == cfg["hub"]["api_port"]:
        raise ConfigError("hub.ui_port and hub.api_port must differ")
    m = cfg["models"]
    for backend, ladder in m["ladders"].items():
        if not ladder:
            raise ConfigError(f"models.ladders.{backend} must not be empty")
    for name in (m["review"], m["director"]):
        if not any(name in ladder for ladder in m["ladders"].values()):
            raise ConfigError(f"model {name!r} is not on any ladder in models.ladders")
    if m["director"] not in m["ladders"]["claude"]:
        raise ConfigError(f"models.director {m['director']!r} must be on the claude ladder")
    if any(not d or d < 1 for d in cfg["policy"]["tuner_cadence_days"]) or not cfg["policy"]["tuner_cadence_days"]:
        raise ConfigError("policy.tuner_cadence_days must be a non-empty list of positive whole numbers")
    if cfg["director"]["model"] and cfg["director"]["model"] not in m["ladders"]["claude"]:
        raise ConfigError("director.model must be on the claude ladder")
    return cfg


# ---- loading --------------------------------------------------------------------------------------------------

def _read(path):
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        return None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: not valid TOML: {e}") from e
    except OSError as e:
        raise ConfigError(f"{path}: cannot read: {e}") from e


def _merge(base, over):
    out = copy.deepcopy(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _leaves(table, prefix=()):
    for key, value in table.items():
        if key in ("seats", "trackers", "ids", "ladders"):
            continue
        if isinstance(value, dict):
            yield from _leaves(value, prefix + (key,))
        else:
            yield prefix + (key,), value


def env_names():
    """{ENV_NAME: dotted.path} for every environment override, aliases included."""
    names = {"DROVER_" + "_".join(path).upper(): ".".join(path) for path, _ in _leaves(DEFAULTS)}
    names["DROVER_REPO_CONFIGS_DIR"] = "repos_dir"
    names.update(ENV_ALIASES)
    return names


def _parse_env(text, default, name):
    if isinstance(default, bool):
        low = text.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
        raise ConfigError(f"{name} must be true or false, not {text!r}")
    try:
        if isinstance(default, int):
            return int(text)
        if isinstance(default, float):
            return float(text)
    except ValueError:
        raise ConfigError(f"{name} must be a number, not {text!r}") from None
    if isinstance(default, list):
        items = [x.strip() for x in text.split(",") if x.strip()]
        return [int(x) for x in items] if default and isinstance(default[0], int) else items
    return text


def _apply_env(raw, env):
    for name, dotted in env_names().items():
        if name not in env or env[name] == "":
            continue
        parts = dotted.split(".")
        default = DEFAULTS
        for p in parts:
            default = default[p]
        target = raw
        for p in parts[:-1]:
            target = target.setdefault(p, {})
        target[parts[-1]] = _parse_env(env[name], default, name)
    return raw


def load(home_path=None, overlay=None, env=None):
    """The validated config. home_path: the directory holding config.toml (default home()); overlay: the repo-local
    file (default <checkout>/.drover/config.toml); env: the environment (default os.environ). Missing files are fine."""
    env = os.environ if env is None else env
    raw = {}
    base = Path(home_path) if home_path else home(env)
    overlay = overlay or env.get("DROVER_OVERLAY") or CHECKOUT / ".drover" / "config.toml"
    for path in (base / "config.toml", Path(overlay)):
        layer = _read(path)
        if layer is not None:
            layer = _legacy(layer)
            try:
                validate(layer)                 # each file on its own, so an error can name it
            except ConfigError as e:
                raise ConfigError(f"{path}: {e}") from e
            raw = _merge_layer(raw, layer)
    raw = _apply_env(raw, env)
    try:
        return validate(raw)
    except ConfigError as e:
        raise ConfigError(f"configuration error: {e}") from e


def _merge_layer(raw, layer):
    """Layer tables merge key by key; [[seats]] replace; [[trackers]] accumulate (merged by name at validation)."""
    layer = dict(layer)
    out = {k: v for k, v in raw.items() if k != "trackers"}
    trackers = list(raw.get("trackers") or []) + list(layer.pop("trackers", None) or [])
    out = _merge(out, layer)
    if trackers:
        out["trackers"] = trackers
    return out


_cache = {}


def get():
    """The process-wide config, loaded on first use. Tests call reset() or use()."""
    if "cfg" not in _cache:
        _cache["cfg"] = load()
    return _cache["cfg"]


def reset():
    _cache.clear()


def use(cfg):
    """Install a config for this process (tests, `drover init`)."""
    _cache["cfg"] = cfg


# ---- views used by the rest of the package ---------------------------------------------------------------------

def seat_by_name(name, cfg=None):
    return next((s for s in (cfg or get())["seats"] if s["name"] == name), None)


def director_seat(cfg=None):
    return next(s for s in (cfg or get())["seats"] if s["director"])


def director_seats(cfg=None):
    """Seats a director/planner run may use, in order: Claude seats (the director first, then by priority), then
    the other seats by priority as a last resort."""
    seats = (cfg or get())["seats"]
    claude = sorted((s for s in seats if s["backend"] == "claude"), key=lambda s: (not s["director"], s["priority"], s["name"]))
    rest = sorted((s for s in seats if s["backend"] != "claude"), key=lambda s: (s["priority"], s["name"]))
    return claude + rest


def seats_for(backend, cfg=None):
    return sorted((s for s in (cfg or get())["seats"] if s["backend"] == backend), key=lambda s: s["priority"])


def seat_env(seat):
    """Account selection for a worker or usage probe."""
    if not seat:
        return {}
    path = (seat.get("codex_home") or seat.get("config_dir")) if seat["backend"] == "codex" else seat.get("config_dir")
    path = path or ("~/.claude" if seat["backend"] == "claude" else "~/.codex")
    # Pin a Windows account before a task can move its worker to WSL. WSL seats expand ~ there.
    if seat.get("placement") == "windows":
        path = os.path.expanduser(path)
    return {"CLAUDE_CONFIG_DIR" if seat["backend"] == "claude" else "CODEX_HOME": path}


def tracker(kind, cfg=None, name=None):
    return next((t for t in (cfg or get())["trackers"] if t["kind"] == kind and name in (None, t["name"])), None)


def trackers(kind, cfg=None):
    return [t for t in (cfg or get())["trackers"] if t["kind"] == kind]


def tier_effort(model, cfg=None):
    """The configured effort for a model: its ladder position decides cheap (first), top (last) or mid."""
    cfg = cfg or get()
    for ladder in cfg["models"]["ladders"].values():
        if model in ladder:
            e = cfg["models"]["efforts"]
            return e["cheap"] if model == ladder[0] else e["top"] if model == ladder[-1] else e["mid"]
    return cfg["models"]["efforts"]["mid"]


def model_id(model, cfg=None):
    return (cfg or get())["models"]["ids"].get(model, model)


def dig(cfg, dotted):
    value = cfg
    for part in dotted.split("."):
        if isinstance(value, list):
            value = value[int(part)]
        else:
            value = value[part]
    return value


# ---- writing TOML (there is no stdlib writer) -----------------------------------------------------------------

def _str(s):
    if "\\" in s and "'" not in s and "\n" not in s:
        return "'" + s + "'"
    return json.dumps(s, ensure_ascii=False)


def _val(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return _str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_val(x) for x in v) + "]"
    raise TypeError(f"cannot write {v!r} as TOML")


def _key(k):
    return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else json.dumps(k)


def dumps(table, _path=()):
    """TOML text for a dict of scalars, lists of scalars, sub-tables and lists of tables."""
    lines, subs = [], []
    for key, value in table.items():
        if isinstance(value, dict):
            subs.append((key, value, False))
        elif isinstance(value, list) and value and all(isinstance(x, dict) for x in value):
            subs.extend((key, x, True) for x in value)
        else:
            lines.append(f"{_key(key)} = {_val(value)}")
    out = "\n".join(lines) + ("\n" if lines else "")
    for key, value, is_array in subs:
        path = _path + (_key(key),)
        header = ("[[%s]]" if is_array else "[%s]") % ".".join(path)
        body = dumps(value, path)
        out += ("\n" if out else "") + header + "\n" + body
    return out


# ---- CLI ------------------------------------------------------------------------------------------------------

def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="drover.config")
    ap.add_argument("action", nargs="?", choices=("sync",))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--reason", help="reason for a configuration sync decision")
    ap.add_argument("--show", action="store_true", help="print the effective config as TOML")
    ap.add_argument("--get", metavar="KEY", help="print one value (dotted key, e.g. paths.state_dir); the state dir "
                                                 "is resolved to a path")
    ap.add_argument("--check", metavar="FILE", help="validate a config file and exit")
    a = ap.parse_args(argv)
    try:
        if a.check:
            raw = _read(a.check)
            if raw is None:
                raise ConfigError(f"{a.check}: no such file")
            validate(raw)
            print(f"{a.check}: ok")
            return 0
        cfg = load()
        if a.action == "sync":
            from . import decisions
            try:
                result = decisions.sync(state_dir(cfg), settings=cfg["config_sync"], reason=a.reason)
            except decisions.DecisionError as exc:
                print(f"drover config sync: {exc}", file=sys.stderr)
                return 2
            print(json.dumps(result) if a.json else result.get("warning") or "Configuration synced locally.")
            return 0
        if a.get:
            value = str(state_dir(cfg)) if a.get == "paths.state_dir" else dig(cfg, a.get)
            print(json.dumps(value) if isinstance(value, (list, dict)) else str(value).lower()
                  if isinstance(value, bool) else value)
            return 0
        print(dumps(cfg), end="")
        return 0
    except (ConfigError, KeyError, IndexError, ValueError) as e:
        print(f"drover.config: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
