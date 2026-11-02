"""The recorded decision trace, and the comparison that stops a slice moving it towards deletion."""
import ast
import inspect
import json
import unittest

from drover.disk_cleanup import decide
from drover.disk_worktrees import identity
from tests.disk_trace import TRACE, Scene, collect, compare
from tests.disk_trace_compare import HOLDING, KEPT, ON_THE_CLOCK

TRACE_DATA = json.loads(TRACE.read_text(encoding='utf-8'))
EMPTY = dict(rows=[], errors=[], journal=[], deleted=[])


def reasons_in_decide():
    """Every reason code decide can give, read from its source, so a new code needs a scenario."""
    found = set()
    for node in ast.walk(ast.parse(inspect.getsource(decide))):
        if isinstance(node, ast.keyword) and node.arg == 'reason_code' and isinstance(node.value, ast.Constant):
            found.add(node.value.value)
        if isinstance(node, ast.Assign) and getattr(node.targets[0], 'id', '') == 'blocked':
            for pair in (t for t in ast.walk(node.value) if isinstance(t, ast.Tuple) and len(t.elts) == 2):
                found.update(c.value for c in ast.walk(pair.elts[1]) if isinstance(c, ast.Constant)
                             and c.value != 'process_reason')
    return {reason for reason in found if isinstance(reason, str)}


def reasons_in(trace):
    return {row['reason_code'] for scene in trace.values() for row in scene['rows']}


def rejections_in(trace):
    return {row['rejection'] for scene in trace.values() for row in scene['rows'] if 'rejection' in row}


def recorded(*names):
    """The named scenarios, leaving out any not yet recorded, so a test names what is missing and does not raise."""
    return {name: TRACE_DATA[name] for name in names if name in TRACE_DATA}


def row(action, reason='orphan', path='<TMP>/w', **extra):
    return dict(path=path, action=action, reason_code=reason, **extra)


def scene(**parts):
    return dict(one={**EMPTY, **parts})


def directions(old, new):
    return [change.direction for change in compare(old, new)]


class Trace(unittest.TestCase):
    def test_trace_matches_the_recording(self):
        self.assertEqual(collect(), json.loads(TRACE.read_text(encoding='utf-8')))

    def test_trace_is_host_neutral(self):
        self.assertNotRegex(TRACE.read_text(encoding='utf-8'), r'[A-Za-z]:[\\/]|/mnt/|drover-')

    def test_each_scenario_freezes_rows_errors_journal_and_deletions(self):
        keys = {'rows', 'errors', 'journal', 'deleted'}
        for scenario in TRACE_DATA.values():
            self.assertEqual(set(scenario), keys)

    def test_head_is_not_frozen_on_kept_rows(self):
        for name, scenario in TRACE_DATA.items():
            for frozen in scenario['rows']:
                held = {'head', 'branch', 'side_lock'} & set(frozen)
                self.assertEqual(held, set() if frozen['action'] not in ('candidate', 'removed')
                                 else {'head', 'branch', 'side_lock'}, (name, frozen))

    def test_every_decide_reason_has_a_scenario(self):
        self.assertGreaterEqual(len(reasons_in_decide()), 14)
        self.assertLessEqual(reasons_in_decide(), reasons_in(TRACE_DATA))

    def test_a_worktree_gone_dirty_and_a_registration_change_both_stay_recorded(self):
        for name in ('dirty', 'registration', 'unsettled', 'head', 'tag'):
            self.assertIn('revalidation_changed', reasons_in({name: TRACE_DATA[f'revalidation_{name}.applied']}))

    def test_a_scene_that_guards_a_removal_deletes_nothing_today(self):
        for name in ('revalidation_registration.applied', 'revalidation_head.applied', 'revalidation_tag.applied',
                     'revalidation_busy.applied', 'rescue_ref_fails.applied', 'approved_dirty.applied', 'lease_expired.applied',
                     'unsettled_partly_picked.applied', 'unsettled_clean_merge.applied', 'repo_cannot_be_listed.applied',
                     'quarantine_touched.elapsed', 'quarantine_unlisted_touched.elapsed'):
            self.assertEqual(TRACE_DATA[name]['deleted'], [], name)

    def test_a_repo_that_cannot_be_listed_is_not_judged_at_all(self):
        for label in ('dry', 'applied'):
            paths = [row['path'] for row in TRACE_DATA[f'repo_cannot_be_listed.{label}']['rows']]
            self.assertFalse([path for path in paths if 'alpha' in path], paths)

    def test_a_settled_neighbour_stays_out_of_an_approval(self):
        self.assertFalse([row for row in TRACE_DATA['approved_removal.applied']['rows'] if 'neighbour' in row['path']])

    def test_nothing_is_deleted_by_a_dry_scenario(self):
        for name, scenario in TRACE_DATA.items():
            if name.endswith('.dry'):
                self.assertEqual((scenario['deleted'], scenario['journal']), ([], []), name)

    def test_every_output_evidence_rejection_has_a_scenario(self):
        self.assertLessEqual({'main_checkout', 'not_directory', 'unsafe_path', 'owner_side', 'physical_drive',
                              'process using worktree', 'windows process using worktree', 'idle'}, rejections_in(TRACE_DATA))

    def test_every_rejection_keeps_what_it_rejects(self):
        kept = recorded('output_rejected.applied', 'output_kept.applied')
        self.assertEqual(len(kept), 2)
        self.assertLessEqual({'not_directory', 'unsafe_path', 'owner_side', 'physical_drive', 'process using worktree', 'idle'},
                             rejections_in(kept))
        self.assertEqual([scene['deleted'] for scene in kept.values()], [[], []])

    def test_idle_waiver_is_pinned_both_ways(self):
        """A drive at hold or critical waives it; a lesser level, or a budget at any level, never does."""
        waived = recorded('output_idle_hold.applied', 'output_idle_critical.applied')
        self.assertEqual(len(waived), 2)
        self.assertEqual(rejections_in(waived), {'main_checkout'})
        self.assertTrue(all(scene['deleted'] for scene in waived.values()))
        names = 'output_idle.applied', 'output_idle_warn.applied', 'output_idle_queue.applied', 'budget_idle_at_critical.applied'
        kept = recorded(*names)
        self.assertEqual(sorted(kept), sorted(names))
        self.assertTrue(all('idle' in rejections_in({name: scene}) and not scene['deleted'] for name, scene in kept.items()))

    def test_every_stage_has_a_scenario_and_every_deadline_row_appears(self):
        self.assertLessEqual({'settled_deadline', 'output_deadline', 'budget_deadline'}, reasons_in(TRACE_DATA))
        self.assertLessEqual({'stale_temp', 'temp_retained', 'temp_root_elapsed', 'temp_root_partial', 'declared_cache',
                              'folder_cache', 'declared_disposable', 'drive_unproven', 'output_evidence_unavailable',
                              'repository_unavailable', 'budget_not_needed', 'budget_shortfall', 'budget_used_unknown',
                              'budget_processes_unknown', 'worktree_inventory', 'untracked_or_ignored', 'budget_usage',
                              'merged_live_task', 'unlisted_folder', 'declared_report'}, reasons_in(TRACE_DATA))

    def test_a_path_tracked_mid_purge_keeps_its_whole_folder(self):
        self.assertEqual(TRACE_DATA['budget_becomes_tracked.applied']['deleted'], [])

    def test_a_budget_that_cannot_be_measured_removes_nothing(self):
        names = 'budget_used_unknown.applied', 'budget_processes_unknown.applied', 'report_rows.applied'
        self.assertEqual(sorted(recorded(*names)), sorted(names))
        for name in names:
            self.assertEqual(TRACE_DATA[name]['deleted'], [], name)
        usage = [row['path'] for row in TRACE_DATA['report_rows.dry']['rows'] if row['reason_code'] == 'budget_usage']
        self.assertTrue([path for path in usage if path.endswith('/w')])
        self.assertFalse([path for path in usage if path.endswith('/partial')])

    def test_every_holding_reason_has_a_scenario(self):
        self.assertLessEqual(HOLDING, reasons_in(TRACE_DATA))

    def test_every_kept_reason_recorded_is_classified(self):
        kept = {row['reason_code'] for scene in TRACE_DATA.values() for row in scene['rows'] if row['action'] in KEPT}
        self.assertEqual(kept - HOLDING - ON_THE_CLOCK, set())
        self.assertEqual(HOLDING & ON_THE_CLOCK, set())

    def test_a_registration_alone_leaves_the_folder_as_it_was(self):
        """Touch the folder and the fingerprint refuses first, so `quarantine_unlisted_registered` would prove nothing of the listing."""
        with Scene() as scene:
            path = scene.folder('stray')
            before = identity(path)
            scene.registered_only(path, 'stray')
            self.assertEqual(identity(path), before)

    def test_a_temp_root_turned_link_mid_stage_keeps_what_is_under_it(self):
        self.assertEqual(TRACE_DATA['temp_root_becomes_a_link.applied']['deleted'], [])

    def test_a_stray_is_kept_when_it_goes_busy_or_is_registered_mid_stage(self):
        names = 'quarantine_unlisted_goes_busy.elapsed', 'quarantine_unlisted_registered.elapsed'
        self.assertEqual(sorted(recorded(*names)), sorted(names))
        for name in names:
            self.assertEqual(TRACE_DATA[name]['deleted'], [], name)

    def test_a_scene_that_guards_a_task5_mutant_deletes_nothing_today(self):
        names = ('stale_task_snapshot.applied', 'future_task_snapshot.applied', 'ignored_holds_a_repository.applied',
                 'lease_for_another_repo.applied', 'quarantine_unlisted_other_side.elapsed', 'removal_leaves_the_drive.applied',
                 'removal_becomes_a_link.applied')
        self.assertEqual(sorted(recorded(*names)), sorted(names))
        for name in names:
            self.assertEqual(TRACE_DATA[name]['deleted'], [], name)

    def keeps(self, name, path, reason):
        """`name` is recorded, keeps `path` for `reason` and deletes nothing there."""
        self.assertIn(name, TRACE_DATA)
        rows = [(row['action'], row['reason_code']) for row in TRACE_DATA[name]['rows'] if row.get('path') == path]
        self.assertEqual(rows, [('keep', reason)], name)
        self.assertNotIn(path, TRACE_DATA[name]['deleted'], name)

    def test_a_stray_with_an_edited_tracked_file_stays(self):
        for label in ('dry', 'applied'):
            self.keeps(f'unlisted_edited.{label}', '<TMP>/worktrees/alpha.worktrees/stray', 'unavailable')

    def test_a_tag_written_after_inspect_read_it_stops_the_removal(self):
        self.keeps('identity_changed_by_tag.applied', '<TMP>/worktrees/alpha.worktrees/w', 'identity_changed')

    def test_a_task_row_naming_a_path_keeps_the_worktree_and_what_is_under_it(self):
        for label, unit in (('dry', 'bin'), ('applied', 'bin/x')):     # a dry status collapses the folder
            self.keeps(f'busy_task_at_path.{label}', '<TMP>/worktrees/alpha.worktrees/w', 'busy')
            self.keeps(f'output_task_at_path.{label}', f'<TMP>/worktrees/alpha.worktrees/w/{unit}', 'live_owner')

    def test_a_temp_entry_holding_a_protected_root_is_protected(self):
        for label in ('dry', 'applied'):
            for path in ('<TMP>/worktrees', '<TMP>/repos'):
                self.keeps(f'temp_holds_a_worktree_root.{label}', path, 'protected')

    def test_a_cache_leaf_on_another_drive_is_kept_at_the_listing(self):
        for label in ('dry', 'applied'):
            self.keeps(f'cache_kept.{label}', '<TMP>/holds_other_drive/old', 'protected')


def removal(path='<TMP>/w', **row_extra):
    """The recorded shape of one removal: row, journal and deletion, each a separate witness."""
    return dict(rows=[row('removed', 'settled', path, **row_extra)], deleted=[path],
                journal=[dict(phase='planned', path=path), dict(phase='completed', path=path)])


class Compare(unittest.TestCase):
    def test_keep_to_removed_is_towards_deletion(self):
        self.assertEqual(directions(scene(rows=[row('keep')]), scene(rows=[row('removed')])), ['towards_deletion'])

    def test_candidate_to_keep_is_towards_keep(self):
        self.assertEqual(directions(scene(rows=[row('candidate', 'settled')]), scene(rows=[row('keep', 'settled')])),
                         ['towards_keep'])

    def test_new_deleted_path_is_towards_deletion_whatever_the_row_says(self):
        old, new = scene(rows=[row('keep')]), scene(rows=[row('keep')], deleted=['<TMP>/w'])
        self.assertEqual(directions(old, new), ['towards_deletion'])

    def test_added_report_row_is_report_added(self):
        self.assertEqual(directions(scene(), scene(rows=[row('report', 'unlocked_worktree')])), ['report_added'])

    def test_added_candidate_row_is_towards_deletion(self):
        self.assertEqual(directions(scene(), scene(rows=[row('candidate', 'settled')])), ['towards_deletion'])

    def test_changed_keep_reason_is_towards_keep(self):
        old, new = scene(rows=[row('keep', 'orphan')]), scene(rows=[row('keep', 'identity_changed')])
        self.assertEqual(directions(old, new), ['towards_keep'])

    def test_task_10s_change_of_reason_is_towards_keep(self):
        old, new = scene(rows=[row('keep', 'revalidation_changed')]), scene(rows=[row('keep', 'identity_changed')])
        self.assertEqual(directions(old, new), ['towards_keep'])

    def test_a_refused_removal_is_towards_keep_by_every_witness(self):
        changes = compare(scene(**removal()), scene(rows=[row('keep', 'identity_changed')]))
        self.assertEqual({change.direction for change in changes}, {'towards_keep'})
        self.assertEqual(sorted(change.part for change in changes), ['deleted', 'journal', 'journal', 'row'])

    def test_a_keep_row_moved_onto_the_deletion_clock_is_towards_deletion(self):
        for reason in ('unavailable', 'dirty', 'revalidation_changed'):
            for clock in ('orphan', 'unsettled', 'settled', 'missing', 'quarantine_elapsed'):
                self.assertEqual(directions(scene(rows=[row('keep', reason)]), scene(rows=[row('keep', clock)])),
                                 ['towards_deletion'], (reason, clock))

    def test_a_kept_row_between_clocks_is_towards_deletion(self):
        self.assertEqual(directions(scene(rows=[row('keep', 'unsettled')]), scene(rows=[row('keep', 'orphan')])),
                         ['towards_deletion'])

    def test_an_added_keep_row_on_the_clock_is_towards_deletion(self):
        self.assertEqual(directions(scene(), scene(rows=[row('keep', 'orphan')])), ['towards_deletion'])

    def test_a_reason_nobody_has_classified_is_towards_deletion(self):
        self.assertEqual(directions(scene(rows=[row('keep', 'dirty')]), scene(rows=[row('keep', 'brand_new')])),
                         ['towards_deletion'])
        self.assertEqual(directions(scene(), scene(rows=[row('report', 'brand_new')])), ['towards_deletion'])

    def test_a_row_that_drives_a_notice_is_not_merely_a_report(self):
        for reason in ('merged_live_task', 'budget_usage'):
            self.assertEqual(directions(scene(), scene(rows=[row('keep', reason)])), ['towards_deletion'], reason)

    def test_lost_keep_row_is_towards_deletion(self):
        self.assertEqual(directions(scene(rows=[row('keep')]), scene()), ['towards_deletion'])

    def test_lost_candidate_row_is_towards_deletion(self):
        self.assertEqual(directions(scene(rows=[row('candidate', 'settled')]), scene()), ['towards_deletion'])

    def test_changed_reason_for_a_removal_stops_the_slice(self):
        old, new = scene(**removal()), scene(**removal())
        new['one']['rows'] = [row('removed', 'quarantine_elapsed')]
        self.assertEqual(directions(old, new), ['towards_deletion'])

    def test_lost_error_row_is_towards_deletion_and_a_new_one_is_report_added(self):
        error = dict(repo='<TMP>/r', error='fatal')
        self.assertEqual(directions(scene(errors=[error]), scene()), ['towards_deletion'])
        self.assertEqual(directions(scene(), scene(errors=[error])), ['report_added'])

    def test_a_new_completed_journal_record_is_towards_deletion(self):
        record = dict(phase='completed', path='<TMP>/w')
        self.assertEqual(directions(scene(), scene(journal=[record])), ['towards_deletion'])

    def test_a_deletion_that_lost_its_planned_record_is_towards_deletion(self):
        old, new = scene(**removal()), scene(**removal())
        del new['one']['journal'][0]
        self.assertEqual(directions(old, new), ['towards_deletion'])

    def test_a_deletion_with_no_journal_at_all_is_towards_deletion(self):
        old, new = scene(**removal()), scene(**removal())
        new['one']['journal'] = []
        self.assertEqual(directions(old, new), ['towards_deletion', 'towards_deletion'])

    def test_a_lost_removed_row_is_towards_deletion_while_the_path_still_goes(self):
        old, new = scene(**removal()), scene(**removal())
        new['one']['rows'] = []
        self.assertEqual(directions(old, new), ['towards_deletion'])

    def test_a_partial_shrink_spares_nothing_of_one_removal(self):
        """The folder stays, the registration still goes: nothing was spared, whatever the row and journal say."""
        old = removal()
        old['deleted'] = ['<TMP>/admin', '<TMP>/w']
        changes = compare(scene(**old), scene(deleted=['<TMP>/admin']))
        self.assertEqual({change.direction for change in changes}, {'towards_deletion'})

    def test_a_partial_shrink_spares_nothing_of_a_second_removal(self):
        """A really spared, B unreported and unjournalled while its registration still goes: fail closed on both."""
        first, second = removal('<TMP>/a'), removal('<TMP>/b')
        old = dict(rows=first['rows'] + second['rows'], deleted=['<TMP>/a', '<TMP>/adminb'], journal=first['journal'] + second['journal'])
        changes = compare(scene(**old), scene(rows=[row('keep', 'dirty', '<TMP>/a')], deleted=['<TMP>/adminb']))
        self.assertEqual({change.direction for change in changes}, {'towards_deletion'})

    def test_a_removal_with_no_row_or_journal_left_still_stops_if_its_folder_goes(self):
        """`missing`: the row names a path that never shows in `deleted`, which holds only the admin directory."""
        old = scene(rows=[row('removed', 'missing')], deleted=['<TMP>/admin'],
                    journal=[dict(phase='planned', path='<TMP>/w'), dict(phase='completed', path='<TMP>/w')])
        self.assertEqual(set(directions(old, scene(deleted=['<TMP>/admin']))), {'towards_deletion'})

    def test_lost_planned_or_failed_record_of_a_kept_row_is_towards_deletion(self):
        records = [dict(phase='planned', path='<TMP>/w'), dict(phase='failed', path='<TMP>/w')]
        old, new = scene(rows=[row('keep', 'settled')], journal=records), scene(rows=[row('keep', 'settled')], journal=records[1:])
        self.assertEqual(directions(old, new), ['towards_deletion'])

    def test_a_new_failed_attempt_on_a_path_that_stays_is_report_added(self):
        records = [dict(phase='planned', path='<TMP>/w'), dict(phase='failed', path='<TMP>/w')]
        self.assertEqual(directions(scene(), scene(journal=records)), ['report_added', 'report_added'])

    def test_a_new_planned_record_on_a_path_that_goes_is_towards_deletion(self):
        old, new = scene(**removal()), scene(**removal())
        new['one']['journal'].append(dict(phase='planned', path='<TMP>/w', extra=True))
        self.assertEqual(directions(old, new), ['towards_deletion'])

    def test_a_lost_scenario_is_towards_deletion_whatever_it_held(self):
        self.assertEqual(directions(scene(rows=[row('keep')]), {}), ['towards_deletion'])
        self.assertEqual(directions(scene(**removal()), {}), ['towards_deletion'])

    def test_an_action_nobody_has_classified_is_towards_deletion(self):
        for action in ('executed', 'configured', 'locked', 'reregistered'):
            self.assertEqual(directions(scene(rows=[row('keep', 'dirty')]), scene(rows=[row(action, 'dirty')])),
                             ['towards_deletion'], action)
            self.assertEqual(directions(scene(), scene(rows=[row(action, 'dirty')])), ['towards_deletion'], action)
            self.assertEqual(directions(scene(rows=[row('removed', 'settled')]), scene(rows=[row(action, 'settled')])),
                             ['towards_deletion'], action)

    def test_a_changed_head_on_a_candidate_or_removal_is_towards_deletion(self):
        old, new = scene(rows=[row('candidate', 'settled', head='a')]), scene(rows=[row('candidate', 'settled', head='b')])
        self.assertEqual(directions(old, new), ['towards_deletion'])
        self.assertEqual(directions(scene(**removal(head='a')), scene(**removal(head='b'))), ['towards_deletion'])

    def test_a_changed_count_on_a_report_row_is_neutral(self):
        old, new = scene(rows=[row('report', 'settled_deadline', count=2)]), scene(rows=[row('report', 'settled_deadline', count=3)])
        self.assertEqual(directions(old, new), ['neutral'])

    def test_a_report_row_with_no_path_compares(self):
        report = dict(action='report', reason_code='process_out_of_scope', count=1)
        self.assertEqual(directions(scene(), scene(rows=[report])), ['report_added'])

    def test_the_same_trace_has_no_changes(self):
        self.assertEqual(compare(TRACE_DATA, TRACE_DATA), [])


if __name__ == '__main__':
    unittest.main()
