"""Create a linked checkout with explicit leased ownership."""
import argparse
import datetime
import math
import subprocess
import sys
from pathlib import Path

from . import ownership, placement


def add(repo: str, path: str, ref: str, *, owner: str, lease_until: float) -> str:
    if not all(isinstance(v, str) and v.strip() and '\x00' not in v for v in (repo, path, ref, owner)):
        raise ValueError('repo, path, ref and owner must be nonempty')
    if type(lease_until) not in (int, float) or not math.isfinite(lease_until):
        raise ValueError('lease must be a finite timestamp')
    repo, path = placement.canonical(repo), placement.canonical(path)
    placement.add_worktree(repo, path, '--', path, ref)
    try:
        ownership.tag(path, Path(placement.native(repo)).name, owner, lease_until)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise RuntimeError(f'created {path} but could not record ownership: {error}') from error
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    create = sub.add_parser('add')
    create.add_argument('path')
    create.add_argument('ref')
    create.add_argument('--repo', required=True)
    create.add_argument('--owner', required=True)
    create.add_argument('--lease-until', required=True)
    args = parser.parse_args(argv)
    try:
        until = datetime.datetime.fromisoformat(args.lease_until.replace('Z', '+00:00'))
        if until.tzinfo is None:
            raise ValueError('lease requires a timezone')
        value = add(args.repo, args.path, args.ref, owner=args.owner, lease_until=until.timestamp())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(value)
    return 0
