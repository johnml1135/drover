"""A slow folder must not void a repo's cleanup report: a git timeout is one folder's unproven row, not the repo's."""
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from drover import disk_registrations, disk_removal, disk_worktrees, git_client, git_worktrees, placement
from drover.disk_stage import StageClock
from tests.disk_fixture import answer, listing_answers, stage_run, staged

TIMEOUT = ValueError('git rev-parse failed: timeout')      # what a git timeout gives the disk callers, through `git_out`
SPAWN_TIMEOUT = subprocess.TimeoutExpired(['git'], 60)    # the spawn's own, below git_client
MACHINE = dict(name='alpha', repo='/repo')
CLOCK = StageClock(1800)


def at(*times):
    """A clock reading 0 at the stage's start, then each of `times` in turn, holding the last."""
    readings = [0.0, *times]
    return lambda: readings.pop(0) if len(readings) > 1 else readings[0]


def request(apply=False, drive='/'):
    return dict(apply=apply, now=0, state_dir='state', drive=dict(path=drive))


def found(*names):
    return {f'/nowhere/{n}': (Path(f'/nowhere/admin-{n}'), n) for n in names}


class FolderTimeouts(unittest.TestCase):
    def test_judge_timeout_keeps_that_folder_and_judges_the_next(self):
        outcomes = [TIMEOUT, ('repair', 'b2', 'ok', None)]
        with patch.object(disk_registrations, 'judge', side_effect=outcomes):
            rows = list(disk_registrations.report(request(), MACHINE, [], found('one', 'two'), None, evidence=None, clock=CLOCK))
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
            [row] = disk_registrations.report(request(apply=True), MACHINE, [], found('one'), None, evidence=None, clock=CLOCK)
        self.assertEqual((row['action'], row['reason_code']), ('keep', 'lost_registration_failed'))


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
        evidence = SimpleNamespace(repaired=lambda repo, path: told.append((repo, path)), gitdir=lambda path: str(tmp / 'admin'))
        with patch.object(placement, 'add_worktree', side_effect=add), \
                patch.object(disk_registrations, 'git_out', return_value=''), \
                patch.object(git_worktrees, 'listing', side_effect=listing):
            disk_registrations.reregister(dict(now=0, state_dir=str(tmp / 'state')), MACHINE, orphan, None, 'b',
                                          SimpleNamespace(append=records.append), evidence=evidence,
                                          clock=StageClock(1800, clock=at(1400)))
        self.assertEqual(asked, [40])       # patience(400) 40 s before the line, not 400 s
        self.assertEqual(([r['phase'] for r in records], told), (['planned', 'completed'], [('/repo', orphan)]))

    def test_a_completed_append_takes_the_removal_journal_retries(self):
        """Task 43's carry: a repair that took is not reported failed for one missed `completed` append, and a journal
        that stays unwritable is returned for the row, not raised."""
        for misses, records_left, note in ((1, ['planned', 'completed'], None),
                                           (9, ['planned'], 'journal completed failed: journal busy')):
            with self.subTest(misses=misses):
                tmp, orphan = self.orphan()
                (tmp / 'admin').mkdir()
                (tmp / 'admin' / 'index').write_text('', encoding='utf-8')
                records, tried, pauses = [], [], []

                def append(record, tried=tried, records=records, misses=misses):
                    tried.append(record['phase'])
                    if record['phase'] == 'completed' and tried.count('completed') <= misses:
                        raise OSError('journal busy')
                    records.append(record)

                def add(repo, place, *args, **kwargs):
                    Path(place).mkdir(parents=True)
                    (Path(place) / '.git').write_text('gitdir: new', encoding='utf-8')
                evidence = SimpleNamespace(repaired=lambda repo, path: None, gitdir=lambda path: str(tmp / 'admin'))
                with patch.object(placement, 'add_worktree', side_effect=add), \
                        patch.object(disk_registrations, 'git_out', return_value=''), \
                        patch.object(git_worktrees, 'listing', return_value=git_worktrees.Listing('/repo', (dict(worktree=str(orphan)),))), \
                        patch.object(disk_removal, 'time', SimpleNamespace(monotonic=time.monotonic, sleep=pauses.append)):
                    answer = disk_registrations.reregister(dict(now=0, state_dir=str(tmp / 'state')), MACHINE, orphan, None,
                                                           'b', SimpleNamespace(append=append), evidence=evidence,
                                                           clock=StageClock(1800, clock=at(10)))
                self.assertEqual(([r['phase'] for r in records], answer), (records_left, note))
                self.assertEqual(pauses, list(disk_removal.JOURNAL_PAUSES_S[:misses]))

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
                patch.object(disk_registrations, 'git_out', side_effect=out), \
                patch.object(git_worktrees, 'listing', return_value=git_worktrees.Listing('/repo', (dict(worktree=str(orphan)),))):
            disk_registrations.reregister(dict(now=0, state_dir=str(tmp / 'state')), MACHINE, orphan, None, 'b',
                                          SimpleNamespace(append=lambda record: None),
                                          evidence=SimpleNamespace(repaired=lambda repo, path: None,
                                                                   gitdir=lambda path: str(tmp / 'admin')),
                                          clock=StageClock(100, clock=at(10)))
        self.assertEqual(asked, [('add', 4), ('repair', 4), ('reset', 4)])     # section 6.1:643: each mutating step's cap


C1, C2, C3, C4 = ('1' * 40, '2' * 40, '3' * 40, '4' * 40)
ZERO = '0' * 40
BLOBS = {'only-one\n': 'a' * 40, 'everywhere\n': 'b' * 40, 'only-two\n': 'c' * 40, 'uncommitted\n': 'd' * 40}
A, B, C, D = BLOBS.values()
LOG = [(C3, [(ZERO, A)]), (C4, [(ZERO, C)]), (C2, []), (C1, [(ZERO, B)])]       # newest first; C2 shows no diff
PARENTS = {C3: [C2], C4: [C1], C2: [C1], C1: []}
TIPS = {'main': C2, 'work/one': C3, 'work/two': C4}
C5 = '5' * 40
HEAD_ONLY = {C5: [C4]}      # a detached HEAD's own commit, on no branch


class Suggestions(unittest.TestCase):
    """A report-only suggestion costs one history read per repo, not one `git log` and one `git branch --contains` per
    file: those were the live run's 25 s and 5 s per file, five files per lost folder, every run."""
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__('shutil').rmtree, self.tmp, True)
        self.spawns, self.detached, self.history_fails, self.head = [], False, False, C3

    def folder(self, name, *files):
        path = self.tmp / name
        path.mkdir()
        for age, (file, text) in enumerate(files):        # the first file is the newest
            (path / file).write_text(text, encoding='utf-8')
            __import__('os').utime(path / file, (1000 - age, 1000 - age))
        return path

    def reaches(self, tip, commit):
        return tip == commit or any(self.reaches(p, commit) for p in dict(PARENTS, **HEAD_ONLY)[tip])

    def contains(self, commit):
        return sorted(name for name, tip in TIPS.items() if self.reaches(tip, commit))

    def git(self, repo, *args, timeout):
        self.spawns.append((args, timeout))
        done = lambda out, code=0: subprocess.CompletedProcess(args, code, out, '')
        if args == ('rev-parse', '--verify', 'HEAD'):
            return done(self.head + '\n')
        if args[:2] == ('rev-parse', '--verify'):
            return done('', 1)
        if args[0] == 'hash-object':
            return done(BLOBS[Path(args[-1]).read_text(encoding='utf-8')] + '\n')
        if args[0] == 'log' and any(a.startswith('--find-object=') for a in args):
            blob = next(a for a in args if a.startswith('--find-object='))[len('--find-object='):]
            hit = next((c for c, pairs in LOG if any(blob in pair for pair in pairs)), None)
            return done(f'{hit}\n\n:100644 100644 {ZERO} {blob} M\tf\n' if hit else '')
        if args[0] == 'log':
            if self.history_fails:
                raise SPAWN_TIMEOUT
            return done(''.join(f'{c}\n' + ''.join(f'\n:000000 100644 {old} {new} A\tf{n}\n' for n, (old, new) in enumerate(pairs))
                                for c, pairs in LOG))
        if args[:2] == ('branch', '--contains'):     # what git prints, its detached HEAD first
            on_head = self.detached and self.reaches(self.head, args[2])
            held = (['(HEAD detached at %s)' % self.head[:9]] if on_head else []) + self.contains(args[2])
            return done(''.join(h + '\n' for h in held))
        if args[:2] == ('rev-list', '--branches'):
            graph = dict(PARENTS, **(HEAD_ONLY if 'HEAD' in args else {}))
            return done(''.join(' '.join([c, *ps]) + '\n' for c, ps in graph.items()))
        if args[0] == 'for-each-ref':
            return done(''.join(f'{tip} {name}\n' for name, tip in TIPS.items()))
        if args[:3] == ('symbolic-ref', '-q', 'HEAD'):
            return done('', 1) if self.detached else done('refs/heads/main\n')
        raise AssertionError(f'unexpected git {args!r}')

    def report(self, clock=CLOCK):
        folders = [self.folder('f1', ('a.txt', 'only-one\n')),
                   self.folder('f2', ('b.txt', 'everywhere\n'), ('c.txt', 'only-two\n')),
                   self.folder('f3', ('z.txt', 'uncommitted\n'))]
        lost = {str(p): (self.tmp / 'gone' / p.name, p.name) for p in folders}
        return self.reported(lost, self.tmp, clock)

    def reported(self, lost, drive, clock=CLOCK):
        """Report `lost` as the stage of `drive` sees them; a folder is on a drive by path prefix."""
        from tests.fakeci import git_answers
        with git_answers(self.git), patch.object(placement, 'cleanup_contains', lambda root, path: str(path).startswith(str(root))),                 patch.object(disk_registrations, 'newest_files', wraps=disk_registrations.newest_files) as walk:
            rows = list(disk_registrations.report(request(drive=str(drive)), MACHINE, [], lost, None, evidence=None, clock=clock))
        self.walked = walk.call_count
        return {Path(r['path']).name: r for r in rows}

    def verbs(self, *verb):
        return [args for args, _ in self.spawns if args[:len(verb)] == verb]

    def test_three_folders_read_the_history_once_and_get_the_same_suggestions(self):
        rows = self.report()
        self.assertEqual({k: r.get('suggestion') for k, r in rows.items()}, dict(f1='work/one', f2='work/two', f3=None))
        self.assertEqual({r['reason_code'] for r in rows.values()}, {'lost_registration_unproven'})
        self.assertEqual(len(self.verbs('log')), 1)
        self.assertEqual([a for a in self.verbs('log') if any(x.startswith('--find-object') for x in a)], [])
        self.assertEqual(self.verbs('branch', '--contains'), [])

    def test_the_one_history_read_takes_the_long_patience_not_one_reads_sixty_seconds(self):
        self.report()
        [timeout] = [t for args, t in self.spawns if args[0] == 'log']
        self.assertGreater(timeout, git_client.NORMAL)

    def test_a_detached_head_counts_as_git_branch_lists_it(self):
        self.detached = True        # `git branch --contains` lists the detached HEAD too, so work/one is not alone
        rows = self.report()
        self.assertEqual({k: r.get('suggestion') for k, r in rows.items()}, dict(f1=None, f2='work/two', f3=None))
        self.assertEqual((len(self.verbs('log')), self.verbs('branch', '--contains')), (1, []))

    def test_a_detached_head_on_no_branch_still_holds_its_ancestors(self):
        self.detached, self.head = True, C5     # past work/two's tip: work/two is no longer alone in holding C4
        rows = self.report()
        self.assertEqual({k: r.get('suggestion') for k, r in rows.items()}, dict(f1='work/one', f2=None, f3=None))
        self.assertEqual(self.verbs('branch', '--contains'), [])

    def test_a_failed_history_read_leaves_every_folder_unproven_and_is_not_repeated(self):
        self.history_fails = True
        rows = self.report()
        self.assertEqual({(r['action'], r['reason_code']) for r in rows.values()}, {('keep', 'lost_registration_unproven')})
        self.assertTrue(all(r['detail'].startswith('could not be matched: ') for r in rows.values()))
        self.assertEqual(len(self.verbs('log')), 1)

    def test_past_the_line_no_suggestion_is_looked_for_and_the_folder_stays_unproven(self):
        rows = self.report(clock=StageClock(100, clock=at(85)))
        self.assertEqual({(r['action'], r['reason_code'], r.get('suggestion')) for r in rows.values()},
                         {('keep', 'lost_registration_unproven', None)})
        self.assertTrue(all('past the stage line' in r['detail'] for r in rows.values()))
        self.assertEqual(self.verbs('log') + self.verbs('hash-object'), [])

    # A lost folder off the stage's drive still lists, keeps and repairs; only its report-only suggestion waits for its
    # own drive's pass, so each folder pays the history read and the walk once, not once per drive.
    TEXT = "suggestion on its own drive's pass"

    def on(self, drive, *names):
        folders = [self.folder(n, (n + '.txt', 'only-one\n')) for n in names]
        return self.reported({str(p): (self.tmp / 'gone' / p.name, p.name) for p in folders}, drive)

    def test_a_folder_off_the_stage_drive_gets_no_history_lookup_and_the_fixed_text(self):
        rows = self.on(self.tmp / 'elsewhere', 'f1')
        self.assertEqual((rows['f1']['action'], rows['f1']['reason_code'], rows['f1'].get('suggestion')),
                         ('keep', 'lost_registration_unproven', None))
        self.assertIn(self.TEXT, rows['f1']['detail'])
        self.assertEqual(self.walked, 0)
        self.assertEqual(self.verbs('log') + self.verbs('hash-object') + self.verbs('rev-list') + self.verbs('for-each-ref'), [])

    def test_a_folder_on_the_stage_drive_still_gets_its_suggestion(self):
        rows = self.on(self.tmp, 'f1')
        self.assertEqual(rows['f1'].get('suggestion'), 'work/one')
        self.assertNotIn(self.TEXT, rows['f1']['detail'])
        self.assertEqual(len(self.verbs('log')), 1)

    def test_the_other_drives_folder_is_still_matched_for_repair(self):
        with patch.object(disk_registrations, 'candidate', return_value='b1'),                 patch.object(disk_registrations, 'differing', return_value=(0, 10)):
            rows = self.on(self.tmp / 'elsewhere', 'f1')
        self.assertEqual((rows['f1']['action'], rows['f1']['branch']), ('report', 'b1'))
        self.assertNotIn(self.TEXT, rows['f1']['detail'])

    def test_a_blob_only_deleted_by_a_commit_is_suggested_as_per_file_git_would(self):
        deleted = [(C3, [(A, ZERO)])] + LOG[1:]        # work/one's tip holds f1's blob only as the old side of a deletion
        with patch.dict(globals(), LOG=deleted):
            rows = self.report()
        self.assertEqual(rows['f1'].get('suggestion'), 'work/one')

    def test_a_blob_in_two_commits_on_different_branches_is_suggested_from_the_newest_commit(self):
        twice = [(C3, [(ZERO, A)]), (C4, [(ZERO, C), (ZERO, A)])] + LOG[2:]     # C3 is newer; C4, on work/two, also adds it
        with patch.dict(globals(), LOG=twice):
            rows = self.report()
        self.assertEqual(rows['f1'].get('suggestion'), 'work/one')


class NewestFiles(unittest.TestCase):
    """The suggestion's walk asks the file system once per file: on 9p each ask cost about 4 ms, and the old walk's
    three per file took 115 s on one motif folder of 4,184 files."""
    def test_one_stat_per_file_and_the_same_newest_files(self):
        import os
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__('shutil').rmtree, tmp, True)
        for age, name in enumerate(['src/new.txt', 'a.txt', 'src/deep/b.txt', 'node_modules/x.js', 'obj/y.dll', 'c.txt',
                                    'd.txt', 'e.txt']):
            (tmp / name).parent.mkdir(parents=True, exist_ok=True)
            (tmp / name).write_text(name, encoding='utf-8')
            os.utime(tmp / name, (1000 - age, 1000 - age))
        asked = []
        real_stat, real_islink = os.stat, os.path.islink
        with patch.object(os, 'stat', side_effect=lambda p, *a, **k: asked.append('stat') or real_stat(p, *a, **k)), \
                patch.object(os.path, 'islink', side_effect=lambda p: asked.append('islink') or real_islink(p)):
            got = disk_registrations.newest_files(tmp)
        self.assertEqual(got, ['src/new.txt', 'a.txt', 'src/deep/b.txt', 'c.txt', 'd.txt'])
        self.assertLessEqual(len(asked), 6)        # six files outside build output; the old walk asked 15 times

    def test_links_are_neither_followed_nor_listed(self):
        import os
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__('shutil').rmtree, tmp, True)
        (tmp / 'real').mkdir()
        (tmp / 'real' / 'f.txt').write_text('f', encoding='utf-8')
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(__import__('shutil').rmtree, outside, True)
        (outside / 'secret.txt').write_text('s', encoding='utf-8')
        try:
            os.symlink(outside, tmp / 'dirlink', target_is_directory=True)
            os.symlink(tmp / 'real' / 'f.txt', tmp / 'filelink')
            os.symlink(tmp / 'gone', tmp / 'broken')
        except (OSError, NotImplementedError):
            self.skipTest('no symlinks here')
        self.assertEqual(disk_registrations.newest_files(tmp), ['real/f.txt'])


class UnknownLostFolders(unittest.TestCase):
    def test_lost_timeout_records_the_error_and_judges_no_orphans(self):
        listed = [dict(worktree='/repo')]
        with listing_answers(listed), \
                patch.object(disk_registrations, 'protect', return_value=[]), \
                patch.object(disk_registrations, 'lost', side_effect=TIMEOUT), \
                patch.object(disk_worktrees, 'unlisted', return_value=[]) as unlisted, \
                patch.object(placement, 'cleanup_processes', return_value=SimpleNamespace(busy=lambda path: False)), \
                patch.object(disk_worktrees, 'evaluate', return_value=dict(items=[], errors=[], reclaimed_bytes=0)):
            out = staged(disk_worktrees.settled, dict(request(), repos=[MACHINE]), runtime=None)
        self.assertEqual([e['repo'] for e in out['errors']], ['/repo'])
        unlisted.assert_not_called()


class ProtectClock(unittest.TestCase):
    """S-I2: the prune-expiry write is a mutation. It takes the stage clock's mutation cap, not the 60 s read default,
    and starts only while the clock can finish it; a refusal is the row kept, with nothing written or removed."""
    EXPIRE = ('config', 'gc.worktreePruneExpire', 'never')

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.common, self.asked = Path(tmp.name), []

    def git(self, repo, *args, timeout):
        self.asked.append((args, timeout))
        if args[:2] == ('config', '--get'):
            return subprocess.CompletedProcess(args, 1, '', '')        # unset
        if args[0] == 'rev-parse':
            return subprocess.CompletedProcess(args, 0, str(self.common) + '\n', '')
        return subprocess.CompletedProcess(args, 0, '', '')

    def writes(self):
        return [timeout for args, timeout in self.asked if args == self.EXPIRE]

    def test_the_prune_expiry_write_takes_the_mutation_cap(self):
        from tests.fakeci import git_answers
        with git_answers(self.git):
            [row] = disk_registrations.protect(request(apply=True), MACHINE, StageClock(100, clock=at(10)))
        self.assertEqual((row['action'], self.writes()), ('configured', [4]))      # 100 s: a 4 s cap, not 60 s

    def test_past_the_line_the_prune_expiry_is_not_written(self):
        from tests.fakeci import git_answers
        with git_answers(self.git):
            [row] = disk_registrations.protect(request(apply=True), MACHINE, StageClock(1800, clock=at(1700)))
        self.assertEqual(self.writes(), [])
        self.assertEqual((row['reason_code'], row['action']), ('prune_expiry_unset', 'keep'))
        self.assertEqual(row['error'], 'protect deadline: past the stage line, the prune expiry was not written')

    def test_past_the_line_the_stage_keeps_the_row_and_removes_nothing(self):
        from tests.fakeci import git_answers
        run = stage_run(dict(request(apply=True), repos=[MACHINE]), clock=at(1700))
        with git_answers(self.git), \
                patch.object(disk_registrations, 'lost', return_value=found('one')), \
                patch.object(disk_registrations, 'reregister', side_effect=AssertionError('nothing may start')):
            disk_worktrees.registrations(run.request.decide_view(), MACHINE, [], None, run)
        out = answer(run)
        self.assertEqual(self.writes(), [])
        self.assertEqual([(r['reason_code'], r['action']) for r in out['items']],
                         [('prune_expiry_unset', 'keep'), ('settled_deadline', 'report')])
        self.assertEqual(out['errors'], [])


if __name__ == '__main__':
    unittest.main()
