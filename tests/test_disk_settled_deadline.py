"""Stage 4 ends itself before the worker timeout: unreached worktrees are named, never removed, never an error."""
import unittest
from unittest.mock import patch

from drover import disk_budget, disk_worktrees
from tests.disk_fixture import listing_answers, stage_request

MACHINE = dict(name='alpha', repo='/repo')
OTHER = dict(name='beta', repo='/other')


class SettledDeadline(unittest.TestCase):
    def run_settled(self, *, step, repos=(MACHINE, OTHER), **extra):
        clock = iter(range(0, 10 ** 6, step))
        listed = [dict(worktree=f'/wt/{i}') for i in range(4)]
        seen = []

        def evaluate(request, runtime):
            seen.append(runtime.evidence.entry['worktree'])
            return dict(items=[dict(path=seen[-1], action='keep', reason_code='fresh')], errors=[], reclaimed_bytes=0)
        request = dict(apply=True, now=0, state_dir='state', repos=list(repos), paths=dict(worker_timeout_s=100), **extra)
        with patch.object(disk_budget, '_clock', lambda: next(clock)), \
                listing_answers(listed), \
                patch.object(disk_worktrees, 'registrations', return_value={}), \
                patch.object(disk_worktrees, 'unlisted', return_value=[]), \
                patch('drover.disk_cleanup.evaluate', side_effect=evaluate):
            return disk_worktrees.settled(stage_request(request)), seen

    def test_late_run_reports_what_it_did_not_reach_as_an_item(self):
        out, seen = self.run_settled(step=20)
        late = [i for i in out['items'] if i.get('reason_code') == 'settled_deadline']
        self.assertEqual(len(late), 1)
        self.assertEqual(out['errors'], [])
        self.assertTrue(0 < len(seen) < 4)
        self.assertEqual(late[0]['worktrees'], [f'/wt/{i}' for i in range(len(seen), 4)])
        self.assertEqual(late[0]['repos'], ['/other'])
        self.assertEqual(late[0]['action'], 'report')

    def test_time_to_spare_reaches_everything(self):
        out, seen = self.run_settled(step=1)
        self.assertEqual(len(seen), 8)
        self.assertFalse([i for i in out['items'] if i.get('reason_code') == 'settled_deadline'])

    def test_approved_removal_is_not_cut_off(self):
        out, seen = self.run_settled(step=1000, repos=(MACHINE,), approved_path='/wt/3')
        self.assertEqual(len(seen), 4)
        self.assertFalse([i for i in out['items'] if i.get('reason_code') == 'settled_deadline'])


if __name__ == '__main__':
    unittest.main()
