"""The owner's per-repo dispatch filter: `python -m drover.dispatch_repos show|set REPO...|clear`.

Stored in the ledger settings row `dispatch_repos` as a JSON list. Unset, empty or unparsable-as-a-list means every
repo may dispatch (the default). A non-empty list means only those repos may start work: claims, gates, reviews,
replan planners and landing all skip any other repo. The global `dispatch_enabled` switch still overrides it. The
setting is owner-only on the hub (the `dispatch_` prefix is barred from the token settings route), so this CLI
writes the ledger directly, the way the other owner scripts do.
"""
import argparse
import json
import sys

KEY = "dispatch_repos"


def _norm(name):
    return str(name).strip().casefold()


def parse(raw):
    """The stored value as a tuple of repo names; () means no filter."""
    try:
        v = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return ()
    return tuple(str(n).strip() for n in v if str(n).strip()) if isinstance(v, list) else ()


def get(get_setting):
    """Names from any `get_setting(key, default)` callable; () when unfiltered."""
    return parse(get_setting(KEY, None))


def allowed(get_setting):
    """frozenset of casefolded repo names, or None when every repo may dispatch."""
    names = get(get_setting)
    return frozenset(_norm(n) for n in names) if names else None


def permits(allow, repo):
    return allow is None or _norm(repo or "") in allow


def store(conn, names, actor="owner"):
    from .ledger import ops
    names = list(dict.fromkeys(str(n).strip() for n in names if str(n).strip()))
    ops.set_setting(conn, KEY, json.dumps(names))
    ops.event(conn, "dispatch.repos", actor, {"repos": names})
    return names


def main(argv=None, out=None):
    from .ledger import connect, default_path, ops
    out = out or sys.stdout
    ap = argparse.ArgumentParser(prog="drover.dispatch_repos")
    ap.add_argument("--db", help="ledger path (default: the hub's ledger)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show")
    sub.add_parser("clear")
    s = sub.add_parser("set")
    s.add_argument("repos", nargs="+")
    a = ap.parse_args(argv)
    conn = connect(a.db or default_path())
    try:
        if a.cmd == "set":
            names = store(conn, a.repos)
        elif a.cmd == "clear":
            names = store(conn, [])
        else:
            names = list(get(lambda k, d=None: ops.get_setting(conn, k, d)))
        print(f"dispatch_repos: {', '.join(names) if names else 'all repos'}", file=out)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
