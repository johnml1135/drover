"""The owner's per-repo dispatch filter: `python -m drover.dispatch_repos show|set REPO...|clear`.

Stored in the ledger settings row `dispatch_repos` as a JSON list. Only an absent key means every
repo may dispatch (the default); an invalid value (bad JSON, a bare string, an empty list) blocks every repo. A non-empty list means only those repos may start work: claims, gates, reviews,
replan planners and landing all skip any other repo. The global `dispatch_enabled` switch still overrides it. The
setting is owner-only on the hub (the `dispatch_` prefix is barred from the token settings route), so this CLI
writes the ledger directly, the way the other owner scripts do.
"""
import argparse
import json
import logging
import sys

KEY = "dispatch_repos"
log = logging.getLogger("drover.dispatch_repos")


def _norm(name):
    return str(name).strip().casefold()


def parse(raw):
    """The stored value as a tuple of repo names. Raises ValueError for anything but a non-empty JSON list of names."""
    try:
        v = json.loads(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{KEY} is not JSON: {str(raw)[:80]!r}") from None
    if not isinstance(v, list) or not v or not all(isinstance(n, str) and n.strip() for n in v):
        raise ValueError(f"{KEY} must be a non-empty JSON list of repo names: {str(raw)[:80]!r}")
    return tuple(n.strip() for n in v)


def get(get_setting):
    """Names, or None when the key is absent (all repos). Raises ValueError when the stored value is invalid."""
    raw = get_setting(KEY, None)
    return None if raw is None else parse(raw)


def allowed(get_setting):
    """frozenset of casefolded repo names, or None when every repo may dispatch. An invalid value fails closed:
    the empty set permits no repo, and an error is logged on each call."""
    try:
        names = get(get_setting)
    except ValueError as e:
        log.error("dispatch blocked for every repo: %s (fix it with `python -m drover.dispatch_repos set|clear`)", e)
        return frozenset()
    return None if names is None else frozenset(_norm(n) for n in names)


def permits(allow, repo):
    return allow is None or _norm(repo or "") in allow


def store(conn, names, actor="owner"):
    from .ledger import ops
    names = list(dict.fromkeys(str(n).strip() for n in names if str(n).strip()))
    if not names:
        raise ValueError("no repo names; use clear to remove the filter")
    ops.set_setting(conn, KEY, json.dumps(names))
    ops.event(conn, "dispatch.repos", actor, {"repos": names})
    return names


def clear(conn, actor="owner"):
    """Delete the key: the only way back to all repos."""
    from .ledger import ops, tx
    with tx(conn):
        conn.execute("DELETE FROM settings WHERE key=?", (KEY,))
    ops.event(conn, "dispatch.repos", actor, {"repos": None})


def main(argv=None, out=None, err=None):
    from .ledger import connect, default_path, ops
    out, err = out or sys.stdout, err or sys.stderr
    ap = argparse.ArgumentParser(prog="drover.dispatch_repos")
    ap.add_argument("--db", help="ledger path (default: the hub's ledger)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show")
    sub.add_parser("clear")
    s = sub.add_parser("set")
    s.add_argument("repos", nargs="+")
    s.add_argument("--force", action="store_true", help="accept names that are not configured repos")
    a = ap.parse_args(argv)
    conn = connect(a.db or default_path())
    try:
        if a.cmd == "set":
            from . import repos
            try:
                known = set(repos.load_all())
            except Exception:
                known = set()
            unknown = [n for n in a.repos if known and n.strip().lower() not in known]
            if unknown and not a.force:
                print(f"dispatch_repos: unknown repo(s) {', '.join(unknown)}; known: {', '.join(sorted(known))} "
                      "(--force to set anyway)", file=err)
                return 2
            try:
                names = store(conn, a.repos)
            except ValueError as e:
                print(f"dispatch_repos: {e}", file=err)
                return 2
        elif a.cmd == "clear":
            clear(conn)
            names = None
        else:
            try:
                names = get(lambda k, d=None: ops.get_setting(conn, k, d))
            except ValueError as e:
                print(f"dispatch_repos: INVALID, all repos are blocked: {e}", file=out)
                return 1
        print(f"dispatch_repos: {', '.join(names) if names else 'all repos'}", file=out)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
