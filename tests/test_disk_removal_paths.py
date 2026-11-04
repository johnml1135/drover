"""The remove_* family for paths (section 7.3): a disposable unit, a temp entry and a cache file, and the journal's
retry window, on fakes."""
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from drover import disk_authority, disk_removal, placement
from drover.disk_authority import DisposableUnit, Ruling
from drover.disk_evidence import FreshProcesses, StageEvidence
from drover.disk_stage import StageAbandoned, StageClock, StageRequest
from drover.git_worktrees import Proof, Refusal


def abandoned(*_, **__):
    raise StageAbandoned('the stage lost its reader')


class Disposable(unittest.TestCase):
    """The remove_* family on fakes: a temp worktree `w` whose declared `bin` holds the unit's three paths. This test is
    the stage's evidence, each read noted in `order`; the fresh proofs are real StageEvidence ones; every run method
    that could checkpoint raises StageAbandoned."""
    SIZES = (('a', 3), ('b', 5), ('c', 7))

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.tree = Path(folder.name).absolute() / 'w'
        (self.tree / 'bin').mkdir(parents=True)
        (self.tree / '.git').write_text('gitdir: x', encoding='utf-8')
        self.names = []
        for name, size in self.SIZES:
            (self.tree / 'bin' / name).write_bytes(b'x' * size)
            self.names.append(str(self.tree / 'bin' / name))
        self.measured = {name: dict(files=1, bytes=size, age_s=10 ** 6) for name, (_, size) in zip(self.names, self.SIZES)}
        self.order, self.pointers, self.mutate = [], [], True
        self.entry = dict(worktree=str(self.tree), HEAD='head', branch='refs/heads/w')
        request = StageRequest(stage=5, side='windows', mode=None, apply=True, now=0, drive=dict(path=folder.name),
                               repos=(dict(name='alpha', repo='/repo'),), policy={}, state_dir='/state', tasks={})
        self.real = StageEvidence(request, StageClock(1800), prove=self.prove,
                                  processes=lambda **_: SimpleNamespace(busy=lambda path: False, reason=None))
        self.run = SimpleNamespace(request=request, evidence=self, clock=SimpleNamespace(can_mutate=self.can_mutate, late=lambda: False),
                                   reclaimed=lambda size: self.order.append(('reclaimed', size)),
                                   checkpoint=abandoned, add=abandoned, stop=abandoned, error=abandoned)

    # The stage evidence remove_disposable reads.
    def row(self, repo, path):
        self.order.append('row')
        return dict(self.entry)

    def status(self, path, *, expand=True):
        self.order.append('status')
        return [], list(self.names)

    def fresh_processes(self):
        self.order.append('fresh_processes')
        return self.real.fresh_processes()

    def tracked(self, path, relative=None):
        self.order.append('tracked')
        return []

    def measure(self, path):
        self.order.append('measure')
        return self.measured[str(path)]

    def fresh_registrations(self, repo, target, own, *, pointer):
        self.order.append('fresh_registrations')
        return self.real.fresh_registrations(repo, target, own, pointer=pointer)

    def fresh_snapshot(self):
        return None

    def prove(self, listings, repo, own, *, pointer, timeout_s):
        """As git_worktrees.prove reads `.git`: present where `pointer` says it should be, or the proof refuses."""
        self.pointers.append(pointer)
        present = (Path(own) / '.git').exists()
        return Proof((Refusal(repo, own, 'changed', '.git'),) if present is not pointer else (), ())

    def can_mutate(self):
        self.order.append('can_mutate')
        return self.mutate

    def remove(self):
        row = dict(path=str(self.tree / 'bin'), action='candidate', reason_code='declared_disposable')
        unit = DisposableUnit(machine=dict(name='alpha', repo='/repo'), registration=dict(self.entry),
                              evidence=(self.tree, 'head', None, ('bin',)), touched=set(), target=self.tree / 'bin',
                              measured=self.measured, row=row)

        def first(run, machine, registration, processes, *, touched=()):
            self.order.append('rule_disposable_fresh')
            self.assertIsInstance(processes, FreshProcesses)
            return Ruling('candidate', 'declared_disposable',
                          facts=dict(path=self.tree, head='head', tag=None, declarations=('bin',)))

        def last(run, unit, proof):
            self.order.append('rule_unit_fresh')
            return disk_authority.rule_unit_fresh(run, unit, proof)
        with mock.patch.object(disk_removal, 'rule_disposable_fresh', first), \
                mock.patch.object(disk_removal, 'rule_unit_fresh', last), \
                mock.patch.object(placement, 'cleanup_contains', return_value=True):
            disk_removal.remove_disposable(self.run, unit)
        return row

    def left(self):
        return [name for name in self.names if Path(name).exists()]

    def test_disposable_ordering_is_section_7_3(self):
        """Listing row, status; fresh processes and the rule, tracked, compare; fresh registrations and the unit's last
        rule; the clock; then each path, its bytes counted as it goes."""
        row = self.remove()
        self.assertEqual(self.order, ['row', 'status', 'fresh_processes', 'rule_disposable_fresh', 'tracked',
                                      'measure', 'measure', 'measure', 'fresh_registrations', 'rule_unit_fresh',
                                      'can_mutate', ('reclaimed', 3), ('reclaimed', 5), ('reclaimed', 7)])
        self.assertEqual((row['action'], self.left(), (self.tree / 'bin').exists()), ('removed', [], False))
        self.assertEqual(self.pointers, [True])

    def test_bytes_count_as_each_path_goes_on_a_removal_cut_short(self):
        middle = Path(self.names[1])
        middle.unlink()
        (middle / 'inner').mkdir(parents=True)      # rmdir refuses a folder that is not empty: the second path fails
        row = self.remove()
        self.assertEqual([step for step in self.order if isinstance(step, tuple)], [('reclaimed', 3)])
        self.assertEqual(self.left(), self.names[1:])
        self.assertEqual((row['action'], row['reason_code']), ('keep', 'output_evidence_unavailable'))
        self.assertTrue(row['error'])

    def test_a_disposable_with_no_dot_git_is_refused(self):
        (self.tree / '.git').unlink()
        row = self.remove()
        self.assertEqual(self.pointers, [True])
        self.assertEqual((row['action'], row['reason_code'], row['error']),
                         ('keep', 'output_evidence_unavailable', 'disposable registration changed (.git)'))
        self.assertNotIn('can_mutate', self.order)
        self.assertEqual(self.left(), self.names)

    def test_a_unit_the_clock_cannot_finish_keeps_as_removal_deadline(self):
        """Gated like every other removal, after its fresh proofs and before its first path: the first unit of a
        worktree included."""
        self.mutate = False
        row = self.remove()
        self.assertEqual((row['action'], row['reason_code']), ('keep', 'removal_deadline'))
        self.assertEqual(self.order[-2:], ['rule_unit_fresh', 'can_mutate'])
        self.assertEqual(self.left(), self.names)

    def test_stage_abandoned_never_raised_inside_remove(self):
        """Each remove_* ends in its row, never a checkpoint: removed, cut short, refused or past the clock."""
        outcomes = [self.remove()['action']]
        (self.tree / 'bin').mkdir()
        Path(self.names[1]).mkdir()     # not empty below: the second path fails
        (Path(self.names[1]) / 'inner').mkdir()
        for name, (_, size) in zip(self.names[::2], self.SIZES[::2]):
            Path(name).write_bytes(b'x' * size)
        outcomes.append(self.remove()['reason_code'])
        Path(self.names[0]).write_bytes(b'xxx')     # the cut-short removal took `a`
        self.mutate = False
        outcomes.append(self.remove()['reason_code'])
        self.mutate = True
        (self.tree / '.git').unlink()
        outcomes.append(self.remove()['action'])
        leaf = self.tree / 'leaf'
        leaf.write_bytes(b'xx')
        for remove, args in ((disk_removal.remove_temp_entry, (leaf,)), (disk_removal.remove_cache_file, (leaf, leaf.stat()))):
            self.mutate = False
            row = dict(path=str(leaf), action='candidate', reason_code='stale_temp', bytes=2)
            with mock.patch.object(placement, 'cleanup_contains', return_value=True):
                remove(self.run, row, *args)
            outcomes.append(row['reason_code'])
        self.assertEqual(outcomes, ['removed', 'output_evidence_unavailable', 'removal_deadline', 'keep', 'removal_deadline',
                                    'removal_deadline'])

    def test_a_temp_entry_goes_whole_and_counts_its_row(self):
        for path in (Path(self.names[0]), self.tree / 'bin'):
            with self.subTest(path=path.name):
                self.order.clear()
                row = dict(path=str(path), action='candidate', reason_code='stale_temp', bytes=15)
                disk_removal.remove_temp_entry(self.run, row, path)
                self.assertEqual((row['action'], path.exists(), self.order), ('removed', False, ['can_mutate', ('reclaimed', 15)]))
        self.mutate, row = False, dict(path=str(self.tree), action='candidate', reason_code='build_root_idle', bytes=1)
        disk_removal.remove_temp_entry(self.run, row, self.tree)
        self.assertEqual((row['action'], row['reason_code'], self.tree.exists()), ('keep', 'removal_deadline', True))

    def test_a_cache_file_goes_only_as_it_was_measured(self):
        leaf = Path(self.names[0])
        info = leaf.stat()
        row = dict(path=str(leaf), action='candidate', reason_code='folder_cache')
        leaf.write_bytes(b'longer now')
        with mock.patch.object(placement, 'cleanup_contains', return_value=True):
            disk_removal.remove_cache_file(self.run, row, leaf, info)
            self.assertEqual((row['action'], leaf.exists()), ('candidate', True))
            info = leaf.stat()
            disk_removal.remove_cache_file(self.run, row, leaf, info)
        self.assertEqual((row['action'], leaf.exists(), self.order[-1]), ('removed', False, ('reclaimed', info.st_size)))

    def test_a_journal_retry_window_ends_with_the_stage(self):
        """The `completed` or `failed` retries stay inside both the mutation cap and what is left of the stage."""
        for cap, left, pauses_taken in ((60.0, 0.1, disk_removal.JOURNAL_PAUSES_S[:1]), (0.1, 60.0, disk_removal.JOURNAL_PAUSES_S[:1]),
                                        (60.0, 60.0, disk_removal.JOURNAL_PAUSES_S)):
            with self.subTest(cap=cap, left=left):
                tried, pauses, row = [], [], {}

                def append(record, tried=tried):
                    tried.append(record['phase'])
                    raise OSError('journal gone')
                clock = SimpleNamespace(mutation_cap=lambda cap=cap: cap, left=lambda left=left: left)
                with mock.patch.object(disk_removal, 'time', SimpleNamespace(monotonic=time.monotonic, sleep=pauses.append)):
                    note = disk_removal.close_journal(SimpleNamespace(append=append), dict(phase='planned'), 'completed',
                                                      clock, row)
                self.assertEqual((pauses, len(tried)), (list(pauses_taken), len(pauses_taken) + 1))
                self.assertEqual((row['error'], note), ('journal completed failed: journal gone',) * 2)
