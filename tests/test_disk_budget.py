"""The budget purge removes disposable paths, least recently used first, until WSL used space reaches the target."""
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from drover import config, disk, disk_budget, disk_cleanup, disk_side, disk_state, disk_worktrees, placement, placement_proof
from tests.disk_fixture import GIB, empty_result
from tests.disk_memory import MemoryWorktreeCase

HOUR = 3600
DAY = 86400
SIZE = 1000
BUDGET = dict(target_gb=225.0, windows_paths=[])


def row(path, age_s, size=SIZE, **extra):
    return dict(path=path, age_s=age_s, bytes=size, action='candidate', **extra)


class Plan(unittest.TestCase):
    def test_oldest_idle_goes_first(self):
        rows = [row('new', 3 * HOUR), row('old', 9 * DAY), row('mid', 2 * DAY)]
        self.assertEqual([r['path'] for r in disk_budget.plan(rows, 3 * SIZE)], ['old', 'mid', 'new'])

    def test_stops_once_target_met(self):
        rows = [row('new', 3 * HOUR), row('old', 9 * DAY), row('mid', 2 * DAY)]
        self.assertEqual([r['path'] for r in disk_budget.plan(rows, SIZE + 1)], ['old', 'mid'])
        self.assertEqual([r['path'] for r in disk_budget.plan(rows, SIZE)], ['old'])
        self.assertEqual(disk_budget.plan(rows, 0), [])

    def test_worktree_idle_outranks_the_age_of_one_file(self):
        rows = [row('old file, worktree in use today', 9 * DAY, idle_s=3 * HOUR),
                row('new file, worktree idle for days', 4 * DAY, idle_s=4 * DAY)]
        self.assertEqual(disk_budget.plan(rows, 1)[0]['path'], 'new file, worktree idle for days')

    def test_only_candidates_are_planned(self):
        self.assertEqual(disk_budget.plan([dict(row('kept', 9 * DAY), action='keep')], SIZE), [])

    def test_plan_leaves_its_input_alone(self):
        rows = [row('new', 3 * HOUR), row('old', 9 * DAY)]
        disk_budget.plan(rows, SIZE)
        self.assertEqual([r['path'] for r in rows], ['new', 'old'])

    def test_need_bytes(self):
        self.assertEqual(disk_budget.need_bytes(226, 225), GIB)
        self.assertEqual(disk_budget.need_bytes(225, 225), 0)
        self.assertEqual(disk_budget.need_bytes(100, 225), 0)


class BudgetCase(MemoryWorktreeCase):
    def setUp(self):
        super().setUp()
        self.cfg['trackers'][0].update(wsl_combined=True, host_floor_gb=10.0)
        self.free = 200.         # H: is healthy unless a test says otherwise
        self.outputs = []
        self.allowed = 0         # bytes of output that may stay at the target
        self.ttls = []
        self.clock = 0.
        self.read_cost = self.clock_step = 0.
        self.after_read = None
        # The coordinator's Windows snapshot is real down to the CIM query; a harmless editor elsewhere by default.
        self.windows_rows = [dict(pid=7, name='editor.exe', same_user=True, cwd=r'C:\work', cmd='editor.exe')]
        self.windows_failure = None

        def cim(request, current):
            return self.windows_failure or dict(rows=list(self.windows_rows), identities=current)

        def clock():
            self.clock += self.clock_step
            return self.clock
        for context in (patch.object(placement, 'wsl_used_gb', side_effect=self.reading),
                        patch.object(placement_proof, 'IS_WINDOWS', True),
                        patch.object(placement_proof, '_windows_processes',
                                     side_effect=lambda: {str(r['pid']): (r['name'], 1) for r in self.windows_rows}),
                        patch.object(placement_proof, '_cleanup_cim', side_effect=cim),
                        patch.object(disk_budget, '_clock', side_effect=clock)):
            context.start()
            self.addCleanup(context.stop)

    def reading(self, distro, up=True, *, ttl=60):
        self.ttls.append(ttl)
        self.clock += self.read_cost
        if self.after_read:
            self.after_read(len(self.ttls))
        left = sum(p.stat().st_size for p in self.outputs if p.exists())
        return 225 + (left - self.allowed) / GIB

    def output(self, name, idle_s, size=SIZE):
        tree = self.worktree(name)
        path = tree / 'generated' / 'result'
        path.parent.mkdir()
        path.write_bytes(b'x' * size)
        self.age(tree, idle_s)
        self.outputs.append(path)
        return path

    def claim(self, applied=True):
        def seed(state):
            state.setdefault('episodes', {})['wsl'] = dict(claim=dict(
                id='wsl-claim', identity=dict(pid=1, birth='x', side=self.side), applied=applied, at=self.now),
                last_cleanup=self.now)
        disk_state.update(self.state, seed)

    def episode(self):
        return disk_state.read(self.state)['episodes']['wsl']

    def receipt(self):
        return json.loads((self.state / 'disk' / 'result-wsl-claim.json').read_text(encoding='utf-8'))

    def purge(self):
        self.approve_report()
        self.claim()
        return self.run_cleanup(apply=True, mode='triggered')


class LocalSide(BudgetCase):
    """The whole path on this machine's own side: run, worker, serve, purge and the shared removal."""
    def setUp(self):
        super().setUp()
        side = patch.object(disk_budget, 'SIDE', self.side)
        side.start()
        self.addCleanup(side.stop)


class Purge(LocalSide):
    def test_oldest_idle_goes_first(self):
        new, old, mid = self.output('new', 3 * HOUR), self.output('old', 9 * DAY), self.output('mid', 2 * DAY)
        self.allowed = 2 * SIZE + 500
        self.purge()
        self.assertFalse(old.exists())
        self.assertTrue(mid.exists())
        self.assertTrue(new.exists())

    def test_stops_once_target_met(self):
        new, old, mid = self.output('new', 3 * HOUR), self.output('old', 9 * DAY), self.output('mid', 2 * DAY)
        self.allowed = SIZE + 500
        result = self.purge()
        self.assertEqual([p.exists() for p in (old, mid, new)], [False, False, True])
        self.assertEqual(self.receipt()['reclaimed_bytes'], 2 * SIZE)
        self.assertEqual(self.receipt()['shortfall_bytes'], 0)
        self.assertEqual(self.receipt()['budget'], 'met')
        self.assertFalse([i for i in result['items'] if i.get('reason_code') == 'budget_shortfall'])
        self.assertTrue([i for i in result['items'] if i.get('reason_code') == 'budget_not_needed' and i['path'] == str(new.parent)])

    def test_nothing_goes_when_already_at_target(self):
        path = self.output('idle', 9 * DAY)
        self.allowed = SIZE
        self.purge()
        self.assertTrue(path.exists())
        self.assertEqual(self.receipt()['budget'], 'met')

    def test_used_is_reread_uncached_after_each_batch(self):
        # Removing hard-linked files frees less than they measure: the first batch falls short of its estimate.
        first, second, third = self.output('a', 9 * DAY), self.output('b', 8 * DAY), self.output('c', 7 * DAY)
        readings = iter([225 + 500 / GIB, 225 + 500 / GIB, 225.])
        self.approve_report()
        self.claim()
        self.ttls.clear()

        def reading(distro, up=True, *, ttl=60):
            self.ttls.append(ttl)
            return next(readings)
        with patch.object(placement, 'wsl_used_gb', side_effect=reading):
            self.run_cleanup(apply=True, mode='triggered')
        self.assertEqual([p.exists() for p in (first, second, third)], [False, False, True])
        self.assertEqual(self.ttls, [0, 0, 0])

    def test_removing_one_folder_does_not_end_the_worktree_idle_clock(self):
        self.now = time.time()      # a real clock, so the removal's new folder time counts as fresh activity
        self.r.commit('.drover/config.toml', config.dumps({
            'schema_version': 1, 'cleanup': {'disposable': ['generated/a', 'generated/b']}}))
        tree = self.worktree('two folders')
        first, second = tree / 'generated' / 'a' / 'x', tree / 'generated' / 'b' / 'x'
        for path in (first, second):
            path.parent.mkdir(parents=True)
            path.write_bytes(b'x' * SIZE)
        self.age(tree, 9 * DAY)
        self.outputs += [first, second]
        self.purge()
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())

    def test_in_use_is_skipped_even_when_short(self):
        idle, editor, build = self.output('idle', 9 * DAY), self.output('editor', 8 * DAY), self.output('build', 7 * DAY)
        self.busy_paths.update({str(editor.parents[1]), str(build.parents[1])})
        result = self.purge()
        self.assertFalse(idle.exists())
        self.assertTrue(editor.exists())
        self.assertTrue(build.exists())
        self.assertEqual(self.receipt()['budget'], 'shortfall')
        self.assertEqual(self.receipt()['shortfall_bytes'], 2 * SIZE)
        self.assertTrue([i for i in result['items'] if str(editor.parents[1]) == i.get('path') and
                         'process using worktree' in i.get('error', '')])

    def test_worktree_that_starts_building_after_planning_keeps_its_paths(self):
        path = self.output('late', 9 * DAY)
        seen = []

        def busy(candidate):
            seen.append(candidate)
            return len(seen) > 1     # free when listed, in use when its removal is revalidated
        self.approve_report()
        self.claim()
        self.busy = busy
        self.run_cleanup(apply=True, mode='triggered')
        self.assertTrue(path.exists())
        self.assertEqual(self.receipt()['budget'], 'shortfall')

    def test_idle_under_two_hours_is_kept(self):
        young, old = self.output('young', 2 * HOUR - 60), self.output('old', 2 * HOUR + 60)
        self.purge()
        self.assertTrue(young.exists())
        self.assertFalse(old.exists())
        self.assertEqual(self.receipt()['shortfall_bytes'], SIZE)

    def test_idle_rule_holds_while_the_drive_is_at_hold(self):
        # The output stage waives the idle rule at hold and critical for a drive in trouble; a budget purge never does.
        self.free = 40.
        young = self.output('young', HOUR)
        self.purge()
        self.assertTrue(young.exists())

    def test_shortfall_is_reported_with_reason_budget_shortfall(self):
        path = self.output('idle', 9 * DAY)
        self.allowed = -(5 * GIB)
        self.approve_report()
        self.claim()
        self.ttls.clear()
        result = self.run_cleanup(apply=True, mode='triggered')
        self.assertFalse(path.exists())
        rows = [i for i in result['items'] if i.get('reason_code') == 'budget_shortfall']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['action'], 'report')
        self.assertEqual(rows[0]['shortfall_bytes'], 5 * GIB)
        self.assertEqual(rows[0]['target_gb'], 225.0)
        self.assertEqual(self.receipt()['budget'], 'shortfall')
        self.assertEqual(self.receipt()['shortfall_bytes'], 5 * GIB)
        self.assertLessEqual(len(self.ttls), 2, 'one reading before and one after the only batch')
        self.assertIsNone(self.episode()['claim'])

    def test_never_lists_non_disposable_paths(self):
        tree = self.worktree('mixed')
        self.r.commit('source.txt', 'committed', cwd=tree)
        output = tree / 'generated' / 'result'
        output.parent.mkdir()
        output.write_bytes(b'x' * SIZE)
        undeclared = tree / 'data' / 'precious'
        undeclared.parent.mkdir()
        undeclared.write_text('keep')
        note = tree / 'notes.txt'
        note.write_text('untracked work')
        (tree / 'source.txt').write_text('owner edit')
        tracked = tree / 'generated' / 'tracked'
        tracked.write_text('tracked output')
        self.r.track(tree, 'generated/tracked')
        self.age(tree, 30 * DAY)
        self.outputs.append(output)
        self.allowed = -(100 * GIB)       # the target is far out of reach
        result = self.purge()
        budget_rows = [i for i in result['items'] if i.get('stage') == 5]
        listed = {i.get('path') for i in budget_rows}
        for path in (undeclared, note, tree / 'source.txt', tracked, tree / '.git'):
            self.assertNotIn(str(path), listed)
            self.assertTrue(path.exists(), path)
        self.assertEqual((tree / 'source.txt').read_text(), 'owner edit')
        self.assertFalse(output.exists())
        self.assertTrue(tree.is_dir())

    def test_dry_run_changes_nothing(self):
        old, new = self.output('old', 9 * DAY), self.output('new', 3 * HOUR)
        self.allowed = SIZE + 500
        self.approve_report()
        self.claim(applied=False)
        result = self.run_cleanup(apply=False, mode='triggered')
        self.assertTrue(old.exists())
        self.assertTrue(new.exists())
        purge = [i for i in result['items'] if i.get('reason_code') == 'budget_not_needed']
        self.assertEqual([i['path'] for i in purge], [str(new.parent)])     # a report sees the whole ignored folder
        self.assertFalse(self.receipt()['applied'])
        self.assertEqual(self.receipt()['reclaimed_bytes'], 0)
        self.assertIsNone(self.episode()['claim'])

    def test_dry_run_projects_the_shortfall(self):
        self.output('old', 9 * DAY)
        self.allowed = -(GIB)
        self.approve_report()
        self.claim(applied=False)
        result = self.run_cleanup(apply=False, mode='triggered')
        rows = [i for i in result['items'] if i.get('reason_code') == 'budget_shortfall']
        self.assertEqual([r['shortfall_bytes'] for r in rows], [GIB])

    def test_unreadable_used_space_removes_nothing(self):
        path = self.output('idle', 9 * DAY)
        with patch.object(placement, 'wsl_used_gb', return_value=None):
            result = self.purge()
        self.assertTrue(path.exists())
        self.assertEqual(self.receipt()['budget'], 'unknown')
        self.assertTrue([i for i in result['items'] if i.get('reason_code') == 'budget_used_unknown'])

    def test_used_space_lost_midway_stops_the_purge(self):
        first, second = self.output('a', 9 * DAY), self.output('b', 8 * DAY)
        self.approve_report()
        self.claim()
        readings = iter([225 + 500 / GIB, None])
        with patch.object(placement, 'wsl_used_gb', side_effect=lambda *a, **k: next(readings)):
            self.run_cleanup(apply=True, mode='triggered')
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertEqual(self.receipt()['budget'], 'unknown')

    def test_apply_still_needs_a_matching_dry_report(self):
        path = self.output('idle', 9 * DAY)
        self.claim()
        with self.assertRaisesRegex(ValueError, 'dry report'):
            self.run_cleanup(apply=True, mode='triggered')
        self.assertTrue(path.exists())

    def test_ordinary_output_stage_is_unchanged_by_the_shared_removal(self):
        path = self.output('idle', 9 * DAY)
        self.free = 80.
        self.approve_report()
        self.run_cleanup(apply=True)
        self.assertFalse(path.exists())


class Folders(LocalSide):
    """A purge proves and removes one declared folder at a time."""
    def leaves(self, name, count, idle_s=9 * DAY):
        tree = self.worktree(name)
        paths = [tree / 'generated' / f'part-{i}' / 'file' for i in range(count)]
        for path in paths:
            path.parent.mkdir(parents=True)
            path.write_bytes(b'x' * SIZE)
        self.age(tree, idle_s)
        self.outputs += paths
        return tree, paths

    def statuses(self, tree, action=None):
        real, calls = disk_worktrees.status, []

        def status(path, **kw):
            if str(path) == str(tree):
                calls.append(kw)
                if action:
                    action(len(calls))
            return real(path, **kw)
        return patch.object(disk_worktrees, 'status', side_effect=status), calls

    def test_a_folder_is_proven_once_not_per_leaf(self):
        tree, paths = self.leaves('many', 40)
        self.approve_report()
        self.claim()
        watch, calls = self.statuses(tree)
        with watch:
            self.run_cleanup(apply=True, mode='triggered')
        self.assertEqual(len(calls), 2, 'one listing and one revalidation for 40 leaves')
        self.assertFalse((tree / 'generated').exists())
        self.assertEqual(self.receipt()['reclaimed_bytes'], 40 * SIZE)

    def test_ordinary_output_stage_still_proves_each_path(self):
        self.free = 80.
        tree, paths = self.leaves('many', 5)
        self.r.commit('pending.txt', cwd=tree)      # unmerged, so stage 4 leaves the worktree for stage 5
        self.age(tree, 9 * DAY)
        self.approve_report()
        watch, calls = self.statuses(tree)
        with watch:
            self.run_cleanup(apply=True)
        self.assertGreaterEqual(len(calls), 5)
        self.assertFalse([p for p in paths if p.exists()])

    def test_a_change_inside_a_folder_keeps_all_of_it(self):
        tree, paths = self.leaves('changing', 3)
        self.approve_report()
        self.claim()

        def rewrite(call):
            if call == 2:           # the build wrote one leaf after the folder was listed
                paths[1].write_bytes(b'y' * (2 * SIZE))
                self.age(paths[1], 9 * DAY)
        watch, _ = self.statuses(tree, rewrite)
        with watch:
            self.run_cleanup(apply=True, mode='triggered')
        self.assertEqual([p.exists() for p in paths], [True, True, True])
        self.assertEqual(self.receipt()['reclaimed_bytes'], 0)

    def test_a_protected_leaf_stays_and_the_rest_goes(self):
        tree, paths = self.leaves('protected', 2)
        kept = tree / 'generated' / '.venv' / 'keep'
        kept.parent.mkdir()
        kept.write_text('environment')
        self.age(tree, 9 * DAY)
        result = self.purge()
        self.assertFalse([p for p in paths if p.exists()])
        self.assertTrue(kept.exists())
        self.assertTrue([i for i in result['items'] if i['path'] == str(kept) and i['reason_code'] == 'protected'])

    def test_a_link_anywhere_keeps_the_worktree(self):
        # The idle proof walks the whole worktree and refuses a link, so nothing proves such a worktree idle.
        tree, paths = self.leaves('linked', 2)
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'keep').write_text('link target')
        link = tree / 'generated' / 'link'
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('symlinks unavailable')
        self.age(tree, 9 * DAY)
        self.purge()
        self.assertEqual([p.exists() for p in paths], [True, True])
        self.assertEqual((outside / 'keep').read_text(), 'link target')

    def test_used_is_read_after_each_folder(self):
        a, b, c = self.output('a', 9 * DAY), self.output('b', 8 * DAY), self.output('c', 7 * DAY)
        self.approve_report()
        self.claim()
        self.ttls.clear()
        self.run_cleanup(apply=True, mode='triggered')
        self.assertEqual(len(self.ttls), 4, 'one reading first and one after each of three folders')
        self.assertEqual(set(self.ttls), {0})
        self.assertFalse(a.exists() or b.exists() or c.exists())

    def test_stops_inside_a_batch_when_a_folder_frees_more_than_it_measured(self):
        a, b, c = self.output('a', 9 * DAY), self.output('b', 8 * DAY), self.output('c', 7 * DAY)
        readings = iter([225 + 3 * SIZE / GIB, 225.])
        self.approve_report()
        self.claim()
        with patch.object(placement, 'wsl_used_gb', side_effect=lambda *x, **k: next(readings)):
            self.run_cleanup(apply=True, mode='triggered')
        self.assertEqual([p.exists() for p in (a, b, c)], [False, True, True])
        self.assertEqual(self.receipt()['budget'], 'met')

    def test_folder_coming_into_use_mid_purge_is_kept(self):
        a, b, c = self.output('a', 9 * DAY), self.output('b', 8 * DAY), self.output('c', 7 * DAY)
        self.approve_report()
        self.claim()
        self.ttls.clear()

        def start_build(reads):
            if reads == 2:
                self.busy_paths.add(str(b.parents[1]))
        self.after_read = start_build
        result = self.run_cleanup(apply=True, mode='triggered')
        self.assertEqual([p.exists() for p in (a, b, c)], [False, True, False])
        self.assertEqual(self.receipt()['reclaimed_bytes'], 2 * SIZE)
        self.assertEqual(self.receipt()['budget'], 'shortfall')
        self.assertTrue([i for i in result['items'] if i['path'] == str(b.parent) and i['action'] == 'keep'])

    def test_deadline_stops_between_folders_and_reports_true_bytes(self):
        a, b, c = self.output('a', 9 * DAY), self.output('b', 8 * DAY), self.output('c', 7 * DAY)
        self.cfg['disk_cleanup']['worker_timeout_s'] = 100
        self.read_cost = 50.      # the deadline, 80 s, passes on the reading after the first folder
        result = self.purge()
        self.assertEqual([p.exists() for p in (a, b, c)], [False, True, True])
        receipt = self.receipt()
        self.assertEqual((receipt['budget'], receipt['reclaimed_bytes'], receipt['partial']), ('deadline', SIZE, True))
        self.assertIsNone(self.episode()['claim'])
        rows = [i for i in result['items'] if i['reason_code'] == 'budget_deadline']
        self.assertEqual([r['action'] for r in rows if r['path'] == str(self.state.parent)], ['report'])
        self.assertEqual({i['path'] for i in rows} - {str(self.root)}, {str(b.parent), str(c.parent)})
        report = next(i for i in rows if i['action'] == 'report')
        self.assertEqual((report['reclaimed_bytes'], report['deadline_s'], report['candidates_left']), (SIZE, 80.0, 2))

    def test_default_deadline_is_most_of_the_worker_timeout(self):
        self.output('a', 9 * DAY)
        self.output('b', 8 * DAY)
        self.read_cost = 1000.
        result = self.purge()
        self.assertEqual([i['deadline_s'] for i in result['items'] if i['action'] == 'report' and
                          i['reason_code'] == 'budget_deadline'], [0.8 * 1800])

    def test_deadline_in_a_first_batch_no_longer_loses_the_outcome(self):
        paths = [self.output(name, (9 - i) * DAY) for i, name in enumerate('abcd')]
        self.allowed = -(5 * GIB)     # one batch would hold every folder, and still fall short
        self.cfg['disk_cleanup']['worker_timeout_s'] = 100
        self.read_cost = 30.
        self.purge()
        gone = [p for p in paths if not p.exists()]
        self.assertEqual(gone, paths[:2])
        self.assertEqual(self.receipt()['reclaimed_bytes'], 2 * SIZE)
        self.assertEqual(self.receipt()['budget'], 'deadline')

    def test_deadline_while_gathering_removes_nothing_and_is_not_partial(self):
        paths = [self.output(name, 9 * DAY) for name in 'abc']
        self.approve_report()
        self.claim()
        self.clock_step = 1000.
        self.run_cleanup(apply=True, mode='triggered')
        self.assertEqual([p.exists() for p in paths], [True, True, True])
        self.assertEqual((self.receipt()['budget'], self.receipt()['partial']), ('deadline', False))


class WindowsProcesses(LocalSide):
    """The WSL worker cannot see Windows processes, so the coordinator looks and the purge keeps what it found."""
    SHARE = r'\\wsl.localhost\Disk Test Distro\home\me\wt'

    def relay(self, tree):
        """The worker's call as it travels, with only the spelling of the fixture's worktree changed: the fixture's
        trees are folders on this host, a real one is the same /home path on both sides of the call."""
        sent = []
        worker = self.runtime(real=True)

        def call(side, request):
            if request.get('budget'):
                sent.append(request['budget']['windows_paths'])
                named = [str(tree) + p.removeprefix('/home/me/wt') for p in request['budget']['windows_paths']]
                request = dict(request, budget=dict(request['budget'], windows_paths=named))
            return worker.call(side, request)
        return call, sent

    def test_windows_process_in_a_wsl_worktree_keeps_its_paths(self):
        used, idle = self.output('open-in-windows', 9 * DAY), self.output('idle', 8 * DAY)
        self.windows_rows.append(dict(pid=8, name='devenv.exe', same_user=True, cwd=self.SHARE + r'\src', cmd='devenv.exe'))
        self.approve_report()
        self.claim()
        call, sent = self.relay(used.parents[1])
        result = self.run_cleanup(apply=True, mode='triggered', handler=call)
        self.assertEqual(sent, [['/home/me/wt/src']])
        self.assertTrue(used.exists())
        self.assertFalse(idle.exists())
        self.assertEqual(self.receipt()['budget'], 'shortfall')
        self.assertTrue([i for i in result['items'] if 'windows process using worktree' in i.get('error', '')])

    def test_windows_command_line_naming_a_wsl_worktree_keeps_its_paths(self):
        used = self.output('built-from-windows', 9 * DAY)
        self.windows_rows.append(dict(pid=8, name='msbuild.exe', same_user=True, cwd=r'C:\work',
                                      cmd=f'msbuild.exe "{self.SHARE}\\app.sln"'))
        self.approve_report()
        self.claim()
        call, sent = self.relay(used.parents[1])
        self.run_cleanup(apply=True, mode='triggered', handler=call)
        self.assertEqual(sent, [['/home/me/wt/app.sln']])
        self.assertTrue(used.exists())

    def test_unknown_snapshot_purges_nothing_with_a_reason(self):
        path = self.output('idle', 9 * DAY)
        self.windows_rows = []
        self.windows_failure = dict(rows=[], error='timed out', reason_code='process_query_timeout')
        result = self.purge()
        self.assertTrue(path.exists())
        self.assertEqual(self.receipt()['budget'], 'processes_unknown')
        self.assertFalse(self.receipt()['applied'])
        self.assertTrue([i for i in result['items'] if i.get('reason_code') == 'budget_processes_unknown'])
        self.assertIsNone(self.episode()['claim'])

    def test_process_it_cannot_read_makes_the_snapshot_unknown(self):
        path = self.output('idle', 9 * DAY)
        self.windows_rows.append(dict(pid=9, name='secret.exe', same_user=True, cwd=None, cmd='secret.exe'))
        result = self.purge()
        self.assertTrue(path.exists())
        rows = [i for i in result['items'] if i.get('reason_code') == 'budget_processes_unknown']
        self.assertEqual([r['count'] for r in rows], [1])

    def test_failed_snapshot_purges_nothing(self):
        path = self.output('idle', 9 * DAY)
        with patch.object(placement, 'cleanup_windows_proof', side_effect=OSError('no cim')):
            self.purge()
        self.assertTrue(path.exists())
        self.assertEqual(self.receipt()['budget'], 'processes_unknown')

    def test_coordinator_off_windows_purges_nothing(self):
        path = self.output('idle', 9 * DAY)
        with patch.object(placement_proof, 'IS_WINDOWS', False):
            self.purge()
        self.assertTrue(path.exists())
        self.assertEqual(self.receipt()['budget'], 'processes_unknown')

    def test_worker_refuses_a_budget_without_a_windows_answer(self):
        path = self.output('idle', 9 * DAY)
        request = dict(stage=5, mode='triggered', apply=True, now=self.now, repos=disk_cleanup._repos(self.cfg),
                       drive=dict(path=str(self.root), level='ok'), paths=dict(temp_roots=[], report_roots=[], caches=[]),
                       state_dir=str(self.state), budget=dict(target_gb=225.0))
        proof = SimpleNamespace(busy=lambda path: False)
        with patch.object(placement, 'cleanup_processes', return_value=proof), \
                patch.object(placement, 'physical_disk_path', return_value=str(self.root)):
            answer = disk_side.serve(request)
        self.assertEqual(answer['budget']['status'], 'processes_unknown')
        self.assertTrue(path.exists())


class WslPaths(unittest.TestCase):
    def proof(self, *rows):
        return placement_proof._CleanupProcesses(windows=True, cim=lambda: [
            dict(pid=i + 1, name='tool.exe', same_user=True, cmd='tool.exe', cwd=r'C:\work', **row)
            if 'cmd' not in row and 'cwd' not in row else dict(dict(pid=i + 1, name='tool.exe', same_user=True,
                                                                     cmd='tool.exe', cwd=r'C:\work'), **row)
            for i, row in enumerate(rows)])

    def test_every_spelling_of_this_distro(self):
        proof = self.proof(dict(cwd=r'\\wsl$\Ubuntu\home\a'), dict(cwd='\\\\wsl.localhost\\UBUNTU\\home\\b\\'),
                           dict(cwd=r'\\?\UNC\wsl.localhost\Ubuntu\home\c'), dict(cwd='//wsl.localhost/Ubuntu/home/d'),
                           dict(cmd='wsl.exe --cd /home/e'), dict(cmd=r'tool.exe --path=\\wsl$\Ubuntu\home\f'))
        self.assertEqual(proof.wsl_paths('Ubuntu'), ['/home/a', '/home/b', '/home/c', '/home/d', '/home/e', '/home/f'])

    def test_linux_case_is_kept(self):
        self.assertEqual(self.proof(dict(cwd=r'\\wsl$\Ubuntu\Home\Mixed')).wsl_paths('Ubuntu'), ['/Home/Mixed'])

    def test_other_distros_and_windows_paths_are_left_out(self):
        proof = self.proof(dict(cwd=r'\\wsl$\Debian\home\a'), dict(cwd=r'C:\home\b'), dict(cwd='/mnt/c/home/c'))
        self.assertEqual(proof.wsl_paths('Ubuntu'), [])

    def test_unknown_snapshot_has_no_answer(self):
        def fail():
            raise OSError('no cim')
        self.assertIsNone(placement_proof._CleanupProcesses(windows=True, cim=fail).wsl_paths('Ubuntu'))


class OtherSide(BudgetCase):
    def test_windows_side_never_acts_on_a_budget(self):
        path = self.output('idle', 9 * DAY)
        request = dict(stage=5, mode='triggered', apply=True, now=self.now, repos=disk_cleanup._repos(self.cfg),
                       drive=dict(path=str(self.root), level='ok'), paths=dict(temp_roots=[], report_roots=[], caches=[]),
                       state_dir=str(self.state), budget=dict(target_gb=225.0))
        proof = SimpleNamespace(busy=lambda path: False)
        with patch.object(placement, 'cleanup_side', return_value='windows'), \
                patch.object(placement, 'cleanup_processes', return_value=proof), \
                patch.object(placement, 'wsl_used_gb') as reader, \
                patch.object(placement, 'physical_disk_path', return_value=str(self.root)):
            answer = disk_side.serve(request)
        self.assertEqual((answer['items'], answer['reclaimed_bytes']), ([], 0))
        reader.assert_not_called()
        self.assertTrue(path.exists())


class Coordinator(BudgetCase):
    """What disk_cleanup.run asks of the sides for a wsl claim; the sides themselves are not run."""
    def setUp(self):
        super().setUp()
        self.answer = dict(empty_result(), budget=dict(status='met', used_gb=224.0, target_gb=225.0, shortfall_bytes=0))
        self.approve_report()
        self.calls.clear()

    def handler(self, side, request):
        return self.answer

    def run_triggered(self, *, handler=None, apply=True):
        return self.run_cleanup(apply=apply, mode='triggered', real=False, handler=handler or self.handler)

    def asked(self):
        return [(side, request['stage'], request.get('budget')) for side, request in self.calls]

    def test_wsl_only_trigger_reaches_the_wsl_output_stage_alone(self):
        self.claim()
        self.run_triggered()
        self.assertEqual(self.asked(), [('wsl', 5, BUDGET)])
        request = self.calls[0][1]
        self.assertTrue(request['apply'])
        self.assertEqual(request['drive']['name'], 'test-drive')

    def test_configured_target_is_the_budget(self):
        self.cfg['disk_cleanup'].update(wsl_target_gb=150.0, wsl_purge_gb=200.0, wsl_stop_gb=300.0)
        self.approve_report()
        self.calls.clear()
        self.claim()
        self.run_triggered()
        self.assertEqual(self.asked(), [('wsl', 5, dict(BUDGET, target_gb=150.0))])

    def test_low_drive_without_its_own_claim_runs_no_drive_stages(self):
        self.free = 80.
        self.claim()
        self.run_triggered()
        self.assertEqual(self.asked(), [('wsl', 5, BUDGET)])

    def test_drive_claim_keeps_its_stages_and_they_carry_no_budget(self):
        self.free = 80.
        self.claim()

        def seed(state):
            state['episodes']['test-drive'] = dict(claim=dict(id='drive-claim', identity={}, applied=True, at=self.now))
        disk_state.update(self.state, seed)
        self.run_triggered()
        asked = self.asked()
        self.assertEqual(asked[-1], ('wsl', 5, BUDGET))
        self.assertEqual([a for a in asked if a[2]], [asked[-1]])
        self.assertEqual({a[1] for a in asked[:-1]}, {1, 2, 3, 4, 5, 6})
        self.assertIsNone(self.episode()['claim'])
        self.assertTrue((self.state / 'disk' / 'result-drive-claim.json').exists())

    def test_no_wsl_claim_means_no_budget(self):
        self.free = 80.
        self.run_triggered()
        self.assertTrue(self.calls)
        self.assertFalse([a for a in self.asked() if a[2]])

    def test_only_a_triggered_run_serves_the_claim(self):
        self.free = 80.
        self.claim()
        self.run_cleanup(apply=True, real=False, handler=self.handler)
        self.assertFalse([a for a in self.asked() if a[2]])
        self.assertIsNotNone(self.episode()['claim'])

    def test_receipt_clears_the_claim_and_sets_no_free_space_baseline(self):
        self.answer['reclaimed_bytes'] = 123
        self.claim()
        self.run_triggered()
        episode = self.episode()
        self.assertIsNone(episode['claim'])
        self.assertNotIn('post', episode)
        self.assertNotIn('report_post', episode)
        self.assertEqual(self.receipt(), dict(applied=True, status='completed', budget='met', used_gb=224.0,
                                              target_gb=225.0, reclaimed_bytes=123, shortfall_bytes=0, partial=False))

    def test_tick_also_settles_a_receipt_the_run_could_not_clear(self):
        self.claim()
        disk_state.save_file(self.state, 'result-wsl-claim.json', dict(applied=True, status='completed', budget='met'))
        with patch.object(placement, 'physical_disk_path', return_value=str(self.root)):
            disk.tick(self.cfg, now=self.now + 60, reader=lambda path: 200., wsl_used=lambda: 200., enqueue=lambda: None)
        self.assertIsNone(self.episode()['claim'])
        self.assertNotIn('post', self.episode())

    def test_unknown_drive_reading_stays_report_only(self):
        self.free = None
        self.claim()
        self.run_triggered()
        self.assertEqual([r['apply'] for s, r in self.calls if r.get('budget')], [False])
        self.assertFalse(self.receipt()['applied'])

    def test_failed_worker_still_ends_the_claim(self):
        self.claim()

        def fail(side, request):
            raise RuntimeError('worker gone')
        result = self.run_triggered(handler=fail)
        self.assertEqual(self.receipt()['budget'], 'failed')
        self.assertIsNone(self.episode()['claim'])
        self.assertIn('worker gone', json.dumps(result['errors']))

    def test_worker_answer_without_a_budget_is_a_failure(self):
        self.claim()
        self.answer = dict(empty_result(), errors=[dict(reason_code='worker_timeout')])
        self.run_triggered()
        self.assertEqual(self.receipt()['budget'], 'failed')

    def test_unconfirmed_termination_propagates(self):
        self.claim()

        def lost(side, request):
            error = RuntimeError('cannot confirm')
            error.termination_confirmed = False
            raise error
        with self.assertRaises(RuntimeError):
            self.run_triggered(handler=lost)
        self.assertIsNotNone(self.episode()['claim'])      # the dead-process path in tick releases it

    def test_no_wsl_host_drive_ends_the_claim_without_a_call(self):
        self.cfg['trackers'][0].pop('wsl_combined')
        self.cfg['trackers'][0].pop('host_floor_gb')
        self.approve_report()
        self.calls.clear()
        self.claim()
        self.run_triggered()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.receipt()['budget'], 'no_host')
        self.assertIsNone(self.episode()['claim'])

    def test_large_purge_is_trimmed_like_any_wsl_stage(self):
        self.answer['reclaimed_bytes'] = GIB
        self.claim()
        self.run_triggered()
        self.assertEqual(self.asked(), [('wsl', 5, BUDGET), ('windows', 1, None)])

    def test_dry_claim_keeps_reporting_every_stage(self):
        self.claim(applied=False)
        self.run_triggered(apply=False)
        budgeted = [a for a in self.asked() if a[2]]
        self.assertEqual(budgeted, [('wsl', 5, BUDGET)])
        self.assertEqual({r['apply'] for s, r in self.calls}, {False})


class Retrigger(BudgetCase):
    """A purge its deadline cut off comes back soon; every other cleanup waits out the cooldown."""
    def setUp(self):
        super().setUp()
        mapping = patch('drover.placement.physical_disk_path', return_value=str(self.root))
        mapping.start()
        self.addCleanup(mapping.stop)
        self.launches = []

    def after(self, seconds, used=310.):
        disk.tick(self.cfg, now=self.now + seconds, reader=lambda path: 200., wsl_used=lambda: used,
                  enqueue=lambda: self.launches.append(seconds))

    def settled(self, **extra):
        def seed(state):
            state.setdefault('episodes', {})['wsl'] = dict(claim=None, last_cleanup=self.now, **extra)
        disk_state.update(self.state, seed)

    def test_partial_purge_retries_at_thirty_minutes_not_before(self):
        self.settled(partial=True)
        self.after(29 * 60)
        self.assertEqual(self.launches, [])
        self.after(30 * 60)
        self.assertEqual(self.launches, [30 * 60])

    def test_retries_stay_thirty_minutes_apart(self):
        self.settled(partial=True)
        self.after(30 * 60)
        self.after(31 * 60)                                  # still claimed
        def finished(state):
            state['episodes']['wsl'].update(claim=None, partial=True)
        disk_state.update(self.state, finished)
        self.after(59 * 60)
        self.assertEqual(self.launches, [30 * 60])
        self.after(60 * 60)
        self.assertEqual(self.launches, [30 * 60, 60 * 60])

    def test_stop_level_also_retries(self):
        self.settled(partial=True)
        self.after(30 * 60, used=510.)
        self.assertEqual(len(self.launches), 1)

    def test_finished_purge_waits_the_full_cooldown(self):
        self.settled(partial=False)
        self.after(30 * 60)
        self.assertEqual(self.launches, [])
        self.after(6 * HOUR)
        self.assertEqual(self.launches, [6 * HOUR])

    def test_no_retry_once_under_budget(self):
        self.settled(partial=True)
        self.after(30 * 60, used=200.)
        self.assertEqual(self.launches, [])
        self.assertNotIn('partial', self.episode())

    def test_receipt_settled_by_tick_carries_partial(self):
        self.claim()
        disk_state.save_file(self.state, 'result-wsl-claim.json', dict(applied=True, budget='deadline', partial=True))
        self.after(60)
        self.assertTrue(self.episode()['partial'])

    def test_partial_comes_from_the_worker_answer(self):
        for reclaimed, partial in ((5, True), (0, False)):
            with self.subTest(reclaimed=reclaimed):
                self.approve_report()
                self.claim()
                answer = dict(empty_result(), reclaimed_bytes=reclaimed,
                              budget=dict(status='deadline', used_gb=300.0, target_gb=225.0, shortfall_bytes=GIB))
                self.run_cleanup(apply=True, mode='triggered', real=False, handler=lambda side, request: answer)
                self.assertEqual(self.episode()['partial'], partial)

    def test_dead_claim_gets_no_early_retry(self):
        self.settled(partial=True)

        def claimed(state):
            state['episodes']['wsl']['claim'] = dict(id='gone', identity=dict(pid=1, birth='absent', side=self.side),
                                                     applied=True, at=self.now)
        disk_state.update(self.state, claimed)
        self.after(60)
        self.assertNotIn('partial', self.episode())


if __name__ == '__main__':
    unittest.main()
