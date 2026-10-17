"""Conservative glob-overlap test for scope_files: false positives are fine, false negatives are not."""
from fnmatch import fnmatchcase

_WILD = "*?["


def _prefix(g):
    for i, ch in enumerate(g):
        if ch in _WILD:
            return g[:i]
    return g


def _literal(g):
    return not any(ch in g for ch in _WILD)


def overlap(a, b):
    a, b = a.strip().lstrip("./"), b.strip().lstrip("./")
    if a == b:
        return True
    la, lb = _literal(a), _literal(b)
    if la and lb:
        return a.startswith(b.rstrip("/") + "/") or b.startswith(a.rstrip("/") + "/")
    if la or lb:
        lit, g = (a, b) if la else (b, a)
        return fnmatchcase(lit, g) or _prefix(g).startswith(lit.rstrip("/") + "/")
    pa, pb = _prefix(a), _prefix(b)
    return pa.startswith(pb) or pb.startswith(pa)


def any_overlap(globs_a, globs_b):
    return any(overlap(a, b) for a in globs_a for b in globs_b)
