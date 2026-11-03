"""Default-tier replay of the hand-recorded registration trace: nothing real runs here, git is faked at the command."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.registration_trace import CASES, materialize, recorded, rows_of
from drover import git_worktrees, placement_git
from drover.git_worktrees import Listings, RegistrationsUnknown, listing, prove
from drover.placement_paths import cleanup_path

RAW = (Path(__file__).parent / 'registration_trace.json').read_text(encoding='utf-8')
TRACE = json.loads(RAW)
SIDES = ('windows', 'wsl')
ROOT = Path(tempfile.gettempdir()) / 'registration-replay-absent'
ALWAYS_FAIL = {'broken config', 'unknown extension', 'main refs missing'}
UNSEEN = {'broken config', 'unknown extension', 'main refs missing'}    # no admin file shows them: ruling 14's gap
NOT_WHOLE = {'commondir missing', 'garbage HEAD', 'empty gitdir', 'only locked'}    # git exits 0, the proof refuses


def every(code_zero):
    for case in CASES:
        for side in SIDES:
            if (TRACE[case][side]['code'] == 0) is code_zero:
                yield case, side


def git_answers(answer):
    done = subprocess.CompletedProcess([], answer['code'], answer['stdout'], '')
    return patch.object(placement_git, 'subprocess', SimpleNamespace(run=lambda *args, **kwargs: done))


class Replay(unittest.TestCase):
    def test_every_case_in_section_5_3_is_recorded_for_both_git_versions(self):
        self.assertEqual({c: set(TRACE[c]) for c in TRACE}, {c: {'windows', 'wsl'} for c in CASES})

    def test_listing_parses_each_recorded_listing(self):
        for case, side in every(True):
            with self.subTest(case=case, side=side):
                answer = recorded(case, side, ROOT)
                with git_answers(answer):
                    self.assertEqual(listing(ROOT / 'repo', timeout_s=400.0).rows, tuple(answer['rows']))

    def test_listing_raises_for_each_recorded_failure(self):
        for case, side in every(False):
            with self.subTest(case=case, side=side):
                with git_answers(recorded(case, side, ROOT)), self.assertRaises(RegistrationsUnknown):
                    listing(ROOT / 'repo', timeout_s=400.0)

    def test_the_cases_no_admin_file_shows_fail_the_listing_on_both_sides(self):
        for case in ALWAYS_FAIL:
            for side in SIDES:
                self.assertNotEqual(TRACE[case][side]['code'], 0, (case, side))

    def test_recording_holds_no_host_path(self):
        self.assertNotRegex(RAW, r'[A-Za-z]:[\\/]|/mnt/|/home/')


class Rows(unittest.TestCase):
    def test_two_rows_at_one_path_stay_two_rows_in_gits_order(self):
        twins = ('worktree <EXT4>/main\0HEAD aaa\0branch refs/heads/main\0\0'
                 'worktree <EXT4>/wt\0HEAD bbb\0branch refs/heads/wt\0\0'
                 'worktree <EXT4>/wt\0HEAD bbb\0detached\0\0')
        self.assertEqual([(row['worktree'], 'detached' in row) for row in rows_of(twins)],
                         [('<EXT4>/main', False), ('<EXT4>/wt', False), ('<EXT4>/wt', True)])
        with git_answers(dict(code=0, stdout=twins)):
            self.assertEqual(len(listing(ROOT / 'repo', timeout_s=400.0).rows), 3)


class Proof(unittest.TestCase):
    """`prove` over each recorded tree, against a recorded listing: what git said against what the reader reads."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def proved(self, case, side, part, label):
        root = self.root / label
        repo = materialize(case, side, root)
        answer = recorded(case, side, root)
        said = answer['before'] if part == 'before' else answer
        common = repo if case == 'bare repos' else repo / '.git'
        tip = next(row['HEAD'] for row in answer['before']['rows'] if 'HEAD' in row)

        def run(argv, **_):
            output = said['stdout'] if 'list' in argv else f'{common}\n' if '--git-common-dir' in argv else f'{tip}\n'
            return subprocess.CompletedProcess(argv, 0, output, '')
        target = said['rows'][-1]['worktree']
        # On Windows a row spelled under the admin directory has a colon in its path, and `lstat` refuses the name.
        with patch.object(placement_git, 'subprocess', SimpleNamespace(run=run)),                 patch.object(git_worktrees, '_dot_git', lambda path: os.path.lexists(os.path.join(path, '.git'))):
            # Expanded on a drive, a WSL recording's own trees read as drive letters too, so only this case is told.
            told = side if case == 'drive-letter' else None
            return prove(Listings([str(repo)]), str(repo), target, side=told, timeout_s=30.0,
                         pointer=os.path.lexists(os.path.join(target, '.git')))

    def test_prove_refuses_wherever_recorded_git_exited_non_zero(self):
        for number, (case, side) in enumerate(every(False)):
            with self.subTest(case=case, side=side):
                refused = self.proved(case, side, 'before', str(number)).refused
                self.assertEqual(bool(refused), case not in UNSEEN, refused)

    def test_prove_agrees_wherever_the_recorded_row_is_whole(self):
        agreed = set()
        for number, (case, side) in enumerate(every(True)):
            if case not in NOT_WHOLE:
                with self.subTest(case=case, side=side):
                    self.assertEqual(self.proved(case, side, 'after', str(number)).refused, ())
                    agreed.add((case, side))
        # `prunable` is not compared. These rows are prunable only as recorded, where the pointer's tree was absent.
        self.assertLessEqual({('/mnt', 'wsl'), ('/mnt', 'windows'), ('drive-letter', 'wsl'), ('ext4', 'windows')}, agreed)

    def test_prove_refuses_what_git_lists_without_complaint_yet_is_not_whole(self):
        for number, (case, side) in enumerate((c, s) for c in sorted(NOT_WHOLE) for s in SIDES):
            with self.subTest(case=case, side=side):
                self.assertNotEqual(TRACE[case][side]['code'], 128)
                self.assertTrue(self.proved(case, side, 'after', str(number)).refused)


class Placeholders(unittest.TestCase):
    def pointer(self, case, side):
        return TRACE[case][side]['files']['repo/.git/worktrees/wt/gitdir']

    def test_a_pointer_keeps_its_form_on_both_sides(self):
        for side in SIDES:
            self.assertTrue(self.pointer('drive-letter', side).startswith('<DRIVE>:/'), side)
            self.assertTrue(self.pointer('/mnt', side).startswith('<MNT>/'), side)
            self.assertTrue(self.pointer('ext4', side).startswith('<EXT4>/'), side)
            self.assertTrue(self.pointer('relative', side).startswith('../'), side)

    def test_each_side_spells_its_own_tree_in_its_own_form(self):
        self.assertTrue(TRACE['absolute']['windows']['stdout'].startswith('worktree <DRIVE>:/repo\0'))
        self.assertTrue(TRACE['absolute']['wsl']['stdout'].startswith('worktree <EXT4>/repo\0'))

    def test_expansion_leaves_no_placeholder_and_spells_the_row_as_this_host_does(self):
        for case in CASES:
            for side in SIDES:
                answer = recorded(case, side, ROOT)
                self.assertNotIn('<', json.dumps(answer).replace('<UNREADABLE>', '').replace('<VANISHED>', ''), case)
        row = recorded('absolute', 'wsl', ROOT)['rows'][1]
        self.assertEqual(row['worktree'], cleanup_path(str(ROOT / 'wt').replace('\\', '/')))

    def test_without_a_root_the_recording_comes_back_as_stored(self):
        self.assertEqual(recorded('absolute', 'wsl'), TRACE['absolute']['wsl'])


class Materialize(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_files_come_back_as_the_recorded_bytes(self):
        repo = materialize('locked with trailing blank lines', 'wsl', self.root)
        self.assertEqual(repo, self.root / 'repo')
        self.assertEqual((repo / '.git/worktrees/wt/locked').read_bytes(), b'wsl\n\n\n')
        expected = recorded('locked with trailing blank lines', 'wsl', self.root)['files']
        self.assertEqual((self.root / 'wt/.git').read_bytes().decode(), expected['wt/.git'])

    def test_the_tree_before_the_damage_has_what_the_damage_took(self):
        repo = materialize('main HEAD missing', 'windows', self.root, damaged=False)
        self.assertTrue((repo / '.git/HEAD').is_file())

    def test_the_damaged_tree_lacks_what_the_damage_took(self):
        repo = materialize('main HEAD missing', 'windows', self.root)
        self.assertFalse((repo / '.git/HEAD').exists())

    def test_unreadable_is_a_directory_at_that_name_so_the_open_fails(self):
        repo = materialize('locked unreadable', 'windows', self.root)
        with self.assertRaises(OSError):
            (repo / '.git/worktrees/wt/locked').read_bytes()

    def test_a_commondir_that_is_a_directory_fails_the_open_too(self):
        repo = materialize('commondir a directory', 'wsl', self.root)
        with self.assertRaises(OSError):
            (repo / '.git/worktrees/wt/commondir').read_bytes()

    def test_vanishing_is_absent(self):
        repo = materialize('directory vanishing', 'wsl', self.root)
        self.assertFalse((repo / '.git/worktrees/wt').exists())
        self.assertEqual(recorded('directory vanishing', 'wsl')['files']['repo/.git/worktrees/wt'], '<VANISHED>')
        repo = materialize('directory vanishing', 'windows', self.root / 'before', damaged=False)
        self.assertTrue((repo / '.git/worktrees/wt/gitdir').is_file())

    def test_a_bare_repository_keeps_its_admin_tree_at_the_top(self):
        repo = materialize('bare repos', 'windows', self.root)
        self.assertTrue((repo / 'worktrees/wt/gitdir').is_file())


if __name__ == '__main__':
    unittest.main()
