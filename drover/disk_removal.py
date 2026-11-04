"""Journalled removal (section 7.3): take away what authority allowed, journalled first and re-proven immediately before.
Between journal `planned` and `completed` every git step takes the stage clock's `mutation_cap()`."""
import shutil
import time
import uuid
from pathlib import Path

from . import git_client, ownership, placement
from .disk_authority import LazyProof, decide, ignored_outside, inspect, quarantine, unlisted_live
from .disk_evidence import _out, identity, registration_identity, snapshot
from .disk_inventory import safe_path

JOURNAL_PAUSES_S = (0.05, 0.2)      # before a `completed` or `failed` append's second and third tries


def unlock_at(repo, path, *, timeout_s):
    _out(repo, 'worktree', 'unlock', '--', path, timeout_s=timeout_s)


def lock_at(repo, path, reason, *, timeout_s):
    """Restore a marker just proved and unlocked. A lock that does not take raises, so the row can say so."""
    args = ('worktree', 'lock', '--reason', reason, '--', str(path))
    done = git_client.mutate(repo, *args, timeout_s=timeout_s)
    if not done.ok:
        raise git_client.GitFailed(args, done.why, done.code, done.err, done.out)


def discard_at(repo, path, evidence, *, timeout_s):
    _out(repo, 'worktree', 'remove', '--force', '--', path, timeout_s=timeout_s)
    evidence.took(repo, path)


def rescue_at(repo, ref, head, *, timeout_s):
    """The update and its verifying `rev-parse`, both inside the journalled window, so both take the cap (6.1:643)."""
    _out(repo, 'update-ref', ref, head, '', timeout_s=timeout_s)
    if _out(repo, 'rev-parse', ref, timeout_s=timeout_s).strip() != head:
        raise ValueError('rescue ref verification failed')


def _close(store, record, phase, cap, row):
    """Append `completed` or `failed` with a few short tries inside `cap`; a journal still unwritable joins the row's
    error rather than being lost."""
    end = time.monotonic() + cap
    for pause in JOURNAL_PAUSES_S + (None,):
        try:
            return store.append(dict(record, phase=phase))
        except OSError as error:
            if pause is None or time.monotonic() + pause >= end:
                row['error'] = '; '.join(filter(None, (row.get('error'), f'journal {phase} failed: {error}')))
                return None
            time.sleep(pause)


def _fingerprint(path, row):
    return identity(path) + repr((row.get('head'), row.get('tag'), row.get('branch'),
                                  placement.cleanup_parts(row['gitdir']) if row.get('gitdir') else None, row.get('side_lock')))


def remove_worktree(run, selected: dict, *, store) -> dict:
    """Section 7.3's five steps: the row from the stage listing, a fresh inspect with a new process proof, the one
    `fresh_registrations`, `decide` then nesting, then ignored content, `can_mutate()`, journal `planned`, rescue ref,
    path recheck, unlock, delete, journal `completed`. No `planned` is written unless the clock can still finish the
    removal; otherwise the row keeps as `removal_deadline`. A failure after `planned` restores only the marker just
    unlocked and journals `failed`, a failed restore included, the row carrying both errors."""
    request, clock, evidence = run.request.decide_view(), run.clock, run.evidence
    row = dict(selected)
    machine = next(m for m in request['repos'] if placement.cleanup_parts(m['repo']) == placement.cleanup_parts(row['repo']))
    repo, path = machine['repo'], Path(row['path'])
    record, planned, unlocked, cap = None, False, False, clock.mutation_cap()     # each git mutation's timeout (6.1)
    try:
        entry = evidence.row(repo, path)
        if row.get('unlisted'):
            if entry is not None or snapshot(machine, path, evidence=evidence) != row['head']:
                raise ValueError('snapshot changed')
            if _fingerprint(path, row) != row['quarantine_identity']:
                raise ValueError('snapshot activity changed')
            current = dict(row)
        else:
            if entry is None:
                raise ValueError('registration changed')
            current = inspect(run, machine, entry, processes=LazyProof(lambda: evidence.fresh_processes().proof))
            if row['reason_code'] == 'quarantine_elapsed' and current['reason_code'] == 'orphan' and \
                    _fingerprint(path, current) == row['quarantine_identity']:
                current.update(action='candidate', reason_code='quarantine_elapsed')
        if request.get('approved_path') and placement.cleanup_parts(request['approved_path']) == placement.cleanup_parts(path) and current['reason_code'] in ('unsettled', 'orphan'):
            current['action'] = 'candidate'
        proof = evidence.fresh_registrations(repo, path, path, pointer=not row.get('unlisted') and current['reason_code'] != 'missing')
        refusal = next(iter(proof.refused), None)
        if refusal and refusal.why == 'changed' and row.get('unlisted'):
            # Registered since the listing, as a row at `entry` would say: its three days start again (spec S5).
            quarantine(run, machine, dict(current, action='keep', reason_code='identity_changed'))
            raise ValueError('snapshot changed')
        if refusal or (path.exists() and ownership.read(str(path)) != current.get('tag')):
            current.update(action='keep', reason_code='registration_unknown' if refusal and refusal.why == 'unknown' else 'identity_changed',
                           detail=refusal.detail if refusal else 'tag')
            return quarantine(run, machine, current) if row.get('unlisted') else current
        keys = ('head', 'branch', 'side_lock', 'gitdir', 'tag')
        fresh_identity, selected_identity = registration_identity(current), registration_identity(row)

        def validate(**validation):
            return decide(dict(request, approved_path=None), [dict(row=current, validation=validation)], {}, request['now'])['decisions'][0]
        if not (current['action'] == 'candidate' and all(fresh_identity.get(k) == selected_identity.get(k) for k in keys)):
            return validate(identity_matches=False)
        if proof.overlaps:
            return validate(nested=True)
        if current['reason_code'] != 'missing':
            excluded = ignored_outside(path, current['ignored'], evidence.declarations(machine, repo if row.get('unlisted') else path))
            if excluded:
                return validate(excluded=excluded)
        if not clock.can_mutate():      # past the line, or too little time left for its capped steps: nothing starts
            current.update(action='keep', reason_code='removal_deadline')
            return current
        rescue = 'refs/drover/disk-rescue/' + uuid.uuid4().hex if not row.get('branch') else None
        target = row['branch'].removeprefix('refs/heads/') if row.get('branch') else row['head']
        lock = ['--lock', '--reason', row.get('side_lock') or placement.cleanup_side()]   # restored locked, as created
        args = ['worktree', 'add'] + lock + ([] if row.get('branch') else ['--detach']) + ['--', str(path), target]
        record = dict(v=1, id=uuid.uuid4().hex, phase='planned', ts=request['now'],
                      side=placement.cleanup_side(), repo=repo, path=str(path),
                      head=row['head'], branch=row.get('branch'), side_lock=row.get('side_lock'),
                      rescue_ref=rescue, restore_argv=git_client.argv(repo, args),
                      lock_argv=None)      # restore_argv locks at creation
        store.append(record)        # no retry: a failed `planned` stops before any mutation
        planned = True
        if rescue:
            rescue_at(repo, rescue, row['head'], timeout_s=cap)
        if not safe_path(path) or not placement.cleanup_contains(request['drive']['path'], str(path)):
            raise ValueError('worktree path became linked or protected')
        if row.get('unlisted') and unlisted_live(run, path):
            row.update(action='keep', reason_code='live_owner')
            quarantine(run, machine, row)
            _close(store, record, 'failed', cap, row)
            return row
        if row.get('side_lock'):
            unlock_at(repo, path, timeout_s=cap)
            unlocked = True
        if row.get('unlisted'):
            shutil.rmtree(path)
        else:
            discard_at(repo, path, evidence, timeout_s=cap)
        row.update(action='removed', manifest=record['id'])
        _close(store, record, 'completed', cap, row)
    except (OSError, ValueError) as error:
        message = str(error)
        if unlocked:
            # On removal failure, restore only the marker we just proved and unlocked; its own failure is reported.
            try:
                lock_at(repo, path, row['side_lock'], timeout_s=cap)
            except (OSError, ValueError) as relock:
                message += f'; lock restore failed: {relock}'
        row.update(action='keep' if row['action'] != 'removed' else 'removed', error=message)
        if planned:
            _close(store, record, 'failed', cap, row)
    return row
