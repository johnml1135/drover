"""`git_worktrees.listing` and `Listings`: git is faked at the command, so these count real spawns."""
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from drover import git_worktrees, placement_git, process
from drover.git_worktrees import Listing, Listings, RegistrationsUnknown, listing
from drover.placement_paths import cleanup_parts, cleanup_path, native
from tests.fakeci import FakeCommands, process_options

LIST = ('worktree', 'list', '--porcelain', '-z')
COMMON = ('rev-parse', '--path-format=absolute', '--git-common-dir')
MAIN, TREE = '/repo', '/trees/one'
PORCELAIN = (f'worktree {MAIN}\0HEAD aaa\0branch refs/heads/main\0\0'
             f'worktree {TREE}\0HEAD bbb\0detached\0locked wsl\0\0')


class FakedGit(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.fake = FakeCommands(temporary.name)
        context = self.fake.patch(process)
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)

    def expect(self, repo=MAIN, args=LIST, timeout=400.0, output=PORCELAIN, **answer):
        self.fake.expect(placement_git.git_argv(repo, args), output=output, **process_options(timeout), **answer)


class ListingOfOneRepo(FakedGit):
    def test_listing_timeout_is_400_seconds(self):
        self.assertEqual(git_worktrees.LISTING_TIMEOUT_S, 400.0)
        self.expect()
        listing(MAIN, timeout_s=git_worktrees.LISTING_TIMEOUT_S)

    def test_failure_raises_registrations_unknown_with_todays_message(self):
        self.expect(returncode=128, stderr_output='fixture stderr\n')
        with self.assertRaises(RegistrationsUnknown) as caught:
            listing(MAIN, timeout_s=400.0)
        self.assertIsInstance(caught.exception, OSError)
        self.assertEqual(str(caught.exception), 'fixture stderr')

    def test_failure_with_nothing_on_stderr_keeps_todays_words(self):
        self.expect(returncode=1)
        with self.assertRaisesRegex(RegistrationsUnknown, '^git command failed$'):
            listing(MAIN, timeout_s=400.0)

    def test_timeout_is_a_failure_of_the_whole_repo(self):
        self.expect(error=subprocess.TimeoutExpired(['git'], 400.0))
        with self.assertRaises(RegistrationsUnknown) as caught:
            listing(MAIN, timeout_s=400.0)
        argv = placement_git.git_argv(MAIN, LIST)      # git's own TimeoutExpired, as the elapsed check raised it
        self.assertEqual(str(caught.exception), str(subprocess.TimeoutExpired(argv, 400.0)))

    def test_a_refused_spawn_is_a_failure_of_the_whole_repo(self):
        self.expect(error=PermissionError('fixture refused'))
        with self.assertRaisesRegex(RegistrationsUnknown, 'fixture refused'):
            listing(MAIN, timeout_s=400.0)

    def test_main_checkout_row_comes_first(self):
        self.expect()
        rows = listing(MAIN, timeout_s=400.0).rows
        self.assertEqual([row['worktree'] for row in rows], [cleanup_path(MAIN), cleanup_path(TREE)])

    def test_rows_keep_every_key_git_wrote(self):
        self.expect()
        rows = listing(MAIN, timeout_s=400.0).rows
        self.assertEqual(rows[0], dict(worktree=cleanup_path(MAIN), HEAD='aaa', branch='refs/heads/main'))
        self.assertEqual(rows[1], dict(worktree=cleanup_path(TREE), HEAD='bbb', detached='', locked='wsl'))

    def test_row_finds_a_path_under_any_spelling_and_none_for_a_stranger(self):
        self.expect()
        found = listing(MAIN, timeout_s=400.0)
        self.assertIs(found.row(TREE + '/'), found.rows[1])
        self.assertIsNone(found.row('/trees/two'))

    def test_row_at_a_path_two_admin_directories_share_is_the_first_and_both_stay(self):
        self.expect(output=f'worktree {TREE}\0HEAD aaa\0branch refs/heads/a\0\0worktree {TREE}\0HEAD bbb\0detached\0\0')
        found = listing(MAIN, timeout_s=400.0)
        self.assertEqual(len(found.rows), 2)
        self.assertIs(found.row(TREE), found.rows[0])


class OneStage(FakedGit):
    def test_constructor_lists_nothing(self):
        Listings([MAIN, '/other'])
        self.assertEqual(self.fake.calls, [])

    def test_each_repo_is_listed_once_on_first_use(self):
        self.expect()
        listings = Listings([MAIN])
        for _ in range(3):
            found = listings.of(MAIN)
        self.assertEqual(len(self.fake.calls), 1)
        self.assertIsInstance(found, Listing)
        self.assertEqual(found.repo, MAIN)

    def test_failure_is_kept_and_reraised_without_listing_again(self):
        self.expect(returncode=128, stderr_output='fixture stderr')
        listings = Listings([MAIN])
        with self.assertRaises(RegistrationsUnknown) as first:
            listings.of(MAIN)
        with self.assertRaises(RegistrationsUnknown) as second:
            listings.of(MAIN)
        with self.assertRaises(RegistrationsUnknown) as third:
            listings.every()
        self.assertEqual(len(self.fake.calls), 1)
        self.assertIs(second.exception, first.exception)
        self.assertIs(third.exception, first.exception)

    def test_every_lists_the_rest_then_raises_if_any_failed(self):
        self.expect('/one', returncode=128, stderr_output='first failed')
        self.expect('/two')
        self.expect('/three')
        listings = Listings(['/one', '/two', '/three'])
        with self.assertRaisesRegex(RegistrationsUnknown, 'first failed'):
            listings.every()
        self.assertEqual(len(self.fake.calls), 3)
        self.assertEqual(listings.of('/three').repo, '/three')
        self.assertEqual(len(self.fake.calls), 3)

    def test_every_gives_each_repo_in_order(self):
        self.expect('/one')
        self.expect('/two')
        self.assertEqual([found.repo for found in Listings(['/one', '/two']).every()], ['/one', '/two'])

    def test_reach_is_every_row_of_every_repo_and_a_failed_repo_raises(self):
        self.expect('/one')
        self.assertEqual(Listings(['/one']).reach(), (cleanup_parts(MAIN), cleanup_parts(TREE)))
        self.expect('/two')
        self.expect('/three', returncode=128, stderr_output='failed')
        with self.assertRaisesRegex(RegistrationsUnknown, 'failed'):
            Listings(['/two', '/three']).reach()

    def test_timeout_callable_is_called_at_each_listing(self):
        self.expect('/one', timeout=400.0)
        self.expect('/two', timeout=7.0)
        values = iter((400.0, 7.0))
        listings = Listings(['/one', '/two'], timeout_s=lambda: next(values))
        listings.of('/one')
        listings.of('/two')
        listings.of('/two')

    def test_removed_drops_the_row_and_repaired_expects_the_pointer(self):
        self.expect()
        listings = Listings([MAIN])
        listings.removed(MAIN, TREE + '/')
        self.assertIsNotNone(listings.of(MAIN).row(TREE), 'nothing listed yet, so nothing was dropped')
        listings.removed(MAIN, TREE + '/')
        self.assertIsNone(listings.of(MAIN).row(TREE))
        self.assertIsNotNone(listings.of(MAIN).row(MAIN))
        listings.repaired(MAIN, '/trees/mended/')
        self.assertIsNone(listings.of(MAIN).row('/trees/mended'), 'git did not list it, so no row is added')
        self.assertEqual(listings.repaired_pointers(MAIN), {cleanup_parts('/trees/mended')})
        self.assertEqual(listings.repaired_pointers('/other'), frozenset())
        self.assertEqual(len(self.fake.calls), 1)

    def test_removed_takes_one_row_of_two_at_a_path_as_git_does(self):
        self.expect(output=f'worktree {TREE}\0HEAD aaa\0branch refs/heads/a\0\0worktree {TREE}\0HEAD bbb\0detached\0\0')
        listings = Listings([MAIN])
        listings.of(MAIN)
        listings.removed(MAIN, TREE)
        self.assertEqual([row['HEAD'] for row in listings.of(MAIN).rows], ['bbb'])

    def test_removed_leaves_a_kept_failure_in_place(self):
        self.expect(returncode=128, stderr_output='fixture stderr')
        listings = Listings([MAIN])
        with self.assertRaises(RegistrationsUnknown):
            listings.of(MAIN)
        listings.removed(MAIN, TREE)
        with self.assertRaises(RegistrationsUnknown):
            listings.of(MAIN)
        self.assertEqual(len(self.fake.calls), 1)

    def test_two_spellings_of_one_repo_list_once_and_share_removed(self):
        self.expect()
        listings = Listings([MAIN, MAIN + '/'])
        listings.of(MAIN)
        listings.of(MAIN + '/')
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(len(listings.every()), 1)
        listings.removed(MAIN + '/', TREE)
        self.assertIsNone(listings.of(MAIN).row(TREE))

    def test_a_listing_goes_on_the_side_the_stage_asked_for(self):
        self.fake.expect(placement_git.git_argv(r'H:\repo', LIST, side='wsl'), output=PORCELAIN,
                         **process_options(400.0))
        Listings([r'H:\repo'], side='wsl').of(r'H:\repo')

    def test_common_dir_asks_git_once_per_repo_on_the_evidence_timeout(self):
        self.expect(args=COMMON, timeout=60, output='/repo/.git\n')
        listings = Listings([MAIN])
        self.assertEqual(listings.common_dir(MAIN), native('/repo/.git'))
        self.assertEqual(listings.common_dir(MAIN + '/'), native('/repo/.git'))
        self.assertEqual(len(self.fake.calls), 1)

    def test_common_dir_failure_is_kept_and_leaves_the_listing_alone(self):
        self.expect(args=COMMON, timeout=60, returncode=128, stderr_output='fixture stderr')
        listings = Listings([MAIN])
        for _ in range(2):
            with self.assertRaisesRegex(RegistrationsUnknown, 'fixture stderr'):
                listings.common_dir(MAIN)
        self.assertEqual(len(self.fake.calls), 1)
        self.expect()
        self.assertEqual(len(listings.of(MAIN).rows), 2)


class Spellings(unittest.TestCase):
    def test_a_drive_spelling_case_and_mount_of_one_repo_are_one_repo(self):
        spelled = [r'H:\repos\One', 'h:/repos/ONE/', '/mnt/h/repos/one']
        calls = []

        def answer(repo, **_):
            calls.append(repo)
            return Listing(repo, (dict(worktree='/mnt/h/trees/one'),))
        with patch.object(git_worktrees, 'listing', answer):
            listings = Listings(spelled)
            for repo in spelled:
                listings.of(repo)
            listings.removed(spelled[2], 'H:/TREES/ONE')
            self.assertEqual(listings.of(spelled[0]).rows, ())
        self.assertEqual(calls, spelled[:1])

    def test_a_repo_the_stage_was_not_given_is_listed_and_counts_in_every(self):
        with patch.object(git_worktrees, 'listing', lambda repo, **_: Listing(repo, ())):
            listings = Listings(['/one'])
            listings.of('/two')
            self.assertEqual([found.repo for found in listings.every()], ['/one', '/two'])


if __name__ == '__main__':
    unittest.main()
