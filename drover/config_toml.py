"""TOML serialization for configuration."""
import json
import re

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
