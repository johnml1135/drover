"""Configuration value validation."""
import math

from . import placement
from .config_defaults import (BACKENDS, PLACEMENTS, TRACKER_KINDS, NAME_RE, SEAT_DEFAULTS,
                              DEFAULT_TRACKERS, DEFAULTS, ConfigError)

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
    "disk": {"path": "", "warn_gb": 150.0, "queue_gb": 100.0, "hold_gb": 50.0, "critical_gb": 25.0,
             "wsl_combined": False, "host_floor_gb": 10.0},
    "vram": {"fallback_pct": 80.0, "queue_pct": 0.0, "hold_pct": 0.0},
    "wsl": {"distro": "", "queue_mem_pct": 0.0, "hold_mem_pct": 0.0},
}
def _disk_levels(path):
    from . import placement
    return (60.0, 40.0, 25.0, 15.0) if placement.drive(path) == "C" else (150.0, 100.0, 50.0, 25.0)


DISK_LEVEL_KEYS = ("warn_gb", "queue_gb", "hold_gb", "critical_gb")


def _tracker(raw, index):
    path = ["trackers", index]
    if not isinstance(raw, dict):
        raise ConfigError(f"{_where(path)} must be a table")
    kind = _choice(raw.get("kind"), TRACKER_KINDS, path + ["kind"]) if "kind" in raw else None
    if kind is None:
        raise ConfigError(f"{_where(path)} needs kind = one of {', '.join(TRACKER_KINDS)}")
    fields = TRACKER_FIELDS[kind]
    if kind == "disk":
        if "wsl_source" in raw:
            raise ConfigError("legacy disk wsl_source: run python -m drover.config migrate-machine")
        fields = {**fields, **dict(zip(DISK_LEVEL_KEYS, _disk_levels(raw.get("path", ""))))}
    _unknown(raw, {"kind", "name", "enabled", *fields}, path)
    out = {"kind": kind, "name": _scalar(raw.get("name", kind), str, path + ["name"]),
           "enabled": _scalar(raw.get("enabled", True), bool, path + ["enabled"])}
    for key, default in fields.items():
        out[key] = _scalar(raw.get(key, default), type(default), path + [key])
        if isinstance(default, float) and out[key] < 0:
            raise ConfigError(f"{_where(path + [key])} must not be negative, not {out[key]!r}")
    if kind == "disk":
        if not math.isfinite(out["host_floor_gb"]):
            raise ConfigError(f"tracker {out['name']!r}: host_floor_gb must be finite")
        values = [out[key] for key in DISK_LEVEL_KEYS]
        if not all(math.isfinite(v) and v >= 0 for v in values):
            raise ConfigError(f"tracker {out['name']!r}: disk levels must be finite and nonnegative")
        if values != sorted(values, reverse=True):
            raise ConfigError(f"tracker {out['name']!r}: critical_gb must not exceed hold_gb; hold_gb must not exceed queue_gb; queue_gb must not exceed warn_gb")
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

def _absolute_machine_path(value):
    from pathlib import PurePosixPath, PureWindowsPath
    return isinstance(value, str) and bool(value) and "\x00" not in value and (
        PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute())


def _cleanup_policy(raw):
    defaults = DEFAULTS["disk_cleanup"]
    _unknown(raw, defaults, ["disk_cleanup"])
    out = {}
    for key in ('worker_timeout_s', 'process_query_timeout_s', 'temp_root_budget_s', 'cooldown_s'):
        timeout = raw.get(key, defaults[key])
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ConfigError(f"disk_cleanup.{key} must be finite and positive")
        out[key] = timeout
    out['process_ignore'] = _strs(raw.get('process_ignore', defaults['process_ignore']), ['disk_cleanup', 'process_ignore'])
    if any(not name or any(char in name for char in '/\\\0') for name in out['process_ignore']):
        raise ConfigError('disk_cleanup.process_ignore must contain image basenames')
    for key in ("temp_roots", "report_roots"):
        paths = _strs(raw.get(key, []), ["disk_cleanup", key])
        if not all(_absolute_machine_path(path) for path in paths):
            raise ConfigError(f"disk_cleanup.{key} must contain absolute machine paths")
        out[key] = paths
    caches = raw.get("caches", [])
    if not isinstance(caches, list):
        raise ConfigError("disk_cleanup.caches must be a list")
    out["caches"] = []
    for i, entry in enumerate(caches):
        path = ["disk_cleanup", "caches", i]
        folder = "path" in entry
        _unknown(entry, {"path", "placement", "max_age_days"} if folder else {"argv", "cwd", "placement"}, path)
        side = _choice(entry.get("placement"), PLACEMENTS, path + ["placement"])
        if folder:
            if not _absolute_machine_path(entry["path"]):
                raise ConfigError(f"{_where(path)}.path must be absolute")
            age = entry.get("max_age_days", 30)
            if type(age) not in (int, float) or not math.isfinite(age) or age <= 0:
                raise ConfigError(f"{_where(path)}.max_age_days must be finite and positive")
            out["caches"].append(dict(path=entry["path"], placement=side, max_age_days=age))
        else:
            argv = _strs(entry.get("argv"), path + ["argv"])
            if not argv or not all(arg and "\x00" not in arg for arg in argv):
                raise ConfigError(f"{_where(path)}.argv must contain nonempty literal arguments")
            if not _absolute_machine_path(entry.get("cwd")):
                raise ConfigError(f"{_where(path)}.cwd must be absolute")
            out["caches"].append(dict(argv=argv, cwd=entry["cwd"], placement=side))
    return out


def _wsl_worktrees(value):
    """WSL worktrees live on the distro's own ext4 (ADR 0007): a Linux path, never a Windows drive or /mnt/<drive>."""
    if not (value == "~" or value.startswith(("~/", "/"))) or placement.MNT_RE.match(value) \
            or value.startswith("//") or ".." in value.replace("\\", "/").split("/"):
        raise ConfigError(f"paths.wsl_worktrees must be a Linux path under the distro's own filesystem "
                          f"(~/work or /abs/path), not {value!r}; /mnt/<drive> is refused")
