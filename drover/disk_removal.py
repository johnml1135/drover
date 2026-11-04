"""Removal (section 7.3): every deletion by path in the disk family, each re-proven immediately before and started
only while the stage clock can finish it. A worktree's is journalled first; between journal `planned` and `completed`
every git step takes the stage clock's `mutation_cap()`. No remove_* checkpoints: each ends in its row."""
import shutil
import stat
import time
import uuid
from pathlib import Path

from . import git_client, ownership, placement
from .disk_authority import OUTPUT_UNAVAILABLE, DisposableUnit, LazyProof, decide, disposable_status, ignored_outside, \
    inspect, quarantine, rule_disposable_fresh, rule_unit_fresh, unlisted_live
from .disk_evidence import git_out, identity, registration_identity, snapshot
from .disk_inventory import safe_path

JOURNAL_PAUSES_S = (0.05, 0.2)      # before a `completed` or `failed` append's second and third tries
REMOVAL_DEADLINE = 'removal_deadline'       # a removal the stage clock could not finish never starts


class LinePassed(Exception):
    """The stage clock's line passed partway through a unit: what went is counted, the rest stays."""


def unlock_at(repo, path, *, timeout_s):
    git_out(repo, 'worktree', 'unlock', '--', path, timeout_s=timeout_s)


def lock_at(repo, path, reason, *, timeout_s):
    """Restore a marker just proved and unlocked. A lock that does not take raises, so the row can say so."""
    args = ('worktree', 'lock', '--reason', reason, '--', str(path))
    done = git_client.mutate(repo, *args, timeout_s=timeout_s)
    if not done.ok:
        raise git_client.GitFailed(args, done.why, done.code, done.err, done.out)


def discard_at(repo, path, evidence, *, timeout_s):
    git_out(repo, 'worktree', 'remove', '--force', '--', path, timeout_s=timeout_s)
    evidence.took(repo, path)


def rescue_at(repo, ref, head, *, timeout_s):
    """The update and its verifying `rev-parse`, both inside the journalled window, so both take the cap (6.1:643)."""
    git_out(repo, 'update-ref', ref, head, '', timeout_s=timeout_s)
    if git_out(repo, 'rev-parse', ref, timeout_s=timeout_s).strip() != head:
        raise ValueError('rescue ref verification failed')


def close_journal(store, record, phase, clock, row=None):
    """Append `completed` or `failed` with a few short tries inside both the clock's mutation cap and what is left of
    the stage. None once written; a journal still unwritable is returned, joined to `row`'s error when given, rather
    than being lost."""
    end = time.monotonic() + min(clock.mutation_cap(), clock.left())
    for pause in JOURNAL_PAUSES_S + (None,):
        try:
            store.append(dict(record, phase=phase))
            return None
        except OSError as error:
            if pause is None or time.monotonic() + pause >= end:
                note = f'journal {phase} failed: {error}'
                if row is not None:
                    row['error'] = '; '.join(filter(None, (row.get('error'), note)))
                return note
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
            current.update(action='keep', reason_code=REMOVAL_DEADLINE)
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
            close_journal(store, record, 'failed', clock, row)
            return row
        if row.get('side_lock'):
            unlock_at(repo, path, timeout_s=cap)
            unlocked = True
        if row.get('unlisted'):
            shutil.rmtree(path)
        else:
            discard_at(repo, path, evidence, timeout_s=cap)
        row.update(action='removed', manifest=record['id'])
        close_journal(store, record, 'completed', clock, row)
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
            close_journal(store, record, 'failed', clock, row)
    return row


def prune_emptied(target, removed, touched):
    """Folders a removal emptied go with it, deepest first, up to the target; rmdir only ever takes an empty one."""
    folders = set()
    for name in removed:
        parent = Path(name).parent
        while parent not in folders:
            folders.add(parent)
            if parent == target:
                break
            parent = parent.parent
    for folder in sorted(folders, key=lambda p: len(p.parts), reverse=True):
        try:
            folder.rmdir()
            touched.add(folder.parent)
        except OSError:
            pass


def take_folder(run, folder, known) -> None:
    """A folder `disposable_status` listed whole, or an empty one: walked first, so a link, a path off the drive or on
    another device anywhere under it, or files and bytes other than `known` (its measure), keep all of it, as each path
    would have kept itself. Then each file is unlinked and counted on `run` as it goes, each folder after what it held,
    each only while its folder is still the one walked (no link, same device and inode), so a folder swapped for a link
    stops it there; and only while the clock's line has not passed (`LinePassed`). Never follows a link."""
    walked, order = {}, []      # folder -> (st_dev, st_ino); (path, is a folder, size) children first

    def concrete(path):
        if placement.cleanup_link(path):
            raise ValueError('link or reparse point: ' + str(path))
        info = path.lstat()
        if walked and info.st_dev != walked[folder][0]:
            raise ValueError('another device: ' + str(path))
        return info

    def walk(directory):
        info = concrete(directory)
        if not placement.cleanup_contains(run.request.drive['path'], str(directory)):
            raise ValueError('path left the drive: ' + str(directory))
        if not info.st_ino:
            raise ValueError('no inode to hold the folder to: ' + str(directory))
        walked[directory] = (info.st_dev, info.st_ino)
        for child in sorted(directory.iterdir()):
            info = concrete(child)
            if stat.S_ISDIR(info.st_mode):
                walk(child)
            elif not placement.cleanup_contains(run.request.drive['path'], str(child)):
                raise ValueError('path left the drive: ' + str(child))
            else:
                order.append((child, False, info.st_size))
        order.append((directory, True, 0))

    def unchanged(directory):
        info = concrete(directory)
        if not info.st_ino or (info.st_dev, info.st_ino) != walked[directory]:
            raise ValueError('folder changed during removal: ' + str(directory))
    walk(folder)
    files = [size for _, is_folder, size in order if not is_folder]
    if (len(files), sum(files)) != (known['files'], known['bytes']):
        raise ValueError('disposable folder changed: ' + str(folder))
    for path, is_folder, size in order:
        if run.clock.late():
            raise LinePassed()
        unchanged(path if is_folder else path.parent)
        if is_folder:
            path.rmdir()
        else:
            concrete(path)
            path.unlink()
            run.reclaimed(size)


def remove_disposable(run, unit: DisposableUnit) -> None:
    """Section 7.3 for a disposable unit: the listing's row and `disposable_status`, read as the first pass read it, so
    a folder it listed whole must still be listed whole, as untracked or ignored as it was; a fresh process proof
    through `rule_disposable_fresh`, tracked, and the compare with the first pass; `fresh_registrations` (pointer:
    `.git` must be there) and `rule_unit_fresh`; then, while the clock can finish it, each path, its bytes counted on
    `run` as it goes. Any change keeps the unit; a clock that cannot start it, or whose line passes partway, keeps it
    as `removal_deadline`. `unit.row` carries the end."""
    machine, entry, evidence, touched, target, measured, row = (
        unit.machine, unit.registration, unit.evidence, unit.touched, unit.target, unit.measured, unit.row)
    path = evidence[0]
    try:
        current = run.evidence.row(machine['repo'], entry['worktree'])
        current_dirty, current_ignored = disposable_status(run, path, evidence[3])
        if registration_identity(current or {}) != registration_identity(entry):
            raise ValueError('disposable registration changed')
        fresh = rule_disposable_fresh(run, machine, current, run.evidence.fresh_processes(), touched=touched)
        if fresh.action != 'candidate':
            return row.update(action='keep', reason_code=fresh.reason_code, error=fresh.detail)
        fresh_evidence = tuple(fresh.facts[key] for key in ('path', 'head', 'tag', 'declarations'))
        tracked = {str(path / name) for name in run.evidence.tracked(path, target.relative_to(path))}
        names = set(measured)
        if placement.cleanup_parts(fresh_evidence[0]) != placement.cleanup_parts(evidence[0]) or \
                fresh_evidence[1:] != evidence[1:] or \
                (names & unit.untracked) - set(current_dirty) or (names - unit.untracked) - set(current_ignored) or \
                tracked & names or (tracked and str(target) in measured) or \
                any(run.evidence.measure(Path(name)) != known for name, known in measured.items()):
            raise ValueError('disposable evidence changed')
        ruling = rule_unit_fresh(run, unit, run.evidence.fresh_registrations(machine['repo'], target, path, pointer=True))
        if ruling.action != 'candidate':
            return row.update(action='keep', reason_code=ruling.reason_code, error=ruling.detail)
        if not run.clock.can_mutate():      # past the line, or too little time left: no path goes
            return row.update(action='keep', reason_code=REMOVAL_DEADLINE)
        for count, (name, known) in enumerate(measured.items()):
            if count and run.clock.late():      # a unit of many paths stops between them at the line
                raise LinePassed()
            leaf = Path(name)
            if leaf.is_dir() or placement.cleanup_link(leaf):       # a folder listed whole; a link refuses there
                take_folder(run, leaf, known)
            else:
                leaf.unlink()
                run.reclaimed(known['bytes'])
            touched.add(leaf.parent)
        if str(target) not in measured:
            prune_emptied(target, measured, touched)
        row['action'] = 'removed'
    except LinePassed:      # partway: what went is counted, the rest stays for the next run
        row.update(action='keep', reason_code=REMOVAL_DEADLINE)
    except (OSError, ValueError, StopIteration) as error:      # git_client turns every spawn failure into one of these
        row.update(action='keep', reason_code=OUTPUT_UNAVAILABLE, error=str(error))


def remove_temp_entry(run, row, path) -> None:
    """A temp entry or build unit its stage has just proven again: a folder's tree, or a file, its row's bytes counted.
    Never follows a link: rmtree refuses one, and every walk before it kept a tree holding one. A clock that cannot
    finish it keeps it as `removal_deadline`; a failed removal raises for the stage's row, as before."""
    if not run.clock.can_mutate():
        return row.update(action='keep', reason_code=REMOVAL_DEADLINE)
    shutil.rmtree(path) if path.is_dir() else path.unlink()
    row['action'] = 'removed'
    run.reclaimed(row['bytes'])


def remove_cache_file(run, row, path, info) -> None:
    """One cache file its stage has just proven again, removed only while it is as measured (`info`) and on the drive,
    its size counted. A clock that cannot finish it keeps it as `removal_deadline`; a failed unlink raises for the
    stage, as before."""
    if not run.clock.can_mutate():
        return row.update(action='keep', reason_code=REMOVAL_DEADLINE)
    if path.stat() == info and placement.cleanup_contains(run.request.drive['path'], str(path)):
        path.unlink()
        row['action'] = 'removed'
        run.reclaimed(info.st_size)
