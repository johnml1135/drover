"""Leased worktree ownership stored outside tracked files through Placement."""
import json
import math
import time

from . import placement

TAG = 'drover-tag.json'


def _time(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def read(worktree: str) -> dict | None:
    """Validated ownership facts; v1 tags remain lease-less historical identity."""
    try:
        record = json.loads(placement.worktree_metadata(worktree, TAG) or 'null')
    except (ValueError, TypeError):
        return None
    if not isinstance(record, dict) or type(record.get('v')) is not int or record['v'] not in (1, 2):
        return None
    if not all(isinstance(record.get(k), str) and record[k].strip() for k in ('repo', 'owner')) \
            or not _time(record.get('created')):
        return None
    facts = {key: record[key] for key in ('v', 'repo', 'owner', 'created')}
    if record['v'] == 2:
        if not _time(record.get('lease_until')):
            return None
        facts['lease_until'] = record['lease_until']
    return facts


def tag(worktree: str, repo: str, owner: str, lease_until: float, *, now: float | None = None) -> dict:
    """Create or renew ownership; an invalid tag never overwrites existing metadata."""
    now = time.time() if now is None else now
    if not all(isinstance(v, str) and v.strip() for v in (repo, owner)) \
            or not _time(now) or not _time(lease_until):
        raise ValueError('ownership requires repo, owner and finite timestamps')
    previous = read(worktree)
    created = previous['created'] if previous and previous['repo'] == repo and previous['owner'] == owner else now
    record = dict(v=2, repo=repo, owner=owner, created=created, lease_until=lease_until)
    placement.worktree_metadata(worktree, TAG, json.dumps(record, sort_keys=True))
    return record


def owned_by(worktree: str, owner: str) -> bool:
    """Identity allows unfinished task reuse even after its lease has expired."""
    record = read(worktree)
    return record is not None and record['owner'] == owner
