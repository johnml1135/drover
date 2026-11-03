"""Checked worktree evidence and path-specific eviction; branches always survive."""
import os
from pathlib import Path
import subprocess

from . import disk_evidence, disk_registrations, git_client, ownership, placement
from .disk_authority import LazyProof, decide, ignored_outside, inspect, quarantine, quarantine_observation, unlisted_live
from .disk_cleanup_decisions import CleanupRuntime, evaluate
from .disk_evidence import _out
from .disk_inventory import safe_path, worktree_roots
from .disk_state import CleanupState
from .disk_stage import item


def unlock_at(repo, path, *, timeout_s):
    _out(repo, 'worktree', 'unlock', '--', path, timeout_s=timeout_s)


def lock_at(repo, path, reason, *, timeout_s):
    # Recovery is best effort, like the original unchecked Git command: a mutate raises nothing, a timeout included.
    git_client.mutate(repo, 'worktree', 'lock', '--reason', reason, '--', str(path), timeout_s=timeout_s)


def discard_at(repo, path, evidence, *, timeout_s):
    _out(repo, 'worktree', 'remove', '--force', '--', path, timeout_s=timeout_s)
    evidence.took(repo, path)


def rescue_at(repo, ref, head, *, timeout_s):     # the update is a mutating step, its check a read
    _out(repo, 'update-ref', ref, head, '', timeout_s=timeout_s)
    if _out(repo, 'rev-parse', ref).strip() != head:
        raise ValueError('rescue ref verification failed')


class WorktreeEvidence:
    """Real evidence on the stage's `run`. apply always enters remove's independent fresh validation."""
    def __init__(self, machine, entry, store=None, *, run):
        self.machine, self.entry, self.store, self.run = machine, entry, store, run

    def observe(self, request):
        run = self.run
        row = inspect(run, self.machine, self.entry, processes=LazyProof(run.evidence.processes))
        observed = quarantine_observation(run, self.machine, row)
        approved = request.get('approved_path')
        observed['approved_match'] = not approved or placement.cleanup_parts(row['path']) == placement.cleanup_parts(approved)
        return [observed]

    def apply(self, request, decision, expected_identity):
        if disk_evidence.registration_identity(decision) != expected_identity:
            raise ValueError('selected identity changed')
        return remove(request, decision, store=self.store, run=self.run)


def settled(run, *, runtime=None) -> None:
    """Stage 4 on `run`: each listed worktree judged, and removed on apply, its rows sent before the next starts. Past
    the clock's line it stops with the one `settled_deadline` row, an item and not an error: unreached worktrees stay,
    and every removal is re-proven. An approved removal's clock is exempt, so it is never cut off."""
    request = run.request.decide_view()
    repos = request['repos']
    for number, machine in enumerate(repos):
        if run.clock.late():
            return run.stop('settled_deadline', machine, [], repos[number:])
        try:
            listed = run.evidence.listed(machine['repo']).rows
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            run.error(dict(repo=machine['repo'], error=str(error)))
            continue
        found = {}
        if not request.get('approved_path'):
            found = registrations(request, machine, listed, runtime, run)
        for position, entry in enumerate(listed):
            run.checkpoint()        # between units: the last worktree's removal has completed or never started
            if run.clock.late():
                return run.stop('settled_deadline', machine, listed[position:], repos[number + 1:])
            try:
                store = runtime.state if runtime and runtime.state is not None else None
                evidence = runtime.evidence if runtime and runtime.evidence is not None else \
                    WorktreeEvidence(machine, entry, store, run=run)
                rt = CleanupRuntime(lambda: request.get('now', 0), None, None, None,
                                    evidence=evidence, state=store if store is not None else CleanupState(request.get('state_dir')))
                answer = evaluate(dict(request, now=request.get('now', 0)), rt)
                for row in answer['items']:
                    run.add(row)
                for row in answer['errors']:
                    run.error(row)
                run.reclaimed(answer['reclaimed_bytes'])
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                row = item(entry['worktree'], 'unavailable', error=str(error))
                if request['apply']:
                    quarantine(run, machine, row)
                if not request.get('approved_path') or placement.cleanup_parts(row['path']) == placement.cleanup_parts(request['approved_path']):
                    run.add(row)
        if not request.get('approved_path'):
            loose = unlisted(request, machine, listed, skip=found, run=run) if found is not None else []
            for row in loose:
                run.add(row)
                if row.get('error') and row.get('unlisted'):
                    run.error(row)
            run.reclaimed(sum(r['bytes'] for r in loose if r['action'] == 'removed'))
        run.checkpoint()


def registrations(request, machine, listed, runtime, run):
    """Prevent a foreign prune from dropping registrations, then report (and on apply repair) lost ones."""
    try:
        for row in disk_registrations.protect(request, machine):
            run.add(row)
        found = disk_registrations.lost(machine, listed)
        store = runtime.state if runtime and runtime.state is not None else CleanupState(request.get('state_dir'))
        for row in disk_registrations.report(request, machine, listed, found, store, evidence=run.evidence, clock=run.clock):
            run.add(row)
        return found
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        run.error(dict(repo=machine['repo'], error=str(error)))
        return None     # lost folders unknown: the caller must not judge this repo's orphans


def remove(request: dict, selected: dict, *, store=None, run) -> dict:
    """Revalidate all evidence, then journal before unlocking or deleting by path. No journal `planned` is written
    unless the run's clock can still finish a removal; otherwise the row is kept as `removal_deadline`. The run's
    evidence holds the listing this removal is compared with."""
    import uuid
    clock, evidence = run.clock, run.evidence
    store = store if store is not None else CleanupState(request.get('state_dir'))
    row = dict(selected)
    machine = next(m for m in request['repos'] if placement.cleanup_parts(m['repo']) == placement.cleanup_parts(row['repo']))
    path = Path(row['path'])
    record, unlocked, cap = None, False, clock.mutation_cap()     # each git mutation's timeout (section 6.1)
    try:
        entry = evidence.row(machine['repo'], path)
        if row.get('unlisted'):
            if entry is not None or disk_evidence.snapshot(machine, path, evidence=evidence) != row['head']:
                raise ValueError('snapshot changed')
            fingerprint = disk_evidence.identity(path) + repr((row.get('head'), row.get('tag'), row.get('branch'),
                                               placement.cleanup_parts(row['gitdir']) if row.get('gitdir') else None, row.get('side_lock')))
            if fingerprint != row['quarantine_identity']:
                raise ValueError('snapshot activity changed')
            current = dict(row)
        else:
            if entry is None:
                raise ValueError('registration changed')
            current = inspect(run, machine, entry, processes=LazyProof(lambda: evidence.fresh_processes().proof))
            if row['reason_code'] == 'quarantine_elapsed' and current['reason_code'] == 'orphan':
                fingerprint = disk_evidence.identity(path) + repr((current.get('head'), current.get('tag'), current.get('branch'),
                                                   placement.cleanup_parts(current['gitdir']) if current.get('gitdir') else None, current.get('side_lock')))
                if fingerprint == row['quarantine_identity']:
                    current.update(action='candidate', reason_code='quarantine_elapsed')
        if request.get('approved_path') and placement.cleanup_parts(request['approved_path']) == placement.cleanup_parts(path) and current['reason_code'] in ('unsettled', 'orphan'):
            current['action'] = 'candidate'
        proof = evidence.fresh_registrations(machine['repo'], path, path,
                                             pointer=not row.get('unlisted') and current['reason_code'] != 'missing')
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
        fresh_identity, selected_identity = disk_evidence.registration_identity(current), disk_evidence.registration_identity(row)
        def validate(**validation):
            return decide(dict(request, approved_path=None), [dict(row=current, validation=validation)], {}, request['now'])['decisions'][0]
        matches = current['action'] == 'candidate' and all(fresh_identity.get(k) == selected_identity.get(k) for k in keys)
        if not matches:
            return validate(identity_matches=False)
        if proof.overlaps:
            return validate(nested=True)
        if current['reason_code'] != 'missing':
            excluded = ignored_outside(path, current['ignored'], evidence.declarations(machine, machine['repo'] if row.get('unlisted') else path))
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
                      side=placement.cleanup_side(), repo=machine['repo'], path=str(path),
                      head=row['head'], branch=row.get('branch'), side_lock=row.get('side_lock'),
                      rescue_ref=rescue, restore_argv=placement.git_argv(machine['repo'], args),
                      lock_argv=None)      # restore_argv locks at creation
        store.append(record)
        if rescue:
            rescue_at(machine['repo'], rescue, row['head'], timeout_s=cap)
        if not safe_path(path) or not placement.cleanup_contains(request['drive']['path'], str(path)):
            raise ValueError('worktree path became linked or protected')
        if row.get('unlisted') and unlisted_live(run, path):
            row.update(action='keep', reason_code='live_owner')
            quarantine(run, machine, row)
            store.append(dict(record, phase='failed'))
            return row
        if row.get('side_lock'):
            unlock_at(machine['repo'], path, timeout_s=cap)
            unlocked = True
        if row.get('unlisted'):
            import shutil
            shutil.rmtree(path)
        else:
            discard_at(machine['repo'], path, evidence, timeout_s=cap)
        row.update(action='removed', manifest=record['id'])
        store.append(dict(record, phase='completed'))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        if unlocked:
            # On removal failure, restore only the marker we just proved and unlocked.
            lock_at(machine['repo'], path, row['side_lock'], timeout_s=cap)
        row.update(action='keep' if row['action'] != 'removed' else 'removed', error=str(error))
        if record:
            try:
                store.append(dict(record, phase='failed'))
            except OSError:
                pass
    return row


def unlisted(request, machine, listed, skip=(), *, run):
    evidence, out = run.evidence, []
    registered = {str(Path(placement.native(e['worktree']))) for e in listed}
    roots = [r for r in worktree_roots(machine, this_side=True)
             if r.name.endswith('.worktrees') and r.is_dir() and safe_path(r)]
    for path in (p for root in roots for p in root.iterdir()):
        if str(path) in registered or any(k == str(path) or k.startswith(str(path) + os.sep) for k in skip) or not placement.cleanup_contains(request['drive']['path'], str(path)):
            continue
        row = item(path, 'unknown_folder', repo=machine['repo'], name=machine['name'])
        try:
            if not path.is_dir():
                raise ValueError('not a checkout directory')
            if placement.worktree_side(str(path)) != placement.cleanup_side():
                raise ValueError('snapshot side unavailable')
            if unlisted_live(run, path):
                row.update(reason_code='live_owner')
                quarantine(run, machine, row)
                out.append(row)
                continue
            head = disk_evidence.snapshot(machine, path, evidence=evidence)
            row.update(head=head, branch=None, side_lock=None, unlisted=True, ignored=[],
                       tag=None, reason_code='orphan', **evidence.measure(path))
            row = quarantine(run, machine, row)
            if request['apply'] and row['action'] == 'candidate':
                row = remove(request, row, run=run)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            row.update(action='keep', reason_code='unavailable', error=str(error))
            quarantine(run, machine, row)
        out.append(row)
    return out
