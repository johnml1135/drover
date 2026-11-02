"""A slow folder must not void a repo's cleanup report: a git timeout is one folder's unproven row, not the repo's."""
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from drover import disk_registrations, disk_worktrees

TIMEOUT = subprocess.TimeoutExpired(['git'], 60)
MACHINE = dict(name='alpha', repo='/repo')


def request(apply=False):
    return dict(apply=apply, now=0, state_dir='state')


def found(*names):
    return {f'/nowhere/{n}': (Path(f'/nowhere/admin-{n}'), n) for n in names}


class FolderTimeouts(unittest.TestCase):
    def test_judge_timeout_keeps_that_folder_and_judges_the_next(self):
        outcomes = [TIMEOUT, ('repair', 'b2', 'ok', None)]
        with patch.object(disk_registrations, 'judge', side_effect=outcomes):
            rows = disk_registrations.report(request(), MACHINE, [], found('one', 'two'), None)
        self.assertEqual([r['reason_code'] for r in rows], ['lost_registration_unproven', 'lost_registration'])
        self.assertEqual([r['action'] for r in rows], ['keep', 'report'])

    def test_judge_timeout_leaves_no_repo_error(self):
        out = dict(items=[], errors=[], reclaimed_bytes=0)
        with patch.object(disk_registrations, 'protect', return_value=[]), \
                patch.object(disk_registrations, 'lost', return_value=found('one', 'two')), \
                patch.object(disk_registrations, 'judge', side_effect=[TIMEOUT, ('unproven', None, 'no', None)]):
            got = disk_worktrees.registrations(request(), MACHINE, [], None, out)
        self.assertEqual(out['errors'], [])
        self.assertEqual(len(out['items']), 2)
        self.assertEqual(set(got), set(found('one', 'two')))

    def test_reregister_timeout_is_a_failed_row_not_an_exception(self):
        with patch.object(disk_registrations, 'judge', return_value=('repair', 'b', 'ok', None)), \
                patch.object(disk_registrations, 'reregister', side_effect=TIMEOUT):
            rows = disk_registrations.report(request(apply=True), MACHINE, [], found('one'), None)
        self.assertEqual((rows[0]['action'], rows[0]['reason_code']), ('keep', 'lost_registration_failed'))


class UnknownLostFolders(unittest.TestCase):
    def test_lost_timeout_records_the_error_and_judges_no_orphans(self):
        listed = [dict(worktree='/repo')]
        with patch.object(disk_worktrees, 'entries', return_value=listed), \
                patch.object(disk_registrations, 'protect', return_value=[]), \
                patch.object(disk_registrations, 'lost', side_effect=TIMEOUT), \
                patch.object(disk_worktrees, 'unlisted', return_value=[]) as unlisted, \
                patch('drover.disk_cleanup.evaluate', return_value=dict(items=[], errors=[], reclaimed_bytes=0)):
            out = disk_worktrees.settled(dict(request(), repos=[MACHINE]), runtime=None)
        self.assertEqual([e['repo'] for e in out['errors']], ['/repo'])
        unlisted.assert_not_called()


if __name__ == '__main__':
    unittest.main()
