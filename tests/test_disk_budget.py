"""The budget purge removes disposable paths, least recently used first, until WSL used space reaches the target."""
import json
import time
import unittest
from unittest.mock import patch

from drover import config, disk, disk_budget, disk_cleanup, disk_state, ledger, placement
from drover.ledger import disk_notices
from tests.disk_fixture import GIB
from tests.disk_budget_fixture import DAY, HOUR, LocalSide, SIZE, row


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


class Notices(LocalSide):
    """Every outcome but `met` reaches the Inbox through the disk outbox, once per outcome per episode."""
    def notices(self):
        return [entry['notice'] for entry in disk_state.read(self.state).get('outbox', {}).values()
                if entry['notice']['kind'] == 'wsl_budget']

    def titles(self):
        return sorted(notice['title'] for notice in self.notices())

    def fire(self, outcome):
        disk_state.update(self.state, lambda state: disk.budget_notice(state, outcome))

    def test_each_outcome_raises_its_own_notice(self):
        outcomes = ['no_host', 'processes_unknown', 'shortfall', 'deadline', 'unknown', 'failed', 'died']
        for outcome in outcomes:
            self.fire(outcome)
        self.assertEqual(self.titles(), sorted('WSL disk budget not met: ' + outcome for outcome in outcomes))

    def test_met_raises_nothing(self):
        self.output('idle', 9 * DAY)
        self.purge()
        self.assertEqual(self.receipt()['budget'], 'met')
        self.assertEqual(self.notices(), [])

    def test_the_same_outcome_does_not_fire_every_run(self):
        self.output('idle', 9 * DAY)
        self.allowed = -GIB
        for _ in range(3):
            self.approve_report()
            self.claim()
            self.run_cleanup(apply=True, mode='triggered')
        self.assertEqual(self.receipt()['budget'], 'shortfall')
        self.assertEqual(self.titles(), ['WSL disk budget not met: shortfall'])

    def test_a_new_episode_notifies_again_under_a_new_id(self):
        self.fire('shortfall')
        first = self.notices()[0]['id']
        self.fire('met')
        self.fire('shortfall')
        ids = {notice['id'] for notice in self.notices()}
        self.assertEqual(len(ids), 2)
        self.assertIn(first, ids)

    def test_a_healthy_reading_ends_the_episode(self):
        with patch.object(placement, 'physical_disk_path', return_value=str(self.root)):
            self.claim()
            disk_state.update(self.state, lambda state: state['episodes']['wsl'].update(claim=None))
            self.fire('shortfall')
            disk.tick(self.cfg, now=self.now, reader=lambda path: 200., wsl_used=lambda: 200., enqueue=lambda: None)
        self.assertNotIn('wsl', disk_state.read(self.state)['notice_episodes'])

    def test_a_report_only_claim_notifies_too(self):
        self.output('old', 9 * DAY)
        self.allowed = -GIB
        self.approve_report()
        self.claim(applied=False)
        self.run_cleanup(apply=False, mode='triggered')
        self.assertIn('Report-only', self.notices()[0]['body'])

    def test_it_is_retried_when_the_state_is_busy(self):
        self.output('old', 9 * DAY)
        self.allowed = -GIB
        self.approve_report()
        self.claim()
        with patch.object(disk_cleanup, '_report_notices', side_effect=BlockingIOError('busy')):
            self.run_cleanup(apply=True, mode='triggered')
        self.assertEqual(self.notices(), [])
        self.assertTrue(list((self.state / 'disk').glob('pending-notices-*.json')))
        disk_cleanup.retry_notices(self.cfg)
        self.assertEqual(self.titles(), ['WSL disk budget not met: shortfall'])

    def test_a_dead_claim_raises_one(self):
        self.claim()
        with patch.object(placement, 'physical_disk_path', return_value=str(self.root)), \
                patch.object(placement, 'cleanup_claim_alive', return_value=False):
            disk.tick(self.cfg, now=self.now + 60, reader=lambda path: 200., wsl_used=lambda: 310., enqueue=lambda: None)
        self.assertEqual(self.titles(), ['WSL disk budget not met: died'])

    def test_the_notice_reaches_the_inbox(self):
        self.fire('shortfall')
        conn = ledger.connect(self.state / 'l.db')
        self.addCleanup(conn.close)
        self.assertEqual(len([i for i in disk_notices.replay(conn, self.cfg, self.now) if i.startswith('wsl_budget:')]), 1)
        self.assertEqual(conn.execute("SELECT title FROM questions").fetchone()[0], 'WSL disk budget not met: shortfall')


if __name__ == '__main__':
    unittest.main()
