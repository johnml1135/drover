"""Drover configuration: one TOML file, defaults that reproduce the built-in behaviour, validation with clear errors.

    python -m drover.config [--show] [--get paths.state_dir] [--check FILE]

Layers, lowest first: built-in DEFAULTS, <home>/config.toml, then recognized DROVER_* environment overrides.
The home is $DROVER_HOME or ~/.drover (%USERPROFILE%\\.drover on Windows).
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
import math
import tempfile
import os
import re
import sys
import tomllib
from pathlib import Path

from . import placement
from .config_defaults import (BACKENDS, PLACEMENTS, TRACKER_KINDS, DEVICES, AUTH_MODES, SIZES,
                              NAME_RE, ENV_ALIASES, DEFAULT_SEATS, SEAT_DEFAULTS, DEFAULT_TRACKERS,
                              DEFAULTS, RESERVED, ConfigError)
from .config_validation import (_where, _type_name, _scalar, _range, _choice, _strs, _unknown,
                                _check_table, _check_flat_map, _check_list, _seat, _seats,
                                TRACKER_FIELDS, _disk_levels, DISK_LEVEL_KEYS, _tracker, _trackers,
                                _absolute_machine_path, _cleanup_policy, _wsl_worktrees)
from .config_sources import (_read, _merge, _leaves, env_names, _parse_env, _apply_env,
                             _source_leaves, _selector_parts, dig, explain_value)
from .config_toml import _str, _val, _key, dumps
from .config_paths import home, seat_env

from .placement import native, default_disk_trackers


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
    _wsl_worktrees(cfg["paths"]["wsl_worktrees"])
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
    cfg["disk_cleanup"] = _cleanup_policy(raw.get("disk_cleanup", {}))
    folders = [c['path'] for c in cfg['disk_cleanup']['caches'] if 'path' in c]
    if folders:
        from . import placement, repos
        from .disk_paths import repository_tree
        protected = [str(state_dir(cfg)), cfg['paths']['repos_root'], cfg['paths']['worktrees']]
        try:
            for machine_file in repos_dir(cfg).glob('*.toml'):
                machine = repos.read_machine(machine_file)
                protected.extend(machine.get(k) for k in ('repo', 'worktrees'))
                if machine.get('repo') and not machine.get('worktrees') and not cfg['paths']['worktrees']:
                    protected.append(machine['repo'].rstrip('\\/') + '.worktrees')
            for folder in folders:
                concrete = Path(placement.native(folder))
                if repository_tree(concrete):
                    raise ConfigError('folder cache overlaps protected repository')
                parts = placement.cleanup_parts(folder)
                if any(part in ('.git', '.venv') for part in parts):
                    raise ConfigError('folder cache overlaps protected path')
                for root in filter(None, protected):
                    other = placement.cleanup_parts(root)
                    if parts[:len(other)] == other or other[:len(parts)] == parts:
                        raise ConfigError('folder cache overlaps protected repository, worktree or state path')
        except (OSError, ValueError) as error:
            raise ConfigError(str(error)) from error
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


def load(home_path=None, env=None, *, sources=None):
    """Load machine defaults, the state config.toml, and recognized environment values."""
    env = os.environ if env is None else env
    base = Path(home_path) if home_path else home(env)
    path = base / "config.toml"
    raw = _read(path)
    raw = _legacy(raw) if raw is not None else {}
    try:
        validate(raw)
    except ConfigError as e:
        raise ConfigError(f"{path}: {e}") from e
    label = "machine:" + str(path.resolve())
    origins = _source_leaves(raw, label)
    raw = _apply_env(raw, env, sources=origins)
    try:
        cfg = validate(raw)
    except ConfigError as e:
        raise ConfigError(f"configuration error: {e}") from e

    normalized = _source_leaves(cfg, "default")
    for key in normalized:
        if key in origins and not key.startswith("trackers."):
            normalized[key] = origins[key]
    # Tracker input merges by name before its normalized list receives indices.
    supplied = {}
    kinds = {t["name"]: t["kind"] for t in DEFAULT_TRACKERS}
    for tracker in raw.get("trackers", []):
        name = tracker.get("name", tracker.get("kind"))
        kind = tracker.get("kind", kinds.get(name))
        previous = supplied.get(name, {}) if kind == kinds.get(name) else {}
        supplied[name] = {**previous, **tracker}
        kinds[name] = kind
    for name, tracker in supplied.items():
        index = next(i for i, item in enumerate(cfg["trackers"]) if item["name"] == name)
        normalized.update(_source_leaves(tracker, label, ("trackers", str(index))))
    for i, seat in enumerate(raw.get("seats", DEFAULT_SEATS)):
        for field, dependencies in (("placement", ("backend",)), ("binary", ("backend",)),
                                    ("director_reserve_pct", ("backend", "director"))):
            if field not in seat:
                deps = {f"seats.{i}.{key}": normalized[f"seats.{i}.{key}"] for key in dependencies}
                normalized[f"seats.{i}.{field}"] = "derived:" + json.dumps(deps, sort_keys=True, separators=(",", ":"))
    if sources is not None:
        sources.clear()
        sources.update(normalized)
    return cfg


_cache = {}


def get():
    """The process-wide config, loaded on first use. Tests call reset() or use()."""
    if "cfg" not in _cache:
        origins = {}
        cfg = load(sources=origins)
        _cache.update(cfg=cfg, sources=origins)
    return _cache["cfg"]


def reset():
    _cache.clear()


def use(cfg, *, sources=None):
    """Install a config for this process (tests, `drover init`)."""
    _cache.update(cfg=cfg, sources=dict(sources) if sources is not None else _source_leaves(cfg, "injected"))


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


def source_of(key):
    cfg = get()
    result = explain_value(cfg, key, _cache["sources"])
    origins = result["sources"]
    if key in origins and len(origins) == 1:
        return origins[key]
    return "aggregate:" + json.dumps(origins, sort_keys=True, separators=(",", ":"))


def migrate_disk_config(raw):
    """Return validated proposed machine facts without changing the input."""
    from . import placement
    proposed = copy.deepcopy(raw)
    rows = []
    trackers = proposed.get("trackers", [])
    if not isinstance(trackers, list) or not all(isinstance(t, dict) for t in trackers):
        raise ConfigError("trackers must be a list of tables")
    for tracker in trackers:
        if tracker.get("kind") != "disk":
            rows.append(tracker)
            continue
        source = tracker.pop("wsl_source", None)
        if source is not None:
            _choice(source, ("home", "path", "vhdx"), ["trackers", "wsl_source"])
        for key in DISK_LEVEL_KEYS:
            if key in tracker:
                value = _scalar(tracker[key], float, ["trackers", key])
                if not math.isfinite(value) or value < 0:
                    raise ConfigError(f"disk {key} must be finite and nonnegative")
        if source == "home":
            continue
        path = tracker.get("path", "")
        if source == "vhdx" and not placement.drive(path):
            host = placement.physical_disk_path("/")
            if not host or not placement.drive(host):
                raise ConfigError("cannot resolve VHDX host drive; migration refused")
            tracker["path"] = path = host
        defaults = dict(zip(DISK_LEVEL_KEYS, _disk_levels(path)))
        old_pair = (50, 25) if placement.drive(path) == "C" else (75, 25)
        if source is not None and placement.drive(path) in ("H", "C") and \
                (tracker.get("queue_gb"), tracker.get("hold_gb")) == old_pair:
            tracker.update({key: value for key, value in defaults.items()
                            if key in ("queue_gb", "hold_gb") or key not in tracker})
        else:
            tracker.setdefault("queue_gb", defaults["queue_gb"])
            tracker.setdefault("hold_gb", defaults["hold_gb"])
            tracker.setdefault("warn_gb", max(defaults["warn_gb"], tracker["queue_gb"]))
            tracker.setdefault("critical_gb", min(defaults["critical_gb"], tracker["hold_gb"]))
        rows.append(tracker)
    if "trackers" in proposed:
        proposed["trackers"] = rows
    if "disk_cleanup" not in proposed:
        proposed["disk_cleanup"] = placement.detected_cleanup_policy(state_dir(validate(proposed)))
    placement.seed_cleanup_process_policy(proposed['disk_cleanup'])
    validate(proposed)
    return proposed


def _migrate_disks(apply):
    from datetime import datetime, timezone
    path = home() / "config.toml"
    original = path.read_bytes()
    raw = tomllib.loads(original.decode("utf-8"))
    proposed = migrate_disk_config(raw)
    print(dumps(proposed), end="")
    if proposed == raw:
        print("Machine disk configuration is already current.")
    elif apply:
        backup = path.with_name(path.name + ".bak-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
        with backup.open("xb") as stream:
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(dumps(proposed))
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        load(path.parent)
        print(f"Backup: {backup}\nApplied machine migration.")
    else:
        print("Dry run; use --apply to back up and write this configuration.")
    return 0


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="drover.config")
    ap.add_argument("action", nargs="?", choices=("sync", "explain", "migrate-registration", "migrate-machine"))
    ap.add_argument("key", nargs="?")
    ap.add_argument("--apply", action="store_true", help="apply registration migration after backing up the file")
    ap.add_argument("--repo", metavar="NAME")
    ap.add_argument("--revision", metavar="REV")
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
        if a.action == "migrate-machine" and not a.key:
            if a.repo or a.revision is not None:
                raise ConfigError("global migrate-machine does not accept repository selectors")
            return _migrate_disks(a.apply)
        origins = {}
        cfg = load(sources=origins)
        if a.action == "migrate-machine":
            print("migrate-machine is deprecated; use migrate-registration.", file=sys.stderr)
            a.action = "migrate-registration"
        if a.action == "migrate-registration":
            from datetime import datetime, timezone
            from . import repos
            if not a.key or a.repo:
                raise ConfigError("migrate-registration requires a repository name")
            path = next((p for p in sorted(repos_dir(cfg).glob("*.toml"))
                         if p.stem.lower() == a.key.lower()), None)
            if path is None:
                raise ConfigError(f"no repo {a.key!r}")
            original = path.read_bytes()
            raw = tomllib.loads(original.decode("utf-8"))
            # The loader remains the sole reader/validator of committed policy.
            facts = {k: v for k, v in raw.items()
                     if k in ("name", "repo", "worktrees", "remote", "default_branch", "budget_gb")}
            if "run" in raw and not isinstance(raw["run"], dict):
                raise ConfigError(f"{path}: run must be a table")
            if "env" in raw.get("run", {}):
                facts["run"] = {"env": raw["run"]["env"]}
            facts.setdefault("name", path.stem)
            use(cfg, sources=origins)
            branch = facts.get("default_branch", "main")
            if not isinstance(branch, str) or not branch.strip():
                raise ConfigError("default_branch must be a non-empty git branch name")
            revision = a.revision or "refs/heads/" + branch.removeprefix("refs/heads/")
            committed = repos.load(facts, revision=revision, machine_path=path, sources=origins)
            if not committed["_adopted"]:
                raise ConfigError("commit .drover/config.toml before migrating the repo registration")
            print(f"Registration facts for {path}:\n{dumps(facts)}", end="")
            print("Policy belongs in committed .drover/config.toml:")
            differences = []
            for table, value in raw.items():
                if table in facts and table != "run":
                    continue
                for field, supplied in (value.items() if isinstance(value, dict) and value else [(None, value)]):
                    key = f"{table}.{field}" if field is not None else table
                    if key == "run.env":
                        continue
                    if key == "gate.commands":
                        print("  gate.commands: obsolete, ignored by the loader; commands now come from scripts.test")
                        continue
                    canonical = "landing.push" if key == "land.push" else key
                    try:
                        actual = dig(committed, canonical)
                    except ConfigError:
                        differences.append(f"{key}: registration={supplied!r}, committed=<absent>")
                        continue
                    # Placement sets have a canonical order in the loader.
                    expected = [p for p in repos.PLATFORMS if p in supplied] if key == "run.platforms" \
                        and isinstance(supplied, list) and supplied and all(p in repos.PLATFORMS for p in supplied) else supplied
                    if type(expected) in (int, float) and type(actual) in (int, float):
                        same = expected == actual
                    else:
                        same = type(expected) is type(actual) and expected == actual
                    if not same:
                        differences.append(f"{key}: registration={supplied!r}, committed={actual!r}")
                    else:
                        source = origins.get(canonical, "")
                        present = source.startswith("repo:") or any(
                            label.startswith("repo:") for selector, label in origins.items()
                            if selector.startswith(canonical + "."))
                        print(f"  {key}: {supplied!r} (" +
                              ("already in committed entry" if present else "matches repository default") + ")")
            if differences:
                raise ConfigError("policy differs; refusing migration:\n" + "\n".join(differences))
            if raw == facts:
                print("Repo registration is already facts-only.")
            elif a.apply:
                backup = path.with_name(path.name + ".bak-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
                with backup.open("xb") as stream:
                    stream.write(original)
                path.write_text(dumps(facts), encoding="utf-8")
                print(f"Backup: {backup}\nApplied registration migration.")
            else:
                print("Dry run; use --apply to back up and write the facts-only file.")
            return 0
        if a.apply:
            raise ConfigError("--apply requires migrate-registration")
        if a.action == "explain":
            if not a.key:
                raise ConfigError("explain requires a key")
            if a.revision is not None and not a.repo:
                raise ConfigError("revision requires repository scope (--repo)")
            if a.repo:
                from . import repos
                path = next((p for p in sorted(repos_dir(cfg).glob("*.toml"))
                             if p.stem.lower() == a.repo.lower()), None)
                if path is None:
                    raise ConfigError(f"no repo {a.repo!r}")
                try:
                    raw = repos.read_machine(path)
                except (OSError, tomllib.TOMLDecodeError) as e:
                    raise ConfigError(f"{path}: {e}") from e
                use(cfg, sources=origins)
                raw.setdefault("name", path.stem)
                cfg = repos.load(raw, a.revision or "HEAD", machine_path=path, sources=origins)
            print(json.dumps(explain_value(cfg, a.key, origins)))
            return 0
        if a.repo or a.revision is not None or a.key:
            raise ConfigError("key, --repo and --revision require explain")
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
    except (ConfigError, KeyError, IndexError, ValueError, OSError) as e:
        print(f"drover.config: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
