"""Owner commands over the hub's existing loopback website listener.

Run as ``python -m drover.cli approve ...``. No worker token or new credentials
are used: the hub authenticates the loopback owner request just like its website.
"""
import argparse
import json
import re
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from . import config

FILTER_KEYS = {"repo", "epic_id", "kind", "priority", "status"}


def task_filter(value):
    filters = {}
    for item in value.split(","):
        key, sep, val = item.partition("=")
        if not sep or key not in FILTER_KEYS or not val.strip():
            raise argparse.ArgumentTypeError("filter must be KEY=VALUE (repo, epic_id, kind, priority, status)")
        if key in filters and filters[key] != val:
            raise argparse.ArgumentTypeError(f"conflicting filter for {key}")
        filters[key] = val
    return filters


def parser():
    p = argparse.ArgumentParser(prog="drover")
    commands = p.add_subparsers(dest="command", required=True)
    approve = commands.add_parser("approve", help="approve proposed tasks as the owner")
    approve.add_argument("ids", nargs="*", help="individual task IDs")
    approve.add_argument("--epic", help="include proposed tasks in this epic")
    approve.add_argument("--filter", type=task_filter, action="append", default=[], metavar="KEY=VALUE",
                         help="include matching proposed tasks; repeat or use comma-separated filters")
    approve.add_argument("--from-file", type=Path, action="append", default=[], metavar="FILE",
                         help="individual IDs from a JSON array or a whitespace/comma-separated file")
    approve.add_argument("--explicit", action="store_true",
                         help="allow explicit approval only for IDs named as arguments or in files")
    approve.add_argument("--reason", help="reason recorded with the approvals")
    return p


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class OwnerClient:
    def __init__(self):
        self.base = f"http://127.0.0.1:{config.get()['hub']['ui_port']}"
        # Loopback owner traffic must never travel through a proxy or redirect.
        self.opener = build_opener(ProxyHandler({}), NoRedirects())

    def request(self, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request(self.base + "/api/v1" + path, data=data,
                          headers={"Content-Type": "application/json", "Origin": self.base})
        try:
            with self.opener.open(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as exc:
            raise ValueError(f"owner hub request failed (HTTP {exc.code}): {exc.read().decode('utf-8', 'replace')}") from exc
        except URLError as exc:
            raise ValueError(f"cannot reach the owner hub at {self.base}: {exc.reason}") from exc


def file_ids(path):
    text = path.read_text(encoding="utf-8-sig").strip()
    if text.startswith("["):
        ids = json.loads(text)
        if not isinstance(ids, list) or any(not isinstance(t, str) or not t.strip() for t in ids):
            raise ValueError(f"{path}: expected a JSON array of task IDs")
        return [tid.strip() for tid in ids]
    return [tid for tid in re.split(r"[\s,]+", text) if tid]


def approve(args, client):
    named = list(args.ids)
    for path in args.from_file:
        named.extend(file_ids(path))
    named = list(dict.fromkeys(named))
    if args.explicit and not named:
        raise ValueError("--explicit requires individual task IDs as arguments or in --from-file")
    filters = {}
    for group in args.filter:
        for key, value in group.items():
            if key in filters and filters[key] != value:
                raise ValueError(f"conflicting filter for {key}")
            filters[key] = value
    if not named and not args.epic and not filters:
        raise ValueError("provide task IDs, --epic, --filter or --from-file")
    selected, excluded = [], []
    if args.epic or filters:
        # Fetch every visible task; filtering is local so kind/priority compose.
        tasks = client.request("/tasks?status=proposed&limit=2147483647")
        for task in tasks:
            if task["status"] != "proposed":
                continue
            if args.epic and task.get("epic_id") != args.epic:
                continue
            if not all(str(task.get(k)) == v for k, v in filters.items()):
                continue
            if task.get("requires_explicit_approval") and task["id"] not in named:
                excluded.append({"id": task["id"], "result": "skipped", "reason": "requires explicit approval of this ID"})
            else:
                selected.append(task["id"])
    # Selector matches are sent without explicit opt-in. Only individually named
    # IDs go in the explicit request, protecting tasks whose flag changed meanwhile.
    selected = [tid for tid in dict.fromkeys(selected) if tid not in named]
    results = []
    if selected:
        results.extend(client.request("/tasks/approve", {"ids": selected, "reason": args.reason})["results"])
    if named:
        results.extend(client.request("/tasks/approve", {"ids": named, "reason": args.reason, "explicit": args.explicit})["results"])
    results.extend(excluded)
    return {"results": results}


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    try:
        result = approve(args, OwnerClient())
    except (ValueError, OSError) as exc:
        print(f"drover: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
