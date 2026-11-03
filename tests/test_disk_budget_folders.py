"""The budget purge in folders, on the other side, at the stop level and when it triggers again."""
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from drover import disk, disk_cleanup, disk_paths, disk_state, disk_worktrees, placement
from tests.disk_fixture import GIB, empty_result, served
from tests.disk_budget_fixture import BudgetCase, DAY, HOUR, LocalSide, SIZE


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

    def test_deadline_with_a_candidate_in_hand_still_names_the_unreached_worktrees(self):
        paths = [self.output(name, (9 - i) * DAY) for i, name in enumerate('abcd')]
        self.cfg['disk_cleanup']['worker_timeout_s'] = 100
        real = disk_paths.output_evidence

        def judged(*args, **options):       # each worktree judged takes 45 s of the stage's clock
            self.clock += 45.
            return real(*args, **options)
        # The 80 s line passes once the main checkout and `a` are judged, `a`'s candidate in hand.
        with patch.object(disk_paths, 'output_evidence', judged):
            result = self.purge()
        late = [i for i in result['items'] if i['reason_code'] == 'output_deadline']
        self.assertEqual(len(late), 1)
        self.assertEqual([Path(w).name for w in late[0]['worktrees']], ['b', 'c', 'd'])    # 'a' was reached
        self.assertEqual(self.receipt()['budget'], 'deadline')

    def test_deadline_while_gathering_removes_nothing_and_is_not_partial(self):
        paths = [self.output(name, 9 * DAY) for name in 'abc']
        self.approve_report()
        self.claim()
        self.clock_step = 1000.
        self.run_cleanup(apply=True, mode='triggered')
        self.assertEqual([p.exists() for p in paths], [True, True, True])
        self.assertEqual((self.receipt()['budget'], self.receipt()['partial']), ('deadline', False))


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
            answer = served(request)
        self.assertEqual((answer['items'], answer['reclaimed_bytes']), ([], 0))
        reader.assert_not_called()
        self.assertTrue(path.exists())


class StopLevel(LocalSide):
    """500 GB and over, from a real tick to a real receipt and back: what ended the run decides when it comes back."""
    def setUp(self):
        super().setUp()
        mapping = patch('drover.placement.physical_disk_path', return_value=str(self.root))
        mapping.start()
        self.addCleanup(mapping.stop)
        self.launches = []
        self.approve_report()
        disk_state.update(self.state, lambda state: state.update(automation=dict(apply=True)))
        self.calls.clear()

    def tick(self, seconds=0, used=510.):
        self.now += seconds
        disk.tick(self.cfg, now=self.now, reader=lambda path: 200., wsl_used=lambda: used,
                  enqueue=lambda: self.launches.append(self.now))

    def receipt(self):
        latest = max((self.state / 'disk').glob('result-*.json'), key=lambda path: path.stat().st_mtime_ns)
        return json.loads(latest.read_text(encoding='utf-8'))

    def claimed(self):
        self.tick()
        self.assertIsNotNone(self.episode()['claim'])
        return len(self.launches)

    def assert_next_launch(self, gap):
        launched = len(self.launches)
        self.tick(30)
        self.tick(gap - 31)
        self.assertEqual(len(self.launches), launched, 'no relaunch before the wait is over')
        self.tick(1)
        self.assertEqual(len(self.launches), launched + 1)

    def test_a_shortfall_with_nothing_to_remove_does_not_relaunch_every_tick(self):
        self.allowed = -(5 * GIB)
        self.claimed()
        self.run_cleanup(apply=True, mode='triggered')
        self.assertEqual((self.receipt()['budget'], self.receipt()['reclaimed_bytes']), ('shortfall', 0))
        self.assert_next_launch(6 * HOUR)

    def test_a_shortfall_that_freed_bytes_comes_back_at_the_floor(self):
        self.output('idle', 9 * DAY)
        self.allowed = -(5 * GIB)
        self.claimed()
        self.run_cleanup(apply=True, mode='triggered')
        self.assertEqual((self.receipt()['budget'], self.receipt()['reclaimed_bytes']), ('shortfall', SIZE))
        self.assert_next_launch(30 * 60)

    def test_outcomes_that_freed_nothing_wait_out_the_cooldown(self):
        self.claimed()
        for status in ('processes_unknown', 'unknown', 'failed', 'deadline'):
            with self.subTest(status=status):
                answer = dict(empty_result(), budget=dict(status=status, used_gb=510., target_gb=225., shortfall_bytes=GIB))
                if status == 'failed':
                    answer = empty_result()
                self.run_cleanup(apply=True, mode='triggered', real=False, handler=lambda side, request: answer)
                self.assertEqual(self.receipt()['budget'], status)
                self.assert_next_launch(6 * HOUR)    # which claims again, for the next outcome

    def test_a_failed_purge_waits_by_its_last_reclaimed_total(self):
        """Ruling (Task 33): a failed or timed-out purge reports its last `reclaimed` total. One that freed bytes
        comes back at the 30-minute floor; one that freed nothing waits out the full cooldown."""
        self.claimed()
        for reclaimed, gap in ((SIZE, 30 * 60), (0, 6 * HOUR)):
            with self.subTest(reclaimed=reclaimed):
                failed = dict(empty_result(), errors=[dict(reason_code='worker_timeout', termination_confirmed=True)],
                              reclaimed_bytes=reclaimed, complete=False, partial=True)    # cleanup_call's, cut off
                self.run_cleanup(apply=True, mode='triggered', real=False, handler=lambda side, request: failed)
                self.assertEqual((self.receipt()['budget'], self.receipt()['reclaimed_bytes']), ('failed', reclaimed))
                self.assert_next_launch(gap)        # which claims again, for the next outcome

    def test_a_report_only_claim_does_not_relaunch_every_tick(self):
        disk_state.update(self.state, lambda state: state.update(automation=dict(apply=False)))
        self.allowed = -(5 * GIB)
        self.claimed()
        self.run_cleanup(apply=False, mode='triggered')
        self.assert_next_launch(6 * HOUR)

    def test_a_dead_claim_waits_out_the_cooldown(self):
        self.claimed()
        with patch.object(placement, 'cleanup_claim_alive', return_value=False):
            self.tick(60)
            self.assertIsNone(self.episode()['claim'])
            self.assert_next_launch(6 * HOUR)

    def test_the_wait_counts_from_the_end_of_the_purge(self):
        self.output('idle', 9 * DAY)
        self.allowed = -(5 * GIB)
        self.claimed()
        self.now += 25 * 60                 # the purge took 25 minutes
        self.run_cleanup(apply=True, mode='triggered')
        self.assertEqual(self.episode()['last_cleanup'], self.now)
        self.assert_next_launch(30 * 60)


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
