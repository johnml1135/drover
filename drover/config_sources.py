"""Configuration file, environment, and source selectors."""
import copy
import tomllib

from .config_defaults import ConfigError, DEFAULTS, ENV_ALIASES

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


def _apply_env(raw, env, *, sources=None):
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
        if sources is not None:
            sources.update(_source_leaves(target[parts[-1]], "env:" + name, tuple(parts)))
    return raw

def _source_leaves(value, label, prefix=()):
    """Terminal selectors, including empty containers, with escaped literal components."""
    if isinstance(value, (dict, list)) and value:
        out = {}
        items = value.items() if isinstance(value, dict) else enumerate(value)
        for key, child in items:
            if not prefix and str(key).startswith("_"):
                continue
            out.update(_source_leaves(child, label, prefix + (str(key),)))
        return out
    selector = ".".join(str(p).replace("\\", "\\\\").replace(".", "\\.") for p in prefix)
    return {selector: label}


def _selector_parts(key):
    """Parse a selector; only dots and backslashes may be escaped."""
    if not isinstance(key, str) or not key:
        raise ConfigError(f"invalid selector {key}")
    parts, part, escaped = [], "", False
    for char in key:
        if escaped:
            if char not in (".", "\\"):
                raise ConfigError(f"invalid selector {key}")
            part += char
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ".":
            if not part:
                raise ConfigError(f"invalid selector {key}")
            parts.append(part)
            part = ""
        else:
            part += char
    if escaped or not part:
        raise ConfigError(f"invalid selector {key}")
    parts.append(part)
    if parts[0].startswith("_"):
        raise ConfigError(f"unknown selector {key}")
    return parts


def dig(cfg, dotted):
    value = cfg
    try:
        for part in _selector_parts(dotted):
            if isinstance(value, list):
                if not part.isascii() or not part.isdigit() or str(int(part)) != part:
                    raise ValueError(part)
                value = value[int(part)]
            elif isinstance(value, dict):
                value = value[part]
            else:
                raise KeyError(part)
    except (KeyError, IndexError, ValueError, TypeError):
        raise ConfigError(f"unknown or invalid selector {dotted}") from None
    return value


def explain_value(cfg, key, sources):
    value = dig(cfg, key)
    terminals = _source_leaves(value, "default", tuple(_selector_parts(key)))
    return {"key": key, "value": value,
            "sources": {path: sources.get(path, "default") for path in terminals}}
