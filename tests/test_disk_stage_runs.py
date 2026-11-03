"""Stages 2, 3 and 6 on the run: each stops at the stage clock's line with its one deadline row, and stage 6 builds each
row whole before it is added. The clock is faked through `clock=`; git, ownership and processes at their seams."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from drover import disk_inventory, disk_worktrees, placement, process
from drover.disk_paths import caches, report
from drover.disk_temp import temporary
from tests.disk_fixture import DAY, listing_answers, staged

LATE = 10 ** 6      # far past any line


def at(*times):
    """A clock reading 0 at the stage's start, then each of `times` in turn, holding the last."""
    readings = [0.0, *times]
    return lambda: readings.pop(0) if len(readings) > 1 else readings[0]


class Lines(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='disk-stage-runs-')).absolute()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.now = os.path.getmtime(self.root) + 60 * DAY
        self.clock = [0.0]      # a clock a test moves past the line mid-stage
        on_drive = patch.object(placement, 'physical_disk_path', return_value=str(self.root))
        on_drive.start()
        self.addCleanup(on_drive.stop)

    def old(self, *names):
        for name in names:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('stale', encoding='utf-8')
        return self.root / names[0]

    def request(self, stage, apply=True, repos=(), **paths):
        return dict(stage=stage, apply=apply, now=self.now, drive=dict(path=str(self.root)), repos=list(repos),
                    state_dir=str(self.root / 'state'), tasks={}, paths=dict(worker_timeout_s=100, **paths))

    def proof(self, jump_at=None):
        """No process holds anything; the proof numbered `jump_at`, from 1, is taken as the line passes."""
        taken = []
        def query(**_):
            taken.append(None)
            if len(taken) == jump_at:
                self.clock[0] = LATE
            return SimpleNamespace(busy=lambda path: False)
        return patch.object(placement, 'cleanup_processes', side_effect=query)

    def assert_stopped(self, answer, reason, repos=()):
        [stop] = [row for row in answer['items'] if row['action'] == 'report' and row['reason_code'].endswith('_deadline')]
        self.assertEqual((stop['reason_code'], stop['path'], stop['worktrees'], stop['repos']),
                         (reason, str(self.root) if not repos else repos[0], [], list(repos)))
        self.assertTrue(answer['partial'])

    def test_stage_2_stops_at_the_line_with_temp_deadline(self):
        stale = self.old('tmp/a')
        with self.proof():
            answer = staged(temporary, self.request(2, temp_roots=[str(stale.parent)]), clock=at(LATE))
        self.assert_stopped(answer, 'temp_deadline')
        self.assertEqual([row['reason_code'] for row in answer['items']], ['temp_deadline'])
        self.assertTrue(stale.exists())
        # The line passes at the first entry's fresh proof (the first pass's is proof 1): its last look is cut short, so
        # it keeps, the next entry is never reached, and the stage stops rather than ending complete.
        self.old('tmp/b')
        with self.proof(jump_at=2):
            answer = staged(temporary, self.request(2, temp_roots=[str(stale.parent)]), clock=lambda: self.clock[0])
        self.assert_stopped(answer, 'temp_deadline')
        entries = [row for row in answer['items'] if row['action'] != 'report']
        self.assertEqual([(row['action'], row['reason_code']) for row in entries], [('keep', 'temp_root_partial')])
        self.assertTrue(stale.exists() and (self.root / 'tmp/b').exists())

    def test_stage_3_stops_at_the_line_with_cache_deadline(self):
        first = self.old('cache/a', 'cache/b')
        request = self.request(3, caches=[dict(path=str(first.parent))])
        with self.proof():
            answer = staged(caches, request, clock=at(LATE))
        self.assert_stopped(answer, 'cache_deadline')
        self.assertEqual([row['reason_code'] for row in answer['items']], ['cache_deadline'])
        self.assertEqual(sorted(p.name for p in first.parent.iterdir()), ['a', 'b'])
        # The line passes while the first removal is proven: it stays, as stage 2's would, and the next never starts.
        with self.proof(jump_at=1):
            answer = staged(caches, request, clock=lambda: self.clock[0])
        self.assert_stopped(answer, 'cache_deadline')
        self.assertEqual([(row['action'], row['reason_code']) for row in answer['items'] if row['action'] != 'report'],
                         [('keep', 'removal_deadline')])
        self.assertEqual(sorted(p.name for p in first.parent.iterdir()), ['a', 'b'])

    def test_stage_3_runs_no_declared_command_past_the_line(self):
        command = dict(argv=['never-run'], cwd=str(self.root), placement=placement.cleanup_side())
        with patch.object(process, 'run', side_effect=AssertionError('ran past the line')):
            answer = staged(caches, self.request(3, caches=[command]), clock=at(LATE))
        self.assert_stopped(answer, 'cache_deadline')
        self.assertEqual([row['reason_code'] for row in answer['items']], ['cache_deadline'])

    def test_stage_2_checks_the_line_before_each_entry(self):
        stale = self.old('tmp/a')
        with self.proof():      # past the line after the root's check, before its first entry's
            answer = staged(temporary, self.request(2, temp_roots=[str(stale.parent)]), clock=self.after(2))
        self.assert_stopped(answer, 'temp_deadline')
        self.assertEqual([row['reason_code'] for row in answer['items']], ['temp_root_elapsed', 'temp_deadline'])

    def after(self, count):
        """A clock reading 0 for the stage's start and `count` more readings, then LATE."""
        readings = iter(range(count + 1))
        return lambda: 0.0 if next(readings, None) is not None else LATE

    def test_stage_6_checks_the_line_before_each_unlisted_folder_and_report(self):
        machine = dict(name='alpha', repo=str(self.root / 'repo'), worktrees=str(self.root / 'trees'))
        self.old('trees/stray/f', 'reports/a')
        def listed(repo):
            self.clock[0] = LATE        # past the line once the repo is listed
            return []
        with listing_answers(listed):
            answer = staged(report, self.request(6, apply=False, repos=[machine]), clock=lambda: self.clock[0])
        self.assertEqual([(row['reason_code'], row['path'], row.get('repos')) for row in answer['items']],
                         [('report_deadline', machine['repo'], [])])        # stopped in its repo, none left after it
        answer = staged(report, self.request(6, apply=False, report_roots=[str(self.root / 'reports')]), clock=at(LATE))
        self.assert_stopped(answer, 'report_deadline')
        self.assertEqual([row['reason_code'] for row in answer['items']], ['report_deadline'])

    def test_stage_6_stops_at_the_line_with_report_deadline(self):
        self.old('reports/a')
        machine = dict(name='alpha', repo=str(self.root / 'repo'))
        request = self.request(6, apply=False, repos=[machine], report_roots=[str(self.root / 'reports')])
        with listing_answers(lambda repo: self.fail('listed past the line')):
            answer = staged(report, request, clock=at(LATE))
        self.assert_stopped(answer, 'report_deadline', repos=[machine['repo']])
        self.assertEqual([row['reason_code'] for row in answer['items']], ['report_deadline'])

    def report(self, *, measure=None, status=None):
        """Stage 6 over one live drover task's merged worktree in a budgeted repo, on this side."""
        tree = self.root / 'w'
        tree.mkdir()
        machine = dict(name='alpha', repo=str(self.root / 'repo'), budget_gb=1)
        side = placement.cleanup_side()
        with listing_answers([dict(worktree=str(tree), locked=side)]), \
                patch.object(placement, 'cleanup_contains', return_value=True), \
                patch.object(placement, 'worktree_side', return_value=side), \
                patch('drover.ownership.read', return_value=dict(owner='drover:7')), \
                patch.object(disk_worktrees, 'task_facts', return_value=(True, False)), \
                patch.object(disk_worktrees, 'head_at', return_value='h'), \
                patch.object(disk_worktrees, 'merged', return_value=True), \
                patch.object(disk_worktrees, 'status', side_effect=status or (lambda path, **_: ([], []))), \
                patch.object(disk_inventory.Inventory, 'measure', autospec=True,
                             side_effect=measure or (lambda self, path, **_: dict(files=1, bytes=2, age_s=3, largest=[]))):
            answer = staged(report, self.request(6, apply=False, repos=[machine]))
        return {row['reason_code']: row for row in answer['items']}, tree

    def test_report_rows_are_built_before_add(self):       # no append then update or pop at disk_paths :442 :457 :465 :475
        def status(path, **_):
            raise ValueError('status failed')
        rows, tree = self.report(status=status)
        self.assertEqual(rows['worktree_inventory']['status_error'], 'status failed')
        self.assertEqual((rows['merged_live_task']['task_id'], rows['merged_live_task']['repo']), ('7', 'alpha'))
        self.assertEqual((rows['budget_usage']['action'], rows['budget_usage']['identity'], rows['budget_usage']['repo']),
                         ('report', placement.cleanup_path(tree), 'alpha'))

    def test_incomplete_budget_usage_never_leaves_the_worker(self):
        rows, _ = self.report(measure=lambda self, path, **_: dict(files=1, bytes=2, age_s=3, largest=[], incomplete=True))
        self.assertNotIn('budget_usage', rows)
        self.assertTrue(rows['worktree_inventory']['incomplete'])


if __name__ == '__main__':
    unittest.main()
