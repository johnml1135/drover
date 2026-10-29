"""Shared waiting identities and constants."""
import json

from .. import placement

TASK_TERMINAL = 'task-terminal'
OWNER_ANSWER = 'owner-answer'
BASE = 'base'
REPLAN = 'replan'
DEADLINE = 'deadline'
MANUAL = 'manual'
OPEN, AUTHORIZED, ATTEMPTING, RESOLVED, CLOSED = 'open', 'authorized', 'attempting', 'resolved', 'closed'

from ..contract import REPLAN_QUEUED as QUEUED, REPLAN_RUNNING as RUNNING, REPLAN_OWNER as OWNER, REPLAN_DONE as DONE


def cause_key(*parts: str | int) -> str:
    if not parts or any(type(p) not in (str, int) or p == '' for p in parts):
        raise ValueError('identity requires nonempty string/integer parts')
    return json.dumps(parts, ensure_ascii=True, separators=(',', ':'))


def _identity(key):
    try:
        parts = json.loads(key)
        if not isinstance(parts, list) or cause_key(*parts) != key:
            raise ValueError('noncanonical identity')
        return parts
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError('invalid identity') from exc


def repo_key(name, checkout=None) -> str:
    if name and name.strip():
        return name.strip().casefold()
    if checkout is None:
        raise ValueError('unnamed repository needs a checkout')
    return cause_key(*placement.path_parts(placement.config_path(checkout, checkout)))


class InvalidSplit(ValueError):
    """A proposed split graph is invalid before materialization."""
