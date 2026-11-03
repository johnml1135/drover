"""A slow folder must not void a repo's cleanup report: a git timeout is one folder's unproven row, not the repo's."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from drover import disk_registrations, disk_worktrees, git_client, placement
from tests.disk_fixture import listing_answers, stage_request

TIMEOUT = ValueError('git rev-parse failed: timeout')      # what a git timeout gives the disk callers, through `_out`
SPAWN_TIMEOUT = subprocess.TimeoutExpired(['git'], 60)    # the spawn's own, below git_client
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


class UnknownCandidate(unittest.TestCase):
    """An unknown on the preferred branch name is not "no such branch": the next spelling must not be tried."""
    def git(self, repo, *args, timeout):
        self.asked.append(args[-1])
        if args[-1] == 'refs/heads/hx/T1':
            raise SPAWN_TIMEOUT
        return subprocess.CompletedProcess(args, 0, 'c' * 40 + '\n', '')

    def test_a_timed_out_preferred_name_raises_and_the_row_is_unproven(self):
        from tests.fakeci import git_answers
        self.asked = []
        with git_answers(self.git), self.assertRaisesRegex(ValueError, '^git rev-parse failed: timeout$'):
            disk_registrations.candidate('/repo', 'T1', 'hx/T1')
        self.assertEqual(self.asked, ['refs/heads/hx/T1'])
        with git_answers(self.git):
            [row] = disk_registrations.report(request(), MACHINE, [], {'/nowhere/hx/T1': (Path('/nowhere/admin-T1'), 'hx/T1')}, None)
        self.assertEqual((row['action'], row['reason_code'], row['branch']), ('keep', 'lost_registration_unproven', None))
        self.assertEqual(row['detail'], 'could not be matched: git rev-parse failed: timeout')


class ReregisterFailure(unittest.TestCase):
    """reregister's own handler: remove the temporary worktree, and always journal `failed`."""
    def run_reregister(self, removal):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__('shutil').rmtree, tmp, True)
        orphan = tmp / 'orphan'
        orphan.mkdir()
        (orphan / '.git').write_text('gitdir: x', encoding='utf-8')
        records = []
        store = type('Store', (), dict(append=lambda self, r: records.append(r)))()

        self.added = []

        def add(repo, place, *args, **kwargs):
            self.added.append(kwargs)
            Path(place).mkdir(parents=True)
            raise kwargs['error'](git_client.GitFailed(('worktree', 'add'), 'timeout', None, '', ''))     # add_worktree's error=
        with patch.object(placement, 'add_worktree', side_effect=add),                 patch.object(git_client, 'mutate', return_value=removal) as git,                 patch.object(placement, 'cleanup_side', return_value='wsl'):
            with self.assertRaisesRegex(ValueError, '^git worktree failed: timeout$'):
                disk_registrations.reregister(dict(now=0, state_dir=str(tmp / 'state')), MACHINE, orphan, None, 'b', store)
        return records, git

    def test_failure_removes_the_temporary_worktree_and_journals_failed(self):
        records, git = self.run_reregister(git_client.GitDone(True, '', '', 0, None))
        self.assertEqual(git.call_count, 1)
        self.assertEqual([r['phase'] for r in records], ['planned', 'failed'])

    def test_the_repair_adds_its_worktree_within_a_stage_change_limit(self):
        self.run_reregister(git_client.GitDone(True, '', '', 0, None))
        self.assertEqual([kw.get('timeout_s') for kw in self.added], [git_client.NORMAL])     # section 6.1: 60 s

    def test_second_timeout_in_the_removal_still_journals_failed(self):
        records, git = self.run_reregister(git_client.GitDone(False, '', '', None, 'timeout'))     # mutate raises nothing
        self.assertEqual(git.call_count, 1)
        self.assertEqual([r['phase'] for r in records], ['planned', 'failed'])


class UnknownLostFolders(unittest.TestCase):
    def test_lost_timeout_records_the_error_and_judges_no_orphans(self):
        listed = [dict(worktree='/repo')]
        with listing_answers(listed), \
                patch.object(disk_registrations, 'protect', return_value=[]), \
                patch.object(disk_registrations, 'lost', side_effect=TIMEOUT), \
                patch.object(disk_worktrees, 'unlisted', return_value=[]) as unlisted, \
                patch('drover.disk_cleanup.evaluate', return_value=dict(items=[], errors=[], reclaimed_bytes=0)):
            out = disk_worktrees.settled(stage_request(dict(request(), repos=[MACHINE])), runtime=None)
        self.assertEqual([e['repo'] for e in out['errors']], ['/repo'])
        unlisted.assert_not_called()


if __name__ == '__main__':
    unittest.main()
