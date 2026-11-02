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
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from drover import disk_budget, disk_paths, disk_side, disk_worktrees, placement, placement_git
from tests.disk_fixture import DAY
from tests.disk_trace_scene import FEATURE, NOW, TIP, SceneTree, TraceGit  # noqa: F401  TraceGit is part of this interface
from tests.placement_trace import normalize

TRACE = Path(__file__).with_name('disk_decision_trace.json')
PATCH_TARGETS = (placement_git, disk_paths)     # each module whose `subprocess` TraceGit replaces; Tasks 15, 16 repoint it
REMOVAL = (disk_worktrees, 'remove')            # where a mid-stage change lands: after selection, before revalidation


class Scene(SceneTree):
    """One serve call at a time over a scene's tree; the record of each is host-neutral."""
    def physical(self, path):
        return 'other-drive' if any(placement.cleanup_parts(path)[:len(placement.cleanup_parts(p))]
                                    == placement.cleanup_parts(p) for p in self.elsewhere) else str(self.root)

    def proof(self, **_):
        return SimpleNamespace(busy=self.busy, reason=self.process_reason)

    def serve(self, apply, now, approved, timeout, tasks):
        request = dict(stage=4, mode='on-demand', apply=apply, now=now, drive=dict(path=str(self.root), name='trace'),
                       repos=self.machines, paths=dict(self.cfg['disk_cleanup'], worker_timeout_s=timeout),
                       state_dir=str(self.state), tasks={'active_worktrees': [], 'tasks': [], 'at': now, **tasks})
        if approved:
            request['approved_path'] = str(approved)
        real = getattr(*REMOVAL)

        def remove(request, selected, **options):
            change, self.midstage = self.midstage, None
            if change:
                change(request)
            return real(request, selected, **options)

        patches = [patch.object(module, 'subprocess', self.git) for module in PATCH_TARGETS]
        patches += [patch.object(*REMOVAL, remove), patch.object(placement, 'cleanup_processes', self.proof),
                    patch.object(placement, 'physical_disk_path', self.physical),
                    patch.object(disk_budget, '_clock', lambda: self.clock),
                    patch('uuid.uuid4', return_value=SimpleNamespace(hex='0' * 32))]
        for item in patches:
            item.start()
        try:
            return disk_side.serve(request)
        finally:
            for item in reversed(patches):
                item.stop()

    def tree(self):
        return {Path(folder, name) for folder, dirs, files in os.walk(self.root) for name in dirs + files
                if self.state not in Path(folder, name).parents and Path(folder, name) != self.state}

    def journal(self):
        manifest = self.state / 'disk-cleanup-manifest.jsonl'
        rows = manifest.read_text(encoding='utf-8').splitlines() if manifest.exists() else []
        fresh, self.journal_seen = rows[self.journal_seen:], len(rows)
        return [json.loads(row) for row in fresh]

    def record(self, *, apply, now=NOW, approved=None, timeout=1800, tasks=None):
        before, self.clock = self.tree(), 0.
        answer = self.serve(apply, now, approved, timeout, tasks or {})
        gone = before - self.tree()
        rows = [frozen_row(row) for row in answer['items']]
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


def frozen_row(row):
    """(path, action, reason_code), a deadline row's count, and head, branch, side_lock where a row may delete."""
    frozen = {key: row[key] for key in ('path', 'action', 'reason_code', 'count') if key in row}
    if row['action'] in ('candidate', 'removed'):
        frozen.update({key: row.get(key) for key in ('head', 'branch', 'side_lock')})
    return frozen


def active(path):
    return dict(tasks=dict(active_worktrees=[str(path)]))


def terminal(task, state):
    return dict(tasks=dict(tasks=[dict(id=task, terminal=True, live=False, state=state)]))


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


def identity_changed(s):
    """The registration moves between removal's two readings of it."""
    s.worktree('w')
    s.midstage = lambda request: setattr(s.git, 'after_status', lambda: s.move('w'))


def revalidation_dirty(s):
    path = s.worktree('w')
    s.midstage = lambda request: s.file(path / 'new.txt')


def revalidation_registration(s):
    s.worktree('w')
    s.midstage = lambda request: s.move('w')


def prune_expiry_unset(s):
    (s.common() / 'config').write_text('[core]\n\tbare = false\n', encoding='utf-8')


def removal_fails(s):
    s.worktree('w')
    s.refuse('worktree remove', 'fatal: removal failed')


def lost_registration(s):
    s.lost('lost')
    (s.common() / 'refs/heads/lost').write_text(TIP + '\n', encoding='utf-8')


def repo_cannot_be_listed(s):
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
    s.worktree('other', commit=FEATURE)
    return dict(approved=s.worktree('w', commit=FEATURE))


# Each is built twice, dry and applied; what a build returns, when a dict, goes to the record. The main checkout's own
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
    protected=lambda s: s.worktree('w', at=s.root / 'elsewhere' / '.venv' / 'w'),
    missing=missing,
    dirty=lambda s: s.worktree('w', extra=['scratch.txt']),
    status_fails=status_fails,
    orphan=lambda s: s.worktree('w', owner=None),
    settled=lambda s: s.worktree('w'),
    settled_detached=lambda s: s.worktree('w', detached=True),
    settled_terminal=lambda s: s.worktree('w', commit=FEATURE, owner='drover:T1') and terminal('T1', 'landed'),
    closed_and_recent=lambda s: s.age(s.worktree('w', commit=FEATURE, owner='drover:T1'), DAY) and terminal('T1', 'yanked'),
    unsettled=lambda s: s.worktree('w', commit=FEATURE),
    ignored_content_orphan=lambda s: ignored(s, 'cache', owner=None),
    ignored_content_at_removal=lambda s: ignored(s, 'cache'),
    ignored_declared=ignored_declared,
    nested_worktree=nested_worktree,
    under_main_checkout=lambda s: s.worktree('w', at=s.common().parent / 'sub' / 'w'),
    identity_changed=identity_changed,
    revalidation_dirty=revalidation_dirty,
    revalidation_registration=revalidation_registration,
    revalidation_locked_by_protect=lambda s: s.worktree('w', lock=None),
    prune_expiry_unset=prune_expiry_unset,
    removal_fails=removal_fails,
    unlisted=lambda s: s.folder('stray'),
    unlisted_untracked=lambda s: s.folder('stray', extra=['scratch.txt']),
    unlisted_live=lambda s: active(s.folder('stray')),
    unlisted_not_a_folder=lambda s: s.file(s.root / 'worktrees/alpha.worktrees/stray.txt'),
    lost_registration=lost_registration,
    lost_registration_unproven=lambda s: s.lost('lost'),
    repo_cannot_be_listed=repo_cannot_be_listed,
    registrations_unknown=registrations_unknown,
    settled_deadline_at_a_repo=settled_deadline_at_a_repo,
    settled_deadline_at_a_worktree=settled_deadline_at_a_worktree,
    approved_removal=approved_removal,
    approved_past_the_deadline=lambda s: dict(approved=s.worktree('w', owner=None), timeout=0),
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


SEQUENCES = (quarantine, quarantine_reset, quarantine_identity, quarantine_unlisted, quarantine_unlisted_goes_live)


def collect():
    trace = {}
    for name, build in SCENES.items():
        with Scene() as scene:
            options = build(scene)
            for label, apply in (('dry', False), ('applied', True)):     # a report changes nothing, so one tree serves both
                trace[f'{name}.{label}'] = scene.record(apply=apply, **(options if isinstance(options, dict) else {}))
    for sequence in SEQUENCES:
        with Scene() as scene:
            trace.update({f'{sequence.__name__}.{label}': row for label, row in sequence(scene).items()})
    return trace


@dataclass(frozen=True)
class Change:
    scenario: str
    part: str
    old: object
    new: object
    direction: str


EMPTY = dict(rows=[], errors=[], journal=[], deleted=[])
WEIGHT = dict(candidate=1, removed=2)


def weight(row):
    return WEIGHT.get(row['action'], 0)


def row_direction(old, new):
    if new is None:     # a kept row gone means a protection may be gone with it
        return 'towards_keep' if old and weight(old) else 'towards_deletion'
    if old is None:
        return 'towards_deletion' if weight(new) else 'report_added'
    if weight(new) != weight(old):
        return 'towards_deletion' if weight(new) > weight(old) else 'towards_keep'
    if new['reason_code'] == old['reason_code']:
        return 'neutral'
    return 'towards_deletion' if weight(new) else 'towards_keep'      # a removal on a new ground is never assumed equal


def paired(before, after):
    """Rows by path and reason, then what is left by path, so a changed reason reads as one changed row."""
    left, pairs, unmatched = list(after), [], []
    for row in before:
        match = next((r for r in left if (r['path'], r['reason_code']) == (row['path'], row['reason_code'])), None)
        (left.remove(match), pairs.append((row, match))) if match else unmatched.append(row)
    for row in unmatched:
        match = next((r for r in left if r['path'] == row['path']), None)
        if match:
            left.remove(match)
        pairs.append((row, match))
    return pairs + [(None, row) for row in left]


def lost_and_new(before, after):
    old, new = Counter(json.dumps(r, sort_keys=True) for r in before), Counter(json.dumps(r, sort_keys=True) for r in after)
    return [json.loads(r) for r in (old - new).elements()], [json.loads(r) for r in (new - old).elements()]


def compare_scene(name, before, after):
    changes = [Change(name, 'row', old, new, row_direction(old, new)) for old, new in paired(before['rows'], after['rows'])
               if old != new]
    lost, new = lost_and_new(before['errors'], after['errors'])
    changes += [Change(name, 'error', row, None, 'towards_deletion') for row in lost]
    changes += [Change(name, 'error', None, row, 'report_added') for row in new]
    lost, new = lost_and_new(before['journal'], after['journal'])
    done = {r['path'] for r in after['journal'] if r['phase'] == 'completed'}
    changes += [Change(name, 'journal', row, None, 'towards_deletion' if row['phase'] != 'completed' and row['path'] in done
                       else 'towards_keep') for row in lost]
    changes += [Change(name, 'journal', None, row, 'towards_deletion' if row['phase'] == 'completed' else 'report_added')
                for row in new]
    changes += [Change(name, 'deleted', path, None, 'towards_keep') for path in before['deleted'] if path not in after['deleted']]
    changes += [Change(name, 'deleted', None, path, 'towards_deletion') for path in after['deleted'] if path not in before['deleted']]
    return changes


def compare(old, new):
    """Every difference between two recordings with its direction. Where a difference cannot be shown harmless, it stops."""
    return [change for name in sorted({*old, *new}) for change in compare_scene(name, old.get(name, EMPTY), new.get(name, EMPTY))]


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
