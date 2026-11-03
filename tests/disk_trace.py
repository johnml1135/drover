"""Deterministic trace of what stage 4 decides and deletes, through today's disk_side.serve.

Each scenario is a temp tree holding real admin files. `TraceGit` stands in for `subprocess` at PATCH_TARGETS and reads
every answer from that tree, so the listing, a proof of it and a removal all see one state. The record is
tests/disk_decision_trace.json; `compare` says which way a re-recording moved it. `python -m tests.disk_trace --write`
records it, `--compare OLD NEW` prints the changes and exits 1 on any `towards_deletion`.
"""
import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from drover import disk_budget, disk_paths, disk_side, disk_worktrees, placement, placement_proof, process
from tests.disk_fixture import DAY, DiskCase
from tests.disk_trace_compare import Change, compare  # noqa: F401  part of this interface
from tests.disk_trace_scene import (CLEAN_MERGE, FEATURE, NOW, PARTLY_PICKED, PICKED, SQUASHED, TIP, SceneTree, TraceGit,
                                    active, deleting_behind, quick_state, task_at, terminal)  # noqa: F401  TraceGit: this interface
from tests.disk_trace_stages import STAGE_SCENES
from tests.placement_trace import normalize

TRACE = Path(__file__).with_name('disk_decision_trace.json')
PATCH_TARGETS = (process,)                      # each module whose `subprocess` TraceGit replaces: every command reaches it
REMOVAL = (disk_worktrees, 'remove')            # where a mid-stage change lands: after selection, before revalidation
OUTPUT_REMOVAL = (disk_paths, 'remove_output')  # the same for a disposable path
WORKTREE_SIDE, CLEANUP_LINK = placement.worktree_side, placement.cleanup_link


class Scene(SceneTree):
    """One serve call at a time over a scene's tree; the record of each is host-neutral."""
    def physical(self, path):
        return 'other-drive' if any(placement.cleanup_parts(path)[:len(placement.cleanup_parts(p))]
                                    == placement.cleanup_parts(p) for p in self.elsewhere) else str(self.root)

    def proof(self, **_):
        """A snapshot, as the real one is: going busy after it shows only in a fresh proof. `fresh`, a change, lands just before the
        first fresh one. It stands in for the class, so the real `cleanup_processes` carries its diagnostics into the request."""
        self.proofs += 1
        change, self.fresh = (self.fresh, None) if self.proofs > 1 else (None, self.fresh)
        if change:
            change()
        seen = SimpleNamespace(busy_paths=set(self.busy_paths))
        return SimpleNamespace(busy=lambda path: DiskCase.busy(seen, path), reason=self.process_reason, **self.process_facts)

    def owner_side(self, path, lock=None):
        return self.other_side if str(path) in self.foreign else WORKTREE_SIDE(path, lock)

    def link(self, path):
        """`links` read as links, on any host; `unreadable` cannot be looked at."""
        if Path(path) in self.unreadable:
            raise PermissionError('unreadable')
        return Path(path) in self.links or CLEANUP_LINK(path)

    def used(self, *_, **__):
        return self.readings.pop(0) if len(self.readings) > 1 else self.readings[0]

    def free_reading(self, path):
        return self.free_gb.pop(0) if len(self.free_gb) > 1 else self.free_gb[0]

    def at_removal(self, real):
        def remove(request, *selected, **options):
            change, self.midstage = self.midstage, None
            if change:
                change(request)
            return real(request, *selected, **options)
        return remove

    def serve(self, apply, now, approved, timeout, tasks, stage=4, level=None, mode='on-demand', budget=None):
        drive = dict(path=str(self.root), name='trace', warn_gb=150., **(dict(level=level) if level else {}))
        request = dict(stage=stage, mode=mode, apply=apply, now=now, drive=drive, repos=self.machines,
                       paths=dict(self.cfg['disk_cleanup'], worker_timeout_s=timeout), state_dir=str(self.state),
                       tasks={'active_worktrees': [], 'tasks': [], 'at': now, **tasks}, budget=budget)
        if approved:
            request['approved_path'] = str(approved)
        self.proofs, self.readings = 0, list(self.used_gb)
        if self.each_run:
            self.each_run()
        patches = [patch.object(module, 'subprocess', self.git) for module in PATCH_TARGETS]
        patches += [patch.object(*where, self.at_removal(getattr(*where))) for where in (REMOVAL, OUTPUT_REMOVAL)]
        patches += [*quick_state(), patch.object(placement_proof, '_CleanupProcesses', self.proof),
                    patch.object(placement, 'physical_disk_path', self.physical),
                    patch.object(placement, 'worktree_side', self.owner_side),
                    patch.object(placement, 'cleanup_link', self.link),
                    patch.object(placement, 'wsl_used_gb', self.used),
                    patch.object(placement, 'cleanup_free', self.free_reading),
                    patch.object(disk_budget, 'SIDE', self.budget_side),
                    patch.object(disk_budget, '_clock', lambda: self.clock),
                    patch('uuid.uuid4', lambda: SimpleNamespace(hex='0' * 32))]
        for item in patches:
            item.start()
        try:
            return disk_side.serve(request)
        finally:
            for item in reversed(patches):
                item.stop()

    def tree(self):
        """Every path but the state directory's, which is the stage's to write."""
        found = set()
        for folder, dirs, files in os.walk(self.root):
            if folder == str(self.root):
                dirs.remove(self.state.name)
            found.update(os.path.join(folder, name) for name in dirs + files)
        return found

    def journal(self):
        manifest = self.state / 'disk-cleanup-manifest.jsonl'
        rows = manifest.read_text(encoding='utf-8').splitlines() if manifest.exists() else []
        fresh, self.journal_seen = rows[self.journal_seen:], len(rows)
        return [json.loads(row) for row in fresh]

    def record(self, *, apply, now=NOW, approved=None, timeout=1800, tasks=None, **stage):
        before, self.clock = self.tree(), 0.
        answer = self.serve(apply, now, approved, timeout, tasks or {}, **stage)
        gone = {Path(path) for path in before - self.tree()}
        rows = sorted((frozen_row(row) for row in answer['items']), key=lambda row: json.dumps(row, sort_keys=True))
        errors = [{k: e[k] for k in ('path', 'repo', 'action', 'reason_code', 'error') if k in e} for e in answer['errors']]
        trace = dict(rows=rows, errors=errors, journal=self.journal(),
                     deleted=sorted(str(p) for p in gone if p.parent not in gone))
        return self.neutral(normalize(trace, [(str(self.root), '<TMP>')]))

    def neutral(self, value):
        """The side's own name reads the same on both hosts; only exact values change, not text around them."""
        if isinstance(value, dict):
            return {key: self.neutral(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.neutral(item) for item in value]
        return {self.side: '<SIDE>', self.other_side: '<OTHER>'}.get(value, value) if isinstance(value, str) else value


def rejection(error):
    """An output rejection's fixed words: its detail names paths and hosts, and the idle limit is configuration."""
    words = error.removeprefix('not an eligible owning-side worktree: ').split(' (')[0]
    return 'idle' if words.startswith('worktree idle') else words


def frozen_row(row):
    """(path, action, reason_code), a deadline row's count, a cache command's argv and result, an output rejection's words,
    and head, branch, side_lock where a row may delete. Rows are sorted: folder order is the host's."""
    frozen = {key: row[key] for key in ('path', 'action', 'reason_code', 'count', 'argv', 'returncode') if key in row}
    if row['action'] in ('candidate', 'removed'):
        frozen.update({key: row.get(key) for key in ('head', 'branch', 'side_lock')})
    if row.get('reason_code') == 'output_evidence_unavailable':
        frozen['rejection'] = rejection(row['error'])
    return frozen


def busy_process_reason(s):
    s.busy_paths.add(str(s.worktree('w')))
    s.process_reason = 'process visibility unavailable'


def missing(s):
    shutil.rmtree(s.worktree('w'))


def status_fails(s):
    s.worktree('w')
    s.refuse('status', 'fatal: status failed')


def ignored(s, name, **options):
    s.exclude(name)
    s.worktree('w', extra=[name + '/x'], **options)


def ignored_declared(s):
    s.declared.append('bin')
    ignored(s, 'bin')


def nested_worktree(s):
    s.exclude('inner')
    s.worktree('inner', at=s.worktree('outer') / 'inner')


def ignored_holds_a_repository(s):
    """Declared and ignored, and still no removal: a repository inside the folder is not disposable."""
    s.declared.append('bin')
    s.exclude('bin')
    s.worktree('w', extra=['bin/inner/.git/HEAD'])


def identity_changed(s):
    """The registration moves between removal's two readings of it."""
    s.worktree('w')
    s.midstage = lambda request: s.git.after.update(status=lambda: s.move('w'))


def identity_changed_by_tag(s):
    """The tag changes once `inspect` has read it: the row still agrees with itself, and only a second read of the tag differs."""
    s.worktree('w')
    s.midstage = lambda request: s.git.after.update(status=lambda: s.tag('w', 'alpha', 'session:other'))
    return dict(applied_only=True)


def revalidation_dirty(s):
    path = s.worktree('w')
    s.midstage = lambda request: s.file(path / 'new.txt')


def revalidation_registration(s):
    """Still settled on its new branch, so only the comparison of the selected identity stops the removal."""
    s.worktree('w')
    s.midstage = lambda request: s.move('w')


def revalidation_unsettled(s):
    s.worktree('w')
    s.midstage = lambda request: s.move('w', FEATURE)


def revalidation_head(s):
    s.worktree('w')
    s.midstage = lambda request: s.advance('w')


def revalidation_tag(s):
    s.worktree('w')
    s.midstage = lambda request: s.tag('w', 'alpha', 'session:other')


def revalidation_busy(s):
    path = s.worktree('w')
    s.midstage = lambda request: s.busy_paths.add(str(path))


def removal_changes(s, change):
    """`change` lands after every check of the worktree, at the last git call before the removal's own path check."""
    path = s.worktree('w')
    s.midstage = lambda request: s.git.after.update({'ls-tree': lambda: change(path)})


def stale_task_snapshot(s, age=1000):
    """A terminal task in a snapshot too old to trust: its worktree counts as live."""
    s.worktree('w', owner='drover:T1')
    answer = terminal('T1', 'landed')
    answer['tasks']['at'] = NOW - age
    return answer


def lease_for_another_repo(s):
    """Settled and leased, but the lease names a repo this worktree is not in."""
    s.worktree('w', owner=None)
    s.tag('w', 'alpha', 'session:w', claims='beta')


def rescue_ref_fails(s):
    s.worktree('w', detached=True)
    s.refuse('update-ref', 'fatal: cannot lock ref')


def prune_expiry_unset(s):
    (s.common() / 'config').write_text('[core]\n\tbare = false\n', encoding='utf-8')


def removal_fails(s):
    s.worktree('w')
    s.refuse('worktree remove', 'fatal: removal failed')


def lost_registration(s):
    s.lost('lost')
    (s.common() / 'refs/heads/lost').write_text(TIP + '\n', encoding='utf-8')


def lost_registration_fails(s):
    lost_registration(s)
    s.refuse('worktree add', 'fatal: cannot add')


def repo_cannot_be_listed(s):
    """Alpha holds a settled worktree and a stray folder; unknown, neither is judged. Beta carries on without it."""
    s.worktree('a')
    s.folder('stray')
    s.repo('beta')
    s.worktree('b', repo='beta')
    s.refuse('worktree list', 'fatal: listing failed', s.common().parent)


def registrations_unknown(s):
    s.folder('stray')
    s.refuse('rev-parse --path-format=absolute --git-common-dir', 'fatal: no common directory')


def settled_deadline_at_a_repo(s):
    s.repo('beta')
    return dict(timeout=0)


def settled_deadline_at_a_worktree(s):
    s.repo('beta')
    s.worktree('w')
    s.git.after_listing = lambda: setattr(s, 'clock', 1e9)


def approved_removal(s):
    """The settled neighbour stays: an approval is for its one target."""
    s.worktree('neighbour')
    return dict(approved=s.worktree('w', commit=FEATURE))


# Each is built once and served dry, then applied; what a build returns, when a dict, goes to the record. The main checkout's own
# row is in every scene, so `main_checkout` is the bare one.
SCENES = dict(
    main_checkout=lambda s: None,
    lock_other_side=lambda s: s.worktree('w', lock=s.other_side),
    lock_note=lambda s: s.worktree('w', lock='a note'),
    other_drive=lambda s: s.elsewhere.add(s.worktree('w')),
    busy_process=lambda s: s.busy_paths.add(str(s.worktree('w'))),
    busy_process_reason=busy_process_reason,
    busy_live_task=lambda s: active(s.worktree('w')),
    busy_unknown_task=lambda s: s.worktree('w', owner='drover:T9'),
    busy_task_at_path=lambda s: task_at(s.worktree('w')),
    protected=lambda s: s.worktree('w', at=s.root / 'elsewhere' / '.venv' / 'w'),
    missing=missing,
    dirty=lambda s: s.worktree('w', extra=['scratch.txt']),
    status_fails=status_fails,
    orphan=lambda s: s.worktree('w', owner=None),
    settled=lambda s: s.worktree('w'),
    settled_detached=lambda s: s.worktree('w', detached=True),
    settled_by_cherry=lambda s: s.worktree('w', commit=PICKED),
    settled_by_equal_tree=lambda s: s.worktree('w', commit=SQUASHED),
    settled_terminal=lambda s: s.worktree('w', commit=FEATURE, owner='drover:T1') and terminal('T1', 'landed'),
    closed_and_recent=lambda s: s.age(s.worktree('w', commit=FEATURE, owner='drover:T1'), DAY) and terminal('T1', 'yanked'),
    unsettled=lambda s: s.worktree('w', commit=FEATURE),
    unsettled_partly_picked=lambda s: s.worktree('w', commit=PARTLY_PICKED),
    unsettled_clean_merge=lambda s: s.worktree('w', commit=CLEAN_MERGE),
    lease_expired=lambda s: s.worktree('w', lease=NOW - DAY),
    lease_for_another_repo=lease_for_another_repo,
    stale_task_snapshot=stale_task_snapshot,
    future_task_snapshot=lambda s: stale_task_snapshot(s, -1),
    ignored_holds_a_repository=ignored_holds_a_repository,
    removal_leaves_the_drive=lambda s: removal_changes(s, s.elsewhere.add),
    removal_becomes_a_link=lambda s: removal_changes(s, s.links.add),
    lost_registration_fails=lost_registration_fails,
    ignored_content_orphan=lambda s: ignored(s, 'cache', owner=None),
    ignored_content_at_removal=lambda s: ignored(s, 'cache'),
    ignored_declared=ignored_declared,
    nested_worktree=nested_worktree,
    under_main_checkout=lambda s: s.worktree('w', at=s.common().parent / 'sub' / 'w'),
    identity_changed=identity_changed,
    identity_changed_by_tag=identity_changed_by_tag,
    revalidation_dirty=revalidation_dirty,
    revalidation_registration=revalidation_registration,
    revalidation_unsettled=revalidation_unsettled,
    revalidation_head=revalidation_head,
    revalidation_tag=revalidation_tag,
    revalidation_busy=revalidation_busy,
    revalidation_locked_by_protect=lambda s: s.worktree('w', lock=None),
    prune_expiry_unset=prune_expiry_unset,
    removal_fails=removal_fails,
    rescue_ref_fails=rescue_ref_fails,
    unlisted=lambda s: s.folder('stray'),
    unlisted_untracked=lambda s: s.folder('stray', extra=['scratch.txt']),
    unlisted_edited=lambda s: s.file(s.folder('stray') / 'README.md', 'edited\n'),
    unlisted_live=lambda s: active(s.folder('stray')),
    unlisted_not_a_folder=lambda s: s.file(s.root / 'worktrees/alpha.worktrees/stray.txt'),
    lost_registration=lost_registration,
    lost_registration_unproven=lambda s: s.lost('lost'),
    repo_cannot_be_listed=repo_cannot_be_listed,
    registrations_unknown=registrations_unknown,
    settled_deadline_at_a_repo=settled_deadline_at_a_repo,
    settled_deadline_at_a_worktree=settled_deadline_at_a_worktree,
    approved_removal=approved_removal,
    approved_dirty=lambda s: dict(approved=s.worktree('w', extra=['scratch.txt'])),
    approved_past_the_deadline=lambda s: dict(approved=s.worktree('w', owner=None), timeout=0),
    **STAGE_SCENES,
)


def steps(scene, *moments):
    """The record of each (label, days after NOW, apply, change before it) on one scene, whose state carries over."""
    answers = {}
    for label, days, apply, change in moments:
        if change:
            change()
        answers[label] = scene.record(apply=apply, now=NOW + days * DAY)
    return answers


def quarantine(s):
    s.worktree('w', owner=None)
    return steps(s, ('started', 0, True, None), ('waiting', 1, True, None), ('report_ignores_it', 3, False, None),
                 ('elapsed', 3, True, None))


def quarantine_reset(s):
    """A busy spell changes no file, so the identity is the same and only the reset stops day 3 from elapsing."""
    path = s.worktree('w', owner=None)
    return steps(s, ('started', 0, True, None), ('reset_by_a_process', 1, True, lambda: s.busy_paths.add(str(path))),
                 ('started_again', 2, True, s.busy_paths.clear), ('not_yet', 3, True, None))


def quarantine_identity(s):
    path = s.worktree('w', owner=None)
    return steps(s, ('started', 0, True, None), ('identity_changed', 3, True, lambda: s.age(path / 'README.md', DAY)))


def quarantine_unlisted(s):
    s.folder('stray')
    return steps(s, ('started', 0, True, None), ('elapsed', 3, True, None))


def quarantine_unlisted_goes_live(s):
    path = s.folder('stray')

    def wake():
        s.midstage = lambda request: request['tasks']['active_worktrees'].append(str(path))
    return steps(s, ('started', 0, True, None), ('elapsed', 3, True, wake))


def touched_at_removal(s, path):
    """A file touched between selection and removal: the row's keys are equal, and only the fingerprint can refuse."""
    def touch():
        s.midstage = lambda request: s.age(path / 'README.md', DAY)
    return steps(s, ('started', 0, True, None), ('elapsed', 3, True, touch))


def quarantine_touched(s):
    return touched_at_removal(s, s.worktree('w', owner=None))


def quarantine_unlisted_touched(s):
    return touched_at_removal(s, s.folder('stray'))


def quarantine_unlisted_goes_busy(s):
    path = s.folder('stray')

    def wake():
        s.midstage = lambda request: s.busy_paths.add(str(path))
    return steps(s, ('started', 0, True, None), ('elapsed', 3, True, wake))


def quarantine_unlisted_registered(s):
    """Registered from mid-stage on, with no `.git` yet: only the proof at removal sees it, not a look at the folder."""
    path = s.folder('stray')

    def wake():
        s.midstage = lambda request: s.registered_only(path, 'stray')
    return steps(s, ('started', 0, True, None), ('elapsed', 3, True, wake))


def quarantine_unlisted_other_side(s):
    """A folder the other side owns, under a root this side can read: a WSL worker on a Windows drive, say."""
    s.foreign.add(str(s.folder('stray')))
    return steps(s, ('started', 0, True, None), ('elapsed', 3, True, None))


def quarantine_unlisted_holds_a_repository(s):
    """Git never lists a work tree's own `.git`, so only the look for one sees this is no plain folder."""
    s.folder('stray', extra=['.git/HEAD'])
    return steps(s, ('started', 0, True, None), ('elapsed', 3, True, None))


SEQUENCES = (quarantine, quarantine_reset, quarantine_identity, quarantine_unlisted, quarantine_unlisted_goes_live,
             quarantine_touched, quarantine_unlisted_touched, quarantine_unlisted_goes_busy, quarantine_unlisted_registered,
             quarantine_unlisted_other_side, quarantine_unlisted_holds_a_repository)


@deleting_behind()
def collect():
    trace = {}
    for name, build in SCENES.items():
        with Scene() as scene:
            options = build(scene)
            options = options if isinstance(options, dict) else {}
            for label, apply in (('dry', False), ('applied', True))[options.pop('applied_only', False):]:     # a report changes nothing
                trace[f'{name}.{label}'] = scene.record(apply=apply, **options)
    for sequence in SEQUENCES:
        with Scene() as scene:
            trace.update({f'{sequence.__name__}.{label}': row for label, row in sequence(scene).items()})
    return trace


def write():
    """One line per scenario, so the record stays small and a diff names the scenario that moved."""
    scenes = [f'{json.dumps(name)}: {json.dumps(scene, sort_keys=True)}' for name, scene in sorted(collect().items())]
    TRACE.write_bytes(('{\n' + ',\n'.join(scenes) + '\n}\n').encode('utf-8'))


def main(argv):
    if argv == ['--write']:
        write()
        return 0
    if len(argv) == 3 and argv[0] == '--compare':
        old, new = (json.loads(Path(path).read_text(encoding='utf-8')) for path in argv[1:])
        changes = compare(old, new)
        for change in changes:
            print(f'{change.direction}\t{change.scenario}\t{change.part}\t{json.dumps(change.old)} -> {json.dumps(change.new)}')
        return 1 if any(change.direction == 'towards_deletion' for change in changes) else 0
    print('usage: python -m tests.disk_trace --write | --compare OLD NEW', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
