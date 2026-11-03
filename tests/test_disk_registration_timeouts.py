"""A slow folder must not void a repo's cleanup report: a git timeout is one folder's unproven row, not the repo's."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from drover import disk_registrations, disk_worktrees, git_client, git_worktrees, placement
from drover.disk_stage import StageClock
from tests.disk_fixture import answer, listing_answers, stage_run, staged

TIMEOUT = ValueError('git rev-parse failed: timeout')      # what a git timeout gives the disk callers, through `_out`
SPAWN_TIMEOUT = subprocess.TimeoutExpired(['git'], 60)    # the spawn's own, below git_client
MACHINE = dict(name='alpha', repo='/repo')
CLOCK = StageClock(1800)


def at(*times):
    """A clock reading 0 at the stage's start, then each of `times` in turn, holding the last."""
    readings = [0.0, *times]
    return lambda: readings.pop(0) if len(readings) > 1 else readings[0]


def request(apply=False):
    return dict(apply=apply, now=0, state_dir='state')


def found(*names):
    return {f'/nowhere/{n}': (Path(f'/nowhere/admin-{n}'), n) for n in names}


class FolderTimeouts(unittest.TestCase):
    def test_judge_timeout_keeps_that_folder_and_judges_the_next(self):
        outcomes = [TIMEOUT, ('repair', 'b2', 'ok', None)]
        with patch.object(disk_registrations, 'judge', side_effect=outcomes):
            rows = disk_registrations.report(request(), MACHINE, [], found('one', 'two'), None, evidence=None, clock=CLOCK)
        self.assertEqual([r['reason_code'] for r in rows], ['lost_registration_unproven', 'lost_registration'])
        self.assertEqual([r['action'] for r in rows], ['keep', 'report'])

    def test_judge_timeout_leaves_no_repo_error(self):
        run = stage_run(dict(request(), repos=[MACHINE]))
        with patch.object(disk_registrations, 'protect', return_value=[]), \
                patch.object(disk_registrations, 'lost', return_value=found('one', 'two')), \
                patch.object(disk_registrations, 'judge', side_effect=[TIMEOUT, ('unproven', None, 'no', None)]):
            got = disk_worktrees.registrations(request(), MACHINE, [], None, run)
        out = answer(run)
        self.assertEqual(out['errors'], [])
        self.assertEqual(len(out['items']), 2)
        self.assertEqual(set(got), set(found('one', 'two')))

    def test_reregister_timeout_is_a_failed_row_not_an_exception(self):
        with patch.object(disk_registrations, 'judge', return_value=('repair', 'b', 'ok', None)), \
                patch.object(disk_registrations, 'reregister', side_effect=TIMEOUT):
            rows = disk_registrations.report(request(apply=True), MACHINE, [], found('one'), None, evidence=None, clock=CLOCK)
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
            [row] = disk_registrations.report(request(), MACHINE, [], {'/nowhere/hx/T1': (Path('/nowhere/admin-T1'), 'hx/T1')}, None,
                                              evidence=None, clock=CLOCK)
        self.assertEqual((row['action'], row['reason_code'], row['branch']), ('keep', 'lost_registration_unproven', None))
        self.assertEqual(row['detail'], 'could not be matched: git rev-parse failed: timeout')


class ReregisterFailure(unittest.TestCase):
    """reregister's own handler: remove the temporary worktree, and always journal `failed`."""
    def run_reregister(self, removal, clock=CLOCK):
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
        with patch.object(placement, 'add_worktree', side_effect=add), \
                patch.object(git_client, 'mutate', return_value=removal) as git, \
                patch.object(placement, 'cleanup_side', return_value='wsl'):
            with self.assertRaisesRegex(ValueError, '^git worktree failed: timeout$'):
                disk_registrations.reregister(dict(now=0, state_dir=str(tmp / 'state')), MACHINE, orphan, None, 'b', store,
                                              evidence=None, clock=clock)
        return records, git

    def test_failure_removes_the_temporary_worktree_and_journals_failed(self):
        records, git = self.run_reregister(git_client.GitDone(True, '', '', 0, None))
        self.assertEqual(git.call_count, 1)
        self.assertEqual([r['phase'] for r in records], ['planned', 'failed'])

    def test_the_repair_adds_and_removes_its_worktree_within_the_mutation_cap(self):
        _, git = self.run_reregister(git_client.GitDone(True, '', '', 0, None), clock=StageClock(100))
        self.assertEqual([kw.get('timeout_s') for kw in self.added], [4])     # section 6.1:643: the cap, not 60 s
        self.assertEqual(git.call_args.kwargs['timeout_s'], 4)

    def test_second_timeout_in_the_removal_still_journals_failed(self):
        records, git = self.run_reregister(git_client.GitDone(False, '', '', None, 'timeout'))     # mutate raises nothing
        self.assertEqual(git.call_count, 1)
        self.assertEqual([r['phase'] for r in records], ['planned', 'failed'])


class ReregisterClock(unittest.TestCase):
    """The carry from slice 1: a repair starts only while the stage can still finish it, and its verifying listing,
    inside the planned window, takes the stage's patience rather than the 400 s listing timeout."""
    def orphan(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__('shutil').rmtree, tmp, True)
        orphan = tmp / 'w' / 'orphan'
        orphan.mkdir(parents=True)
        (orphan / '.git').write_text('gitdir: x', encoding='utf-8')
        return tmp, orphan

    def test_past_the_line_the_repair_never_starts(self):
        tmp, orphan = self.orphan()
        records = []
        with patch.object(placement, 'add_worktree') as add, self.assertRaisesRegex(ValueError, 'deadline'):
            disk_registrations.reregister(dict(now=0, state_dir=str(tmp / 'state')), MACHINE, orphan, None, 'b',
                                          SimpleNamespace(append=records.append), evidence=None,
                                          clock=StageClock(100, clock=at(80)))
        self.assertEqual((records, add.called, (orphan / '.git').read_text(encoding='utf-8')), ([], False, 'gitdir: x'))

    def test_the_verifying_listing_takes_patience(self):
        tmp, orphan = self.orphan()
        (tmp / 'admin').mkdir()
        (tmp / 'admin' / 'index').write_text('', encoding='utf-8')
        records, told, asked = [], [], []

        def add(repo, place, *args, **kwargs):
            Path(place).mkdir(parents=True)
            (Path(place) / '.git').write_text('gitdir: new', encoding='utf-8')

        def listing(repo, *, timeout_s):
            asked.append(timeout_s)
            return git_worktrees.Listing(repo, (dict(worktree=str(orphan)),))
        evidence = SimpleNamespace(repaired=lambda repo, path: told.append((repo, path)))
        with patch.object(placement, 'add_worktree', side_effect=add), \
                patch.object(disk_registrations, '_out', return_value=''), \
                patch.object(disk_registrations, 'gitdir_at', return_value=str(tmp / 'admin')), \
                patch.object(git_worktrees, 'listing', side_effect=listing):
            disk_registrations.reregister(dict(now=0, state_dir=str(tmp / 'state')), MACHINE, orphan, None, 'b',
                                          SimpleNamespace(append=records.append), evidence=evidence,
                                          clock=StageClock(1800, clock=at(1400)))
        self.assertEqual(asked, [40])       # patience(400) 40 s before the line, not 400 s
        self.assertEqual(([r['phase'] for r in records], told), (['planned', 'completed'], [('/repo', orphan)]))

    def test_the_repair_steps_take_the_mutation_cap(self):
        tmp, orphan = self.orphan()
        (tmp / 'admin').mkdir()     # no index, so the reset runs
        asked = []

        def add(repo, place, *args, timeout_s, **kwargs):
            asked.append(('add', timeout_s))
            Path(place).mkdir(parents=True)
            (Path(place) / '.git').write_text('gitdir: new', encoding='utf-8')

        def out(where, *args, timeout_s=None, **_):
            asked.append((args[1] if args[0] == 'worktree' else args[0], timeout_s))
            return ''
        with patch.object(placement, 'add_worktree', side_effect=add), \
                patch.object(disk_registrations, '_out', side_effect=out), \
                patch.object(disk_registrations, 'gitdir_at', return_value=str(tmp / 'admin')), \
                patch.object(git_worktrees, 'listing', return_value=git_worktrees.Listing('/repo', (dict(worktree=str(orphan)),))):
            disk_registrations.reregister(dict(now=0, state_dir=str(tmp / 'state')), MACHINE, orphan, None, 'b',
                                          SimpleNamespace(append=lambda record: None),
                                          evidence=SimpleNamespace(repaired=lambda repo, path: None),
                                          clock=StageClock(100, clock=at(10)))
        self.assertEqual(asked, [('add', 4), ('repair', 4), ('reset', 4)])     # section 6.1:643: each mutating step's cap


class UnknownLostFolders(unittest.TestCase):
    def test_lost_timeout_records_the_error_and_judges_no_orphans(self):
        listed = [dict(worktree='/repo')]
        with listing_answers(listed), \
                patch.object(disk_registrations, 'protect', return_value=[]), \
                patch.object(disk_registrations, 'lost', side_effect=TIMEOUT), \
                patch.object(disk_worktrees, 'unlisted', return_value=[]) as unlisted, \
                patch.object(placement, 'cleanup_processes', return_value=SimpleNamespace(busy=lambda path: False)), \
                patch('drover.disk_cleanup.evaluate', return_value=dict(items=[], errors=[], reclaimed_bytes=0)):
            out = staged(disk_worktrees.settled, dict(request(), repos=[MACHINE]), runtime=None)
        self.assertEqual([e['repo'] for e in out['errors']], ['/repo'])
        unlisted.assert_not_called()


if __name__ == '__main__':
    unittest.main()
