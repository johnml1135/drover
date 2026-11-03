"""`git_worktrees.prove` and `parse`: real admin files in a temp tree, and git faked at the command and counted."""
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from drover import git_worktrees, process
from drover.git_worktrees import Listings, RegistrationsUnknown, parse, prove
from drover.placement_paths import cleanup_parts

SHA, OTHER = 'a' * 40, 'b' * 40


def files(**read):
    return dict(dict(gitdir=None, HEAD=None, locked=None, commondir=None, unreadable=()), **read)


def lstat_refused(name):
    real = os.lstat

    def lstat(path, *args, **options):
        if os.path.basename(str(path)) == name:
            raise PermissionError(name)
        return real(path, *args, **options)
    return patch.object(git_worktrees.os, 'lstat', lstat)


def as_directory(path):
    path.unlink(missing_ok=True)
    path.mkdir()


class Trees(unittest.TestCase):
    """Admin files on disk as git leaves them, and a fake git that answers from what the listing said."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.listed, self.tips, self.failing, self.calls = {}, {}, set(), []
        context = patch.object(process, 'subprocess', SimpleNamespace(run=self.answer, DEVNULL=subprocess.DEVNULL,
                                                                      TimeoutExpired=subprocess.TimeoutExpired))
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)

    def answer(self, argv, **_):
        repo = argv[argv.index('-C') + 1]
        kind = 'list' if 'list' in argv else 'common' if '--git-common-dir' in argv else 'tip'
        self.calls.append(kind)
        output = dict(list=self.listed.get(repo), common=f'{repo}/.git\n', tip=f'{self.tips.get(argv[-1], SHA)}\n')[kind]
        return subprocess.CompletedProcess(argv, 128 if kind in self.failing else 0, (output or '').encode('utf-8'),
                                           b'fixture stderr')

    def write(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode('utf-8'))

    def repo(self, path):
        self.write(path / '.git/HEAD', 'ref: refs/heads/main\n')
        self.listed[str(path)] = f'worktree {path.as_posix()}\0HEAD {SHA}\0branch refs/heads/main\0\0'
        return str(path)

    def admin(self, repo, name, file=''):
        return Path(repo) / '.git/worktrees' / name / file

    def registered(self, repo, name, pointer, *, head=None):
        """An admin directory the listing need not know."""
        self.write(self.admin(repo, name, 'gitdir'), pointer)
        self.write(self.admin(repo, name, 'commondir'), '../..\n')
        self.write(self.admin(repo, name, 'HEAD'), head or f'ref: refs/heads/{name}\n')

    def link(self, repo, name, *, detached=False, pointer=None):
        tree = Path(repo).parent / 'trees' / name
        self.registered(repo, name, pointer or tree.as_posix() + '/.git\n', head=SHA + '\n' if detached else None)
        self.write(tree / '.git', f'gitdir: {self.admin(repo, name).as_posix()}\n')
        head = 'detached' if detached else f'branch refs/heads/{name}'
        self.listed[repo] += f'worktree {tree.as_posix()}\0HEAD {SHA}\0{head}\0\0'
        return str(tree)

    def scene(self, label='scene'):
        """Repo `one` holds trees a and b; repo `two` holds c."""
        self.one, self.two = self.repo(self.root / label / 'one'), self.repo(self.root / label / 'two')
        self.a, self.b = self.link(self.one, 'a'), self.link(self.one, 'b')
        self.c = self.link(self.two, 'c')
        self.listings = Listings([self.one, self.two])

    def proof(self, target, *, repo=None, pointer=True, timeout_s=30.0, **options):
        repo = repo or (self.two if target == self.c else self.one)
        return prove(self.listings, repo, target, pointer=pointer, timeout_s=timeout_s, **options)

    def whys(self, target, **options):
        return [refusal.why for refusal in self.proof(target, **options).refused]

    def assert_every_removal_refused(self):
        for target in (self.a, self.b, self.c):
            self.assertEqual(set(self.whys(target)), {'unknown'}, target)


class ProofTable(Trees):
    def test_target_row_differs_refuses_this_removal(self):
        damages = dict(
            locked=lambda: self.write(self.admin(self.one, 'a', 'locked'), 'wsl\n'),
            HEAD=lambda: self.write(self.admin(self.one, 'a', 'HEAD'), 'ref: refs/heads/other\n'),
            **{'resolved HEAD': lambda: self.tips.update({'refs/heads/a': OTHER}),
               '.git': lambda: (Path(self.a) / '.git').unlink()})
        for field, damage in damages.items():
            with self.subTest(field=field):
                self.scene(field.replace(' ', '-'))
                damage()
                refusal = self.proof(self.a).refused[0]
                self.assertEqual(refusal, git_worktrees.Refusal(self.one, self.a, 'changed', field))
                self.assertEqual((self.whys(self.b), self.whys(self.c)), ([], []))

    def test_target_pointer_differs_refuses_it_as_changed_and_every_other_as_unknown(self):
        self.scene()
        self.write(self.admin(self.one, 'a', 'gitdir'), '/elsewhere/a/.git\n')
        self.assertEqual(self.proof(self.a).refused[0], git_worktrees.Refusal(self.one, self.a, 'changed', 'pointer'))
        self.assertEqual(set(self.whys(self.b)), {'unknown'})     # ruling 14: any row's pointer, for any removal
        self.assertEqual(set(self.whys(self.c)), {'unknown'})

    def test_dot_git_must_be_as_pointer_says(self):
        self.scene()
        self.assertEqual(self.whys(self.a, pointer=False), ['changed'])
        (Path(self.a) / '.git').unlink()
        self.assertEqual(self.whys(self.a, pointer=False), [])

    def test_a_dot_git_that_cannot_be_looked_at_is_unknown_as_either_answer(self):
        self.scene()
        for pointer in (True, False):
            with self.subTest(pointer=pointer), lstat_refused('.git'):
                self.assertEqual(self.whys(self.a, pointer=pointer), ['unknown'])

    def test_target_file_unreadable_refuses_this_removal_as_unknown(self):
        for name, damage in dict(HEAD_missing=Path.unlink, HEAD_a_directory=as_directory).items():
            with self.subTest(name):
                self.scene(name)
                damage(self.admin(self.one, 'a', 'HEAD'))
                self.assertEqual(self.whys(self.a), ['unknown'])
                self.assertEqual((self.whys(self.b), self.whys(self.c)), ([], []))
        self.scene('pointer')
        as_directory(self.admin(self.one, 'a', 'gitdir'))
        self.assertEqual(set(self.whys(self.a)), {'unknown'})

    def test_any_row_locked_or_commondir_bad_refuses_every_removal(self):
        damages = dict(locked=lambda: as_directory(self.admin(self.two, 'c', 'locked')),
                       commondir_a_directory=lambda: as_directory(self.admin(self.two, 'c', 'commondir')),
                       commondir_empty=lambda: self.write(self.admin(self.two, 'c', 'commondir'), ''),
                       commondir_wrong=lambda: self.write(self.admin(self.two, 'c', 'commondir'), '../../../else/.git\n'),
                       commondir_missing=lambda: self.admin(self.two, 'c', 'commondir').unlink())
        for name, damage in damages.items():
            with self.subTest(name):
                self.scene(name)
                damage()
                self.assert_every_removal_refused()

    def test_main_head_unlike_main_row_refuses_every_removal(self):
        damages = dict(other=lambda head: self.write(head, 'ref: refs/heads/other\n'),
                       detached=lambda head: self.write(head, SHA + '\n'),
                       garbage=lambda head: self.write(head, 'not a head\n'), missing=Path.unlink)
        for name, damage in damages.items():
            with self.subTest(name):
                self.scene(name)
                damage(Path(self.two) / '.git/HEAD')
                self.assert_every_removal_refused()

    def test_reftable_directory_refuses_every_removal(self):
        self.scene()
        (Path(self.two) / '.git/reftable').mkdir()
        self.assert_every_removal_refused()

    def test_a_reftable_look_that_fails_refuses_every_removal(self):
        self.scene()
        with lstat_refused('reftable'):
            self.assert_every_removal_refused()

    def test_read_past_timeout_refuses_every_removal(self):
        self.scene()
        self.listed[self.one] = self.listed[self.one].replace('branch refs/heads/a', 'detached')
        self.write(self.admin(self.one, 'a', 'HEAD'), SHA + '\n')
        for target in (self.a, self.b, self.c):
            self.assertEqual(set(self.whys(target, timeout_s=0)), {'unknown'})
        self.assertNotIn('tip', self.calls)

    def test_a_read_that_hangs_refuses_and_does_not_hold_the_proof(self):
        self.scene()
        release = threading.Event()
        self.addCleanup(release.set)
        with patch.object(git_worktrees, '_admin_paths', lambda *_: release.wait(30)):
            self.assertEqual(set(self.whys(self.a, timeout_s=0.05)), {'unknown'})

    def test_listed_row_with_no_admin_directory_refuses_every_removal(self):
        self.scene()
        shutil.rmtree(self.admin(self.two, 'c'))
        self.assertEqual(self.whys(self.a), ['unknown'])
        self.assertEqual(self.whys(self.b), ['unknown'])

    def test_a_row_at_the_targets_path_in_another_repo_is_held_to_its_pointer(self):
        self.scene()
        self.listed[self.two] += f'worktree {Path(self.a).as_posix()}\0HEAD {SHA}\0detached\0\0'
        self.assertEqual(self.whys(self.a), ['unknown'])

    def test_pointer_the_listing_lacks_refuses_every_removal(self):
        self.scene()
        self.registered(self.two, 'new', '/elsewhere/new/.git\n')
        self.assert_every_removal_refused()

    def test_own_stage_removed_and_repaired_are_not_differences(self):
        self.scene()
        self.listings.every()
        self.assertEqual(self.whys(self.a), [])
        shutil.rmtree(self.admin(self.one, 'b'))
        self.listings.removed(self.one, self.b)
        self.assertEqual(self.whys(self.a), [])
        self.registered(self.two, 'mended', '/elsewhere/mended/.git\n')
        self.listings.repaired(self.two, '/elsewhere/mended')
        self.assertEqual(self.whys(self.a), [])
        self.assertIn(cleanup_parts('/elsewhere/mended'), self.proof(self.a).reach)

    def test_an_admin_directory_a_removal_left_behind_still_refuses(self):
        self.scene()
        self.listings.every()
        self.listings.removed(self.one, self.b)
        self.assertEqual(self.whys(self.a), ['unknown'])

    def test_a_second_pointer_at_a_repaired_place_still_refuses(self):
        self.scene()
        for name in ('mended', 'twin'):
            self.registered(self.two, name, '/elsewhere/mended/.git\n')
        self.listings.repaired(self.two, '/elsewhere/mended')
        self.assertEqual(self.whys(self.a), ['unknown'])

    def test_empty_pointer_refuses_every_removal_in_its_own_repo_only(self):
        for name, pointer in dict(empty='', missing=None, undecodable=b'\xff\n').items():
            with self.subTest(name):
                self.scene(name)
                self.write(self.admin(self.two, 'ghost', 'locked'), 'wsl\n')
                if pointer is not None:
                    self.write(self.admin(self.two, 'ghost', 'gitdir'), pointer)
                self.assertEqual((self.whys(self.a), self.whys(self.b)), ([], []))
                refusal = self.proof(self.c).refused[0]
                self.assertEqual((refusal.repo, refusal.worktree, refusal.why), (self.two, None, 'unknown'))

    def test_clean_tree_refuses_nothing_and_reach_covers_rows_and_pointers(self):
        self.scene()
        for target in (self.a, self.b, self.c):
            self.assertEqual(self.proof(target).refused, ())
        self.assertEqual(set(self.proof(self.a).reach),
                         {cleanup_parts(path) for path in (self.one, self.two, self.a, self.b, self.c)})

    def test_two_admin_directories_at_one_path_each_answer_their_own_row(self):
        self.scene()
        tree = Path(self.a).as_posix()
        self.registered(self.one, 'twin', tree + '/.git\n', head=SHA + '\n')
        self.listed[self.one] += f'worktree {tree}\0HEAD {SHA}\0detached\0\0'
        self.assertEqual(self.whys(self.a), [])
        self.write(self.admin(self.one, 'twin', 'HEAD'), OTHER + '\n')
        self.assertEqual(self.proof(self.a).refused[0].detail, 'HEAD')

    def test_an_unlisted_target_a_pointer_names_is_refused_in_any_repo(self):
        self.scene()
        self.registered(self.two, 'late', '/elsewhere/late/.git\n')
        self.listings.repaired(self.two, '/elsewhere/late')
        for repo in (self.one, self.two):
            self.assertEqual(self.whys('/elsewhere/late', repo=repo, pointer=False), ['changed'])
        self.assertEqual(self.whys('/elsewhere/other', pointer=False), [])

    def test_a_target_at_the_main_checkout_is_compared_with_its_row(self):
        self.scene()
        self.assertEqual(self.whys(self.one), [])
        self.tips['refs/heads/main'] = OTHER
        self.assertEqual(self.proof(self.one).refused[0].detail, 'resolved HEAD')


class Reading(Trees):
    ADMIN = '/mnt/h/repo/.git/worktrees/wt'

    def test_gitdir_strips_line_ending_and_one_trailing_dot_git(self):
        for raw, spelled in (('/t/wt/.git\n', '/t/wt'), ('/t/wt/.git\r\n', '/t/wt'), ('/t/wt\n', '/t/wt'),
                             ('/t/wt/.git/.git\n', '/t/wt/.git')):
            self.assertEqual(parse(self.ADMIN, files(gitdir=raw), side='wsl').pointer, spelled, raw)

    def test_relative_pointer_resolves_from_admin_directory(self):
        self.assertEqual(parse(self.ADMIN, files(gitdir='../../../../wt/.git\n'), side='wsl').pointer, '/mnt/h/wt')
        windows = parse('H:/repo/.git/worktrees/wt', files(gitdir='../../../../wt/.git\n'), side='windows')
        self.assertEqual(windows.pointer, 'H:/wt')

    def test_relative_pointer_past_the_drive_root_stops_at_the_root_as_git_does(self):      # survey section 4.2
        admin = parse('H:/repos/drover/.git/worktrees/wt', files(gitdir='../../../../../../../home/u/wt/.git\n'),
                      side='windows')
        self.assertEqual((admin.pointer, admin.place), ('H:/home/u/wt', None))

    def test_drive_letter_pointer_puts_its_true_place_in_reach(self):
        wsl = parse(self.ADMIN, files(gitdir='H:/t/wt/.git\n'), side='wsl')
        self.assertEqual((wsl.pointer, wsl.place), (self.ADMIN + '/H:/t/wt', 'H:/t/wt'))
        windows = parse('H:/repo/.git/worktrees/wt', files(gitdir='H:/t/wt/.git\n'), side='windows')
        self.assertEqual((windows.pointer, windows.place), ('H:/t/wt', None))
        one = self.repo(self.root / 'one')
        tree = self.link(one, 'wt', pointer='X:/t/wt/.git\n')
        spelled = self.admin(one, 'wt').as_posix() + '/X:/t/wt'
        self.listed[one] = self.listed[one].replace(Path(tree).as_posix(), spelled)
        self.listings = Listings([one])
        with patch.object(git_worktrees, '_dot_git', lambda _: False):     # Windows refuses a colon in a name
            proof = self.proof(spelled, repo=one, pointer=False, side='wsl')
        self.assertEqual(proof.refused, ())
        self.assertIn(cleanup_parts('/mnt/x/t/wt'), proof.reach)
        self.assertEqual(self.whys('X:/t/wt', repo=one, pointer=False, side='wsl'), ['changed'])

    def test_only_not_found_with_admin_present_means_unlocked(self):
        self.scene()
        read = git_worktrees._read_files(str(self.admin(self.one, 'a')), ('locked',))
        self.assertEqual((read['locked'], read['unreadable']), (None, ()))
        gone = git_worktrees._read_files(str(self.admin(self.one, 'gone')), ('locked',))
        self.assertEqual((gone['locked'], gone['unreadable']), (None, ('locked',)))

    def test_unreadable_locked_never_reads_as_unlocked(self):
        self.scene()
        self.admin(self.one, 'a', 'locked').mkdir()
        self.assertEqual(self.whys(self.a), ['unknown'])

    def test_locked_is_stripped_as_git_strips_it(self):
        self.assertEqual(parse(self.ADMIN, files(locked='wsl\n\n\n')).locked, 'wsl')
        self.assertEqual(parse(self.ADMIN, files(locked='')).locked, '')
        self.assertIsNone(parse(self.ADMIN, files()).locked)

    def test_garbage_head_differs(self):
        self.assertEqual(parse(self.ADMIN, files(HEAD='ref: refs/heads/wt\n')).head, 'refs/heads/wt')
        self.assertEqual(parse(self.ADMIN, files(HEAD=SHA + '\n')).head, SHA)
        self.assertIsNone(parse(self.ADMIN, files()).head)
        for garbage in ('not a head\n', 'refs/heads/wt\n', 'ref: \n', '', SHA[:-1]):
            self.assertEqual(parse(self.ADMIN, files(HEAD=garbage)).head, '', garbage)
        self.scene()
        self.write(self.admin(self.one, 'a', 'HEAD'), 'not a head\n')
        self.assertEqual(self.whys(self.a), ['changed'])

    def test_stray_file_is_ignored(self):
        self.scene()
        self.write(Path(self.two) / '.git/worktrees/stray', 'not an admin directory\n')
        self.assertEqual((self.whys(self.a), self.whys(self.c)), ([], []))

    def test_non_target_rows_compare_pointer_only_not_branch_or_lock_text(self):
        self.scene()
        self.write(self.admin(self.one, 'b', 'HEAD'), 'ref: refs/heads/other\n')
        self.write(self.admin(self.one, 'b', 'locked'), 'wsl\n')
        self.tips['refs/heads/b'] = OTHER
        self.assertEqual((self.whys(self.a), self.whys(self.c)), ([], []))
        self.assertIn('changed', self.whys(self.b))

    def test_repo_with_no_worktrees_directory_agrees(self):
        self.scene()
        lone = self.repo(self.root / 'lone')
        self.listings = Listings([self.one, lone])
        self.assertEqual((self.whys(self.a), self.whys(lone, repo=lone)), ([], []))

    def test_non_ascii_path_and_crlf_pointer_compare_equal(self):
        self.scene()
        tree = self.link(self.one, 'wörk')
        pointer = self.admin(self.one, 'wörk', 'gitdir')
        pointer.write_bytes(pointer.read_bytes().replace(b'\n', b'\r\n'))
        self.assertEqual((self.whys(tree), self.whys(self.c)), ([], []))

    def test_undecodable_bytes_refuse_without_raising(self):
        for name, place in dict(gitdir=('two', 'c'), locked=('two', 'c'), commondir=('two', 'c'), HEAD=('one', 'a')).items():
            with self.subTest(name):
                self.scene(name)
                self.write(self.admin(getattr(self, place[0]), place[1], name), b'\xff\xfe\n')
                self.assertEqual(set(self.whys(self.a)), {'unknown'})


class Cost(Trees):
    def test_one_prove_reads_every_repo_and_spawns_one_tip_for_a_branch_target(self):
        self.scene()
        self.assertEqual(self.proof(self.a).refused, ())
        self.assertEqual(sorted(self.calls), ['common', 'common', 'list', 'list', 'tip'])
        self.write(self.admin(self.two, 'c', 'commondir'), '')
        self.assertTrue(self.proof(self.a).refused, 'the other repo was read')

    def test_detached_target_spawns_no_tip(self):
        self.scene()
        tree = self.link(self.one, 'still', detached=True)
        self.assertEqual(self.proof(tree).refused, ())
        self.assertNotIn('tip', self.calls)

    def test_a_refused_target_spawns_no_tip(self):
        self.scene()
        self.write(self.admin(self.one, 'a', 'locked'), 'wsl\n')
        self.assertTrue(self.proof(self.a).refused)
        self.assertNotIn('tip', self.calls)

    def test_common_dir_is_one_spawn_per_repo_per_stage(self):
        self.scene()
        for target in (self.a, self.b, self.c):
            self.proof(target)
        self.assertEqual((self.calls.count('common'), self.calls.count('list')), (2, 2))

    def test_failed_listing_anywhere_raises_from_prove(self):
        self.scene()
        self.failing.add('list')
        with self.assertRaisesRegex(RegistrationsUnknown, 'fixture stderr'):
            self.proof(self.a)
        self.assertNotIn('common', self.calls)

    def test_tip_failure_raises_registrations_unknown_and_keeps_the_row(self):
        self.scene()
        self.failing.add('tip')
        with self.assertRaisesRegex(RegistrationsUnknown, 'fixture stderr'):
            self.proof(self.a)
        self.assertIsNotNone(self.listings.of(self.one).row(self.a))


if __name__ == '__main__':
    unittest.main()
