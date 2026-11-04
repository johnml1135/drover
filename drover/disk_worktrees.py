"""Checked worktree evidence and path-specific eviction; branches always survive."""
import os
from pathlib import Path

from . import disk_evidence, disk_registrations, disk_removal, placement
from .disk_authority import LazyProof, inspect, quarantine, quarantine_observation, unlisted_live
from .disk_cleanup_decisions import CleanupRuntime, evaluate
from .disk_inventory import safe_path, worktree_roots
from .disk_state import CleanupState
from .disk_stage import item


class WorktreeEvidence:
    """Real evidence on the stage's `run`. apply always enters `remove_worktree`'s independent fresh validation."""
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
        store = self.store if self.store is not None else CleanupState(request.get('state_dir'))
        return disk_removal.remove_worktree(self.run, decision, store=store)


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
        except (OSError, ValueError) as error:
            run.error(dict(repo=machine['repo'], error=str(error)))
            continue
        found = {}
        if not request.get('approved_path'):
            found = registrations(request, machine, listed, runtime, run, repos[number + 1:])
            if run.stopped:
                return
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
            except (OSError, ValueError) as error:
                row = item(entry['worktree'], 'unavailable', error=str(error))
                if request['apply']:
                    quarantine(run, machine, row)
                if not request.get('approved_path') or placement.cleanup_parts(row['path']) == placement.cleanup_parts(request['approved_path']):
                    run.add(row)
        if not request.get('approved_path') and found is not None:
            unlisted(request, machine, listed, skip=found, run=run, later=repos[number + 1:])
            if run.stopped:
                return
        run.checkpoint()


def registrations(request, machine, listed, runtime, run, later=()):
    """Prevent a foreign prune from dropping registrations, then report (and on apply repair) lost ones, each row sent
    as its folder settles. Past the line before a lost folder, the stage stops: `listed` and the repos in `later` stay
    unreached."""
    try:
        for row in disk_registrations.protect(request, machine):
            run.add(row)
        found = disk_registrations.lost(machine, listed)
        store = runtime.state if runtime and runtime.state is not None else CleanupState(request.get('state_dir'))
        rows = disk_registrations.report(request, machine, listed, found, store, evidence=run.evidence, clock=run.clock)
        for _ in found:         # one row each, judged only when asked for
            if run.clock.late():
                run.stop('settled_deadline', machine, listed, later)
                return found
            run.add(next(rows))
            run.checkpoint()
        return found
    except (OSError, ValueError) as error:
        run.error(dict(repo=machine['repo'], error=str(error)))
        return None     # lost folders unknown: the caller must not judge this repo's orphans


def unlisted(request, machine, listed, skip=(), *, run, later=()):
    """Judge, and on apply remove, the stray folders under the repo's worktree roots, each row and its bytes sent as
    the folder settles. Past the line before a folder, the stage stops: it and the repos in `later` stay unreached."""
    evidence = run.evidence

    def settle(row):
        run.add(row)
        if row.get('error') and row.get('unlisted'):
            run.error(row)
        if row['action'] == 'removed':
            run.reclaimed(row['bytes'])
        run.checkpoint()
    registered = {str(Path(placement.native(e['worktree']))) for e in listed}
    roots = [r for r in worktree_roots(machine, this_side=True)
             if r.name.endswith('.worktrees') and r.is_dir() and safe_path(r)]
    for path in (p for root in roots for p in root.iterdir()):
        if str(path) in registered or any(k == str(path) or k.startswith(str(path) + os.sep) for k in skip) or not placement.cleanup_contains(request['drive']['path'], str(path)):
            continue
        if run.clock.late():
            return run.stop('settled_deadline', machine, [], later)
        row = item(path, 'unknown_folder', repo=machine['repo'], name=machine['name'])
        try:
            if not path.is_dir():
                raise ValueError('not a checkout directory')
            if placement.worktree_side(str(path)) != placement.cleanup_side():
                raise ValueError('snapshot side unavailable')
            if unlisted_live(run, path):
                row.update(reason_code='live_owner')
                quarantine(run, machine, row)
                settle(row)
                continue
            head = disk_evidence.snapshot(machine, path, evidence=evidence)
            row.update(head=head, branch=None, side_lock=None, unlisted=True, ignored=[],
                       tag=None, reason_code='orphan', **evidence.measure(path))
            row = quarantine(run, machine, row)
            if request['apply'] and row['action'] == 'candidate':
                row = disk_removal.remove_worktree(run, row, store=CleanupState(request.get('state_dir')))
        except (OSError, ValueError) as error:
            row.update(action='keep', reason_code='unavailable', error=str(error))
            quarantine(run, machine, row)
        settle(row)
