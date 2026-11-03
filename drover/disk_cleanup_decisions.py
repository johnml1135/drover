"""Cleanup runtime and the observe, decide, apply sequence."""
from dataclasses import dataclass
from typing import Callable

from .disk_authority import decide, decide_and_store
from .disk_evidence import registration_identity


@dataclass(frozen=True)
class CleanupRuntime:
    now: Callable
    read: Callable
    call: Callable
    toast: Callable
    pressure: Callable | None = None   # unused: cleanup no longer consults machine load
    evidence: object | None = None
    state: object | None = None


def evaluate(request, runtime):
    """Observe, decide transactionally, then ask the adapter to revalidate/apply."""
    observations = runtime.evidence.observe(request)
    answer = (decide_and_store(request, observations, runtime.state)
              if any('quarantine' in o for o in observations)
              else decide(request, observations, {}, request['now']))
    rows = []
    for row in answer['decisions']:
        if request['apply'] and row['action'] == 'candidate':
            row = runtime.evidence.apply(request, row, registration_identity(row))
        rows.append(row)
    return dict(items=rows, errors=[dict(r) for r in rows if r.get('error')],
                reclaimed_bytes=sum(r['bytes'] for r in rows if r['action'] == 'removed'),
                events=answer['events'])
