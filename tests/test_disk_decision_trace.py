"""The recorded decision trace, and the comparison that stops a slice moving it towards deletion."""
import ast
import inspect
import json
import unittest

from drover.disk_cleanup import decide
from tests.disk_trace import TRACE, collect, compare

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
        for name in ('revalidation_dirty.applied', 'revalidation_registration.applied'):
            self.assertIn('revalidation_changed', reasons_in({name: TRACE_DATA[name]}))

    def test_nothing_is_deleted_by_a_dry_scenario(self):
        for name, scenario in TRACE_DATA.items():
            if name.endswith('.dry'):
                self.assertEqual((scenario['deleted'], scenario['journal']), ([], []), name)


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

    def test_lost_keep_row_is_towards_deletion(self):
        self.assertEqual(directions(scene(rows=[row('keep')]), scene()), ['towards_deletion'])

    def test_changed_reason_for_a_removal_stops_the_slice(self):
        old, new = scene(rows=[row('removed', 'settled')]), scene(rows=[row('removed', 'quarantine_elapsed')])
        self.assertEqual(directions(old, new), ['towards_deletion'])

    def test_lost_error_row_is_towards_deletion_and_a_new_one_is_report_added(self):
        error = dict(repo='<TMP>/r', error='fatal')
        self.assertEqual(directions(scene(errors=[error]), scene()), ['towards_deletion'])
        self.assertEqual(directions(scene(), scene(errors=[error])), ['report_added'])

    def test_a_completed_journal_record_is_towards_deletion(self):
        record = dict(phase='completed', path='<TMP>/w')
        self.assertEqual(directions(scene(), scene(journal=[record])), ['towards_deletion'])
        self.assertEqual(directions(scene(journal=[record]), scene()), ['towards_keep'])

    def test_a_deletion_that_lost_its_planned_record_is_towards_deletion(self):
        planned, completed = dict(phase='planned', path='<TMP>/w'), dict(phase='completed', path='<TMP>/w')
        self.assertEqual(directions(scene(journal=[planned, completed]), scene(journal=[completed])),
                         ['towards_deletion'])

    def test_a_lost_scenario_is_towards_deletion(self):
        self.assertEqual(directions(scene(rows=[row('keep')]), {}), ['towards_deletion'])

    def test_a_changed_head_on_the_same_candidate_is_neutral(self):
        old, new = scene(rows=[row('candidate', 'settled', head='a')]), scene(rows=[row('candidate', 'settled', head='b')])
        self.assertEqual(directions(old, new), ['neutral'])

    def test_the_same_trace_has_no_changes(self):
        self.assertEqual(compare(TRACE_DATA, TRACE_DATA), [])


if __name__ == '__main__':
    unittest.main()
