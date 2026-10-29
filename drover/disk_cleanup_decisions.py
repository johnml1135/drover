"""Cleanup decisions and evidence adapters."""
import copy
from dataclasses import dataclass
from typing import Callable

from . import disk_state


@dataclass(frozen=True)
class CleanupRuntime:
    now: Callable
    read: Callable
    call: Callable
    toast: Callable
    pressure: Callable | None = None   # () -> {'verdict': go|queue|hold, 'reasons': [...]}; hold aborts the run
    evidence: object | None = None
    state: object | None = None


def decide(request, observations, previous_state, now):
    """Pure selection and grace transitions; candidates are never deletion authority.

    Observations contain a report row and optional settlement or quarantine facts.
    Evidence adapters own path/process/Git probes. Only their apply method may
    mutate a checkout, after obtaining fresh evidence and durably journaling it.
    Checks are semantic retention facts; settlement supplies task/ancestry facts.
    Quarantine supplies a canonical key and activity identity. approved_match is
    canonical path equality computed by the adapter, without path probes here.
    Validation supplies fresh identity, nesting and ignored-content facts.
    Neither observations nor previous_state are changed by this function.
    """
    state = copy.deepcopy(previous_state)
    events, decisions = [], []
    for observed in observations:
        row = copy.deepcopy(observed['row'])
        checks = observed.get('checks', {})
        blocked = next((reason for failed, reason in (
            (checks.get('other_side'), 'other_side_or_lock_note'),
            (checks.get('other_drive'), 'other_drive'),
            (checks.get('main'), 'main_checkout'),
            (checks.get('busy'), checks.get('process_reason') or 'busy'),
            (checks.get('protected'), 'protected'),
            (checks.get('missing'), 'missing'),
            (checks.get('live'), 'busy'),
            (bool(row.get('dirty')), 'dirty')) if failed), None)
        if blocked:
            row.update(action='candidate' if blocked == 'missing' else 'keep', reason_code=blocked)
        settlement = observed.get('settlement') if not blocked else None
        if settlement is not None:
            tag = row.get('tag')
            live, terminal = settlement['live'], settlement['terminal']
            lease = bool(tag and tag['v'] == 2 and tag['repo'] == row['name']
                         and tag['lease_until'] > now)
            if live:
                row.update(action='keep', reason_code='busy')
            elif not lease:
                row.update(action='keep', reason_code='orphan')
            elif terminal or settlement['merged']:
                row.update(action='candidate', reason_code='settled')
        quarantine = observed.get('quarantine')
        if quarantine is not None:
            key = quarantine['key']
            if quarantine.get('live'):
                row.update(action='keep', reason_code='live_owner')
            eligible = row['reason_code'] == 'orphan'
            if eligible and quarantine.get('excluded'):
                row.update(reason_code='ignored_content', excluded=quarantine['excluded'])
                eligible = False
            if request['apply']:
                records = state.setdefault('quarantine', {})
                previous = records.get(key)
                fingerprint = quarantine.get('identity')
                if not eligible:
                    if records.pop(key, None) is not None:
                        events.append(dict(kind='quarantine_reset', path=row['path']))
                elif not previous or previous['identity'] != fingerprint:
                    records[key] = dict(identity=fingerprint, since=now)
                    events.append(dict(kind='quarantine_started', path=row['path']))
                elif now - previous['since'] >= 3 * 86400:
                    row.update(action='candidate', reason_code='quarantine_elapsed',
                               quarantine_identity=fingerprint)
        approved = request.get('approved_path')
        if approved:
            if not observed.get('approved_match', row['path'] == approved):
                continue
            if row['reason_code'] in ('unsettled', 'orphan'):
                row['action'] = 'candidate'
        validation = observed.get('validation', {})
        if validation.get('identity_matches') is False:
            row.update(action='keep', reason_code='revalidation_changed')
        elif validation.get('nested'):
            row.update(action='keep', reason_code='nested_worktree')
        elif validation.get('excluded'):
            row.update(action='keep', reason_code='ignored_content', excluded=validation['excluded'])
        decisions.append(row)
    return dict(decisions=decisions, state=state, events=events)


class CleanupState:
    """Durable state and journal adapter; state transforms run under the file lock."""
    def __init__(self, state_dir):
        self.state_dir = state_dir

    def update(self, transform):
        return disk_state.update(self.state_dir, transform)

    def append(self, record):
        disk_state.append_manifest(self.state_dir, record)


def decide_and_store(request, observations, store):
    def update(state):
        answer = decide(request, observations, state, request['now'])
        state.clear()
        state.update(answer['state'])
        return answer
    # Reports must not even write an unchanged state file.
    return store.update(update) if request['apply'] else decide(request, observations, {}, request['now'])


def evaluate(request, runtime):
    """Observe, decide transactionally, then ask the adapter to revalidate/apply."""
    observations = runtime.evidence.observe(request)
    answer = (decide_and_store(request, observations, runtime.state)
              if any('quarantine' in o for o in observations)
              else decide(request, observations, {}, request['now']))
    rows = []
    for row in answer['decisions']:
        if request['apply'] and row['action'] == 'candidate':
            from .disk_worktrees import registration_identity
            row = runtime.evidence.apply(request, row, registration_identity(row))
        rows.append(row)
    return dict(items=rows, errors=[dict(r) for r in rows if r.get('error')],
                reclaimed_bytes=sum(r['bytes'] for r in rows if r['action'] == 'removed'),
                events=answer['events'])
