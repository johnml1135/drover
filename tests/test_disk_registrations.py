"""A worktree's registration survives the other side's prune, and a lost one is found and re-made without touching files.

Fixture git repositories only. The WSL side cannot be exercised from Windows, so side detection is tested on pointer
text alone and the Linux-path git behaviour stays unverified here.
"""
import shutil
import unittest
from pathlib import Path

from drover import placement, worktree
from drover.disk_registrations import owning_side
from tests.test_disk_worktrees import WorktreeCase
from tests.tiers import integration


class Pointers(unittest.TestCase):
    def test_owning_side_reads_the_pointer_text(self):
        admin = '/mnt/h/repos/x/.git/worktrees/w'
        for pointer, side in (('H:/repos/x.worktrees/w/.git', 'windows'), ('c:\\work\\w\\.git', 'windows'),
                              ('/home/dev/work/x.worktrees/w/.git', 'wsl'),
                              ('/home/dev/work/x.worktrees/w/.git\r\n', 'wsl'),
                              ('../../../../../../home/dev/work/x.worktrees/w/.git', 'wsl'),
                              ('../../../../x.worktrees/w/.git', 'windows')):
            with self.subTest(pointer=pointer):
                self.assertEqual(owning_side(admin, pointer), side)


@integration
class Registry(WorktreeCase):
    """Shared set-up: a main branch with enough files that a content match means something."""
    def setUp(self):
        super().setUp()
        for i in range(20):
            self.r.commit(f'src/f{i}.txt', f'base {i}\n')
        self.r.git('push', '-q', 'origin', 'HEAD:main')
        self.common = self.r.repo / '.git'

    def admin(self, name):
        return self.common / 'worktrees' / name

    def expiry(self):
        done = placement.git(self.r.repo, 'config', '--get', 'gc.worktreePruneExpire', timeout=30)
        return done.stdout.strip()

    def rows(self, result, code):
        return [i for i in result['items'] if i.get('reason_code') == code]

    def add(self, name, branch, *, lock=True):
        path = self.trees / name
        self.r.git('worktree', 'add', '-q', '-b', branch, path, 'origin/main')
        if lock:
            self.r.git('worktree', 'lock', '--reason', self.side, path)
        return path

    def lose(self, name):
        shutil.rmtree(self.admin(name))   # what a foreign prune leaves: the directory, its .git file, no registration

    def apply(self):
        self.cleanup(mode='daily')      # apply requires a dry report of the same scope first
        return self.cleanup(apply=True, mode='daily')

    def listing(self):
        return self.r.git('worktree', 'list', '--porcelain')


@integration
class Prevent(Registry):
    def test_backstop_locks_unlocked_worktrees_and_sets_expiry_on_apply_only(self):
        free = self.add('free', 'b-free', lock=False)
        noted = self.add('noted', 'b-noted', lock=False)
        self.r.git('worktree', 'lock', '--reason', 'hx-guts T31', noted)
        self.assertNotEqual(self.expiry(), 'never')
        dry = self.cleanup(mode='daily')
        self.assertFalse((self.admin('free') / 'locked').exists(), 'a report changes nothing')
        self.assertNotEqual(self.expiry(), 'never')
        self.assertEqual([r['path'] for r in self.rows(dry, 'unlocked_worktree')], [str(free)])
        self.assertEqual(len(self.rows(dry, 'prune_expiry_unset')), 1)
        applied = self.apply()
        self.assertEqual((self.admin('free') / 'locked').read_text().strip(), self.side)
        self.assertEqual((self.admin('noted') / 'locked').read_text().strip(), 'hx-guts T31')   # a note is kept
        self.assertEqual(self.expiry(), 'never')
        self.assertEqual({r['action'] for r in self.rows(applied, 'unlocked_worktree')}, {'locked'})
        again = self.apply()
        self.assertEqual(self.rows(again, 'unlocked_worktree') + self.rows(again, 'prune_expiry_unset'), [])

    def test_an_unlocked_worktree_survives_a_prune_only_once_locked(self):
        path = self.add('vanishing', 'b-vanishing', lock=False)
        self.apply()
        shutil.move(str(path), str(path) + '.away')   # as seen by the other side's git: the path is gone
        self.r.git('worktree', 'prune')
        self.assertIn('b-vanishing', self.listing(), 'locked registration is not pruned')

    def test_a_missing_directory_on_this_side_is_left_prunable(self):
        path = self.add('gone', 'b-gone', lock=False)
        shutil.rmtree(path)
        result = self.apply()
        self.assertFalse((self.admin('gone') / 'locked').exists())
        self.assertEqual(self.rows(result, 'unlocked_worktree'), [])

    def test_worktree_add_sets_the_prune_expiry(self):
        self.r.git('branch', 'created-api')
        worktree.add(str(self.r.repo), str(self.trees / 'created'), 'created-api', owner='lane:x', lease_until=self.now + 1)
        self.assertEqual(self.expiry(), 'never')
        self.assertEqual((self.admin('created') / 'locked').read_text().strip(), self.side)


@integration
class Repair(Registry):
    def setUp(self):
        super().setUp()
        self.good = self.add('drover-good', 'drover/good')
        (self.good / 'untracked.txt').write_text('wip')
        (self.good / 'src/f1.txt').write_text('edited, uncommitted\n')
        self.dup = self.add('drover-dup', 'drover/dup')
        self.bad = self.add('drover-bad', 'drover/bad')
        self.nameless = self.add('mystery', 'other-branch')
        for i in range(20):    # the directory holds files its branch does not
            (self.bad / f'src/f{i}.txt').write_text(f'something else {i}\n')
        for name in ('drover-good', 'drover-dup', 'drover-bad', 'mystery'):
            self.lose(name)
        self.r.git('worktree', 'add', '-q', self.r.root / 'elsewhere', 'drover/dup')   # the dup branch is taken
        self.files = {p: p.read_bytes() for d in (self.good, self.dup, self.bad, self.nameless)
                      for p in d.rglob('*') if p.is_file() and '.git' not in p.relative_to(d).parts[:1]}
        self.pointers = {d: (d / '.git').read_text() for d in (self.good, self.bad)}

    def assert_untouched(self, *dirs):
        for p, data in self.files.items():
            if any(d in p.parents for d in dirs):
                self.assertEqual(p.read_bytes(), data, p)

    def test_report_names_each_decision_and_changes_nothing(self):
        before = self.listing()
        result = self.cleanup(mode='daily')
        found = {Path(r['path']).name: r for r in result['items'] if r['reason_code'].startswith('lost_registration')}
        self.assertEqual(set(found), {'drover-good', 'drover-dup', 'drover-bad', 'mystery'})
        self.assertEqual((found['drover-good']['reason_code'], found['drover-good']['action'], found['drover-good']['branch']),
                         ('lost_registration', 'report', 'drover/good'))
        self.assertIn('checked out at', found['drover-dup']['detail'])
        self.assertIn('does not match', found['drover-bad']['detail'])
        self.assertIn('no branch', found['mystery']['detail'])
        for name in ('drover-dup', 'drover-bad', 'mystery'):
            self.assertEqual((found[name]['reason_code'], found[name]['action']), ('lost_registration_unproven', 'keep'))
        self.assertEqual(self.listing(), before)
        self.assertFalse((self.admin('drover-good')).exists())
        self.assertEqual([r['path'] for r in result['items'] if Path(r['path']).parent == self.trees and not r['reason_code'].startswith('lost_registration')], [], 'not also an unlisted folder')
        self.assert_untouched(self.good, self.dup, self.bad, self.nameless)

    def test_apply_reregisters_only_the_proven_directory_and_keeps_every_file(self):
        self.apply()
        listing = self.listing()
        self.assertIn(str(self.good).replace('\\', '/'), listing.replace('\\', '/'))
        self.assertIn('branch refs/heads/drover/good', listing)
        self.assertEqual((self.admin('drover-good') / 'locked').read_text().strip(), self.side)
        for d in (self.dup, self.bad, self.nameless):
            self.assertNotIn(d.name, listing)
            self.assertTrue(d.is_dir())
        self.assert_untouched(self.good, self.dup, self.bad, self.nameless)
        status = self.r.git('status', '--short', cwd=self.good)
        self.assertIn('?? untracked.txt', status)
        self.assertIn('M src/f1.txt', status)
        self.assertEqual(self.r.git('rev-parse', 'HEAD', cwd=self.good), self.r.git('rev-parse', 'drover/good'))
        backups = list((self.state / 'disk' / 'registration-backup').iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), self.pointers[self.good])
        self.assertEqual([p for p in self.good.parent.iterdir() if p.name.startswith('.drover-repair')], [])
        done = [r for r in self.journal() if r.get('kind') == 'registration_repair']
        self.assertEqual([r['phase'] for r in done], ['planned', 'completed'])

    def test_a_second_apply_changes_nothing_and_keeps_one_backup(self):
        self.apply()
        before = self.listing()
        again = self.apply()
        self.assertEqual(self.listing(), before)
        self.assertEqual(len(list((self.state / 'disk' / 'registration-backup').iterdir())), 1)
        self.assertEqual([r for r in again['items'] if Path(r['path']).name == 'drover-good'
                          and r['reason_code'].startswith('lost_registration')], [])

    def test_unproven_directories_are_never_deleted_even_when_aged(self):
        for _ in range(2):
            self.now += 10 * 86400
            for d in (self.dup, self.bad, self.nameless):
                self.age(d, 30 * 86400)
            self.apply()
        for d in (self.dup, self.bad, self.nameless):
            self.assertTrue(d.is_dir())
        self.assert_untouched(self.dup, self.bad, self.nameless)


@integration
class Nested(Registry):
    """Real worktrees live below the root: `hx/api-names`, `land/gui-perf-20261027`, and herdr's `feat/x`."""
    def setUp(self):
        super().setUp()
        self.slash = self.add('hx/api-names', 'hx/api-names')
        self.feat = self.add('feat/x', 'feat/x')
        (self.feat / 'new.txt').write_text('wip')
        self.dash = self.add('land/gui-perf-20261027', 'land/gui-perf-20261027')
        self.flat = self.add('plain', 'plain')
        self.files = {p: p.read_bytes() for d in (self.slash, self.feat, self.dash, self.flat)
                      for p in d.rglob('*') if p.is_file() and p.parts[len(d.parts)] != '.git'}
        shutil.rmtree(self.common / 'worktrees')
        self.decoy = self.trees / 'node_modules' / 'pkg'      # a .git file inside disposable output is not a worktree
        self.decoy.mkdir(parents=True)
        (self.decoy / '.git').write_text('gitdir: ' + str(self.common / 'worktrees' / 'pkg') + '\n')

    def test_nested_directories_are_found_and_matched_by_relative_path(self):
        before = self.listing()
        result = self.cleanup(mode='daily')
        found = {Path(r['path']).relative_to(self.trees).as_posix(): r for r in result['items']
                 if r['reason_code'].startswith('lost_registration')}
        self.assertEqual(set(found), {'hx/api-names', 'feat/x', 'land/gui-perf-20261027', 'plain'})
        self.assertEqual({k: v['branch'] for k, v in found.items()},
                         {k: k for k in found})
        self.assertEqual({v['action'] for v in found.values()}, {'report'})
        self.assertEqual(self.listing(), before)

    def test_apply_registers_nested_directories_and_keeps_files(self):
        self.apply()
        listing = self.listing()
        for branch in ('hx/api-names', 'feat/x', 'land/gui-perf-20261027', 'plain'):
            self.assertIn('branch refs/heads/' + branch, listing)
        self.assertIn('?? new.txt', self.r.git('status', '--short', cwd=self.feat))
        for p, data in self.files.items():
            self.assertEqual(p.read_bytes(), data)
        locks = sorted(p.read_text().strip() for p in (self.common / 'worktrees').glob('*/locked'))
        self.assertEqual(locks, [self.side] * 4)


@integration
class Suggest(Registry):
    def test_unnamed_directory_gets_a_report_only_branch_suggestion(self):
        path = self.add('mystery', 'work/real-name')
        (path / 'src/own.txt').write_text('committed on the branch\n')
        self.r.git('add', 'src/own.txt', cwd=path)
        self.r.git('commit', '-qm', 'own', cwd=path)
        self.lose('mystery')
        before = self.listing()
        for apply in (False, True):
            result = self.apply() if apply else self.cleanup(mode='daily')
            row = next(r for r in result['items'] if Path(r['path']).name == 'mystery')
            self.assertEqual((row['reason_code'], row['action']), ('lost_registration_unproven', 'keep'))
            self.assertEqual(row['suggestion'], 'work/real-name')
            self.assertIn('suggestion', row['detail'])
        self.assertEqual(self.listing(), before)
        self.assertTrue((path / 'src/own.txt').exists())

    def test_no_suggestion_when_the_newest_file_is_on_many_branches(self):
        path = self.add('mystery', 'other')
        self.lose('mystery')
        result = self.cleanup(mode='daily')
        row = next(r for r in result['items'] if Path(r['path']).name == 'mystery')
        self.assertIsNone(row.get('suggestion'))


@integration
class Interrupted(Registry):
    def test_a_registration_pointing_at_a_vanished_place_is_finished(self):
        path = self.add('drover-half', 'drover/half')
        (path / 'wip.txt').write_text('keep me')
        admin = self.admin('drover-half')
        (admin / 'gitdir').write_text(str(self.r.root / 'gone' / 'drover-half' / '.git') + '\n')   # temp parent vanished
        before = {p: p.read_bytes() for p in path.rglob('*') if p.is_file() and p.name != '.git'}
        dry = self.cleanup(mode='daily')
        row = next(r for r in dry['items'] if r['reason_code'] == 'lost_registration')
        self.assertEqual(Path(row['path']), path)
        self.apply()
        self.assertEqual((admin / 'gitdir').read_text().strip().replace('\\', '/').rstrip('/').lower(),
                         str(path / '.git').replace('\\', '/').lower())
        self.assertEqual(self.r.git('status', '--short', cwd=path), '?? wip.txt')
        self.assertEqual(before, {p: p.read_bytes() for p in path.rglob('*') if p.is_file() and p.name != '.git'})
        again = self.cleanup(mode='daily')
        self.assertEqual([r for r in again['items'] if r['reason_code'].startswith('lost_registration')], [])
