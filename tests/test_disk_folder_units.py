"""Stage 5 removes a folder git reports collapsed as one unit, as its dry run lists it, every guard run once for it."""
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from drover import disk_authority, disk_paths, disk_removal, git_worktrees, placement
from drover.disk_authority import DisposableUnit, Ruling
from drover.disk_evidence import StageEvidence
from drover.disk_stage import StageAbandoned, StageClock, StageRequest
from drover.git_worktrees import Proof, Refusal
from tests.disk_fixture import stage_run
from tests.disk_memory import MemoryWorktreeCase
from tests.test_disk_stage import stage_run as bare_stage_run


class StageUnits(MemoryWorktreeCase):
    """The memory repository declares and ignores `generated`, which git reports collapsed while nothing under it is
    tracked."""
    def setUp(self):
        super().setUp()
        self.tree = self.worktree('outputs')
        self.folder = self.tree / 'generated'
        for name, size in (('a', 3), ('sub/b', 5), ('sub/c', 7)):
            (self.folder / name).parent.mkdir(parents=True, exist_ok=True)
            (self.folder / name).write_bytes(b'x' * size)
        self.age(self.tree, 4 * 86400)

    def rows(self, apply, remove=False):
        request, proof = self.stage5_request(apply=apply)
        with mock.patch.object(disk_paths.placement, 'cleanup_processes', return_value=proof), \
                mock.patch.object(disk_paths.placement, 'physical_disk_path', return_value=str(self.root)):
            run = stage_run(request)
            rows = list(disk_paths.disposable_rows(run))
            if remove:
                remove(run, rows)
            return run, rows

    def test_apply_unit_list_equals_the_dry_run_unit_list(self):
        _, dry = self.rows(False)
        _, applied = self.rows(True)
        self.assertEqual([row['path'] for row, _ in dry], [str(self.folder)])
        self.assertEqual([row['path'] for row, _ in applied], [row['path'] for row, _ in dry])
        self.assertEqual([(row['files'], row['bytes']) for row, _ in applied], [(3, 15)])

    def test_an_ignored_folder_is_removed_as_one_unit_with_one_status_one_measure_one_prove(self):
        from drover.disk_evidence import StageEvidence
        measured, real_measure = [], StageEvidence.measure

        def measure(evidence, path, **options):
            measured.append(Path(path))
            return real_measure(evidence, path, **options)
        before = {}

        def remove(run, rows):
            before.update(statuses=StageEvidence.status.call_count, proves=git_worktrees.prove.call_count,
                          measures=measured.count(self.folder))
            for _, unit in rows:
                disk_removal.remove_disposable(run, unit)
        with mock.patch.object(StageEvidence, 'measure', measure):
            run, rows = self.rows(True, remove)
        statuses, proves, measures = before['statuses'], before['proves'], before['measures']
        self.assertEqual([(row['path'], row['action']) for row, _ in rows], [(str(self.folder), 'removed')])
        self.assertFalse(self.folder.exists())
        self.assertTrue(self.tree.is_dir())
        self.assertEqual(run.reclaimed_bytes, 15)
        self.assertEqual((StageEvidence.status.call_count - statuses, git_worktrees.prove.call_count - proves,
                          measured.count(self.folder) - measures), (1, 1, 1))


class FolderRemoval(unittest.TestCase):
    """`remove_disposable` on a folder unit: `w/bin`, as status reports it collapsed, holding three files. This test is
    the stage's evidence; `listed` is the fresh status's answer and `tracked_names` what ls-files says under it."""
    SIZES = (('a', 3), ('sub/b', 5), ('sub/c', 7))

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).absolute()
        self.tree = self.root / 'w'
        self.bin = self.tree / 'bin'
        (self.tree / '.git').parent.mkdir(parents=True)
        (self.tree / '.git').write_text('gitdir: x', encoding='utf-8')
        self.files = []
        for name, size in self.SIZES:
            path = self.bin / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'x' * size)
            self.files.append(path)
        self.order, self.statuses = [], []
        self.listed, self.tracked_names = ([], [str(self.bin)]), []
        self.entry = dict(worktree=str(self.tree), HEAD='head', branch='refs/heads/w')
        request = StageRequest(stage=5, side='windows', mode=None, apply=True, now=10 ** 10,
                               drive=dict(path=folder.name), repos=(dict(name='alpha', repo='/repo'),), policy={},
                               state_dir='/state', tasks={})
        self.real = StageEvidence(request, StageClock(1800), prove=self.prove,
                                  processes=lambda **_: SimpleNamespace(busy=lambda path: False, reason=None))
        self.run = SimpleNamespace(request=request, evidence=self, clock=SimpleNamespace(can_mutate=lambda: True),
                                   reclaimed=lambda size: self.order.append(('reclaimed', size)),
                                   checkpoint=abandoned, add=abandoned, stop=abandoned, error=abandoned)
        self.measured = {str(self.bin): self.real.measure(self.bin)}

    def row(self, repo, path):
        return dict(self.entry)

    def status(self, path, *, expand=True):
        self.statuses.append(expand)
        return copy.deepcopy(self.listed)

    def fresh_processes(self):
        return self.real.fresh_processes()

    def tracked(self, path, relative=None):
        return list(self.tracked_names)

    def measure(self, path, **options):
        self.order.append(('measure', Path(path)))
        return self.real.measure(path, **options)

    def fresh_registrations(self, repo, target, own, *, pointer):
        return self.real.fresh_registrations(repo, target, own, pointer=pointer)

    def fresh_snapshot(self):
        return None

    def prove(self, listings, repo, own, *, pointer, timeout_s):
        return Proof((Refusal(repo, own, 'changed', '.git'),) if (Path(own) / '.git').exists() is not pointer else (), ())

    def remove(self, *, after_last_rule=None, contains=lambda drive, path: True):
        row = dict(path=str(self.bin), action='candidate', reason_code='declared_disposable')
        unit = DisposableUnit(machine=dict(name='alpha', repo='/repo'), registration=dict(self.entry),
                              evidence=(self.tree, 'head', None, ('bin',)), touched=set(), target=self.bin,
                              measured=self.measured, row=row)

        def first(run, machine, registration, processes, *, touched=()):
            return Ruling('candidate', 'declared_disposable',
                          facts=dict(path=self.tree, head='head', tag=None, declarations=('bin',)))

        def last(run, unit, proof):
            ruling = disk_authority.rule_unit_fresh(run, unit, proof)
            if after_last_rule:
                after_last_rule()
            return ruling
        with mock.patch.object(disk_removal, 'rule_disposable_fresh', first), \
                mock.patch.object(disk_removal, 'rule_unit_fresh', last), \
                mock.patch.object(placement, 'cleanup_contains', contains):
            disk_removal.remove_disposable(self.run, unit)
        return row

    def left(self):
        return [path for path in self.files if path.exists()]

    def kept_whole(self, row, reason='output_evidence_unavailable'):
        self.assertEqual((row['action'], row['reason_code']), ('keep', reason))
        self.assertEqual(self.left(), self.files)
        self.assertNotIn('reclaimed', [step[0] for step in self.order])

    def test_the_folder_goes_whole_with_one_unexpanded_status_and_one_measure(self):
        row = self.remove()
        self.assertEqual((row['action'], self.bin.exists(), self.tree.is_dir()), ('removed', False, True))
        self.assertEqual(self.statuses, [False])
        self.assertEqual([step for step in self.order if step[0] == 'measure' and step[1] == self.bin],
                         [('measure', self.bin)])
        self.assertEqual(sorted(size for kind, size in self.order if kind == 'reclaimed'), [3, 5, 7])

    def test_a_folder_with_a_newly_tracked_file_inside_is_kept_whole(self):
        self.tracked_names = ['bin/sub/b']
        self.kept_whole(self.remove())

    def test_a_folder_no_longer_collapsed_in_status_is_kept_whole(self):
        for listed in (([], [str(path) for path in self.files]),        # git lists what is under it, one by one
                       ([str(self.bin)], []),                            # reported untracked, no longer ignored
                       ([], [])):
            with self.subTest(listed=listed):
                self.listed = listed
                self.kept_whole(self.remove())

    def test_a_link_inside_an_ignored_folder_keeps_the_folder(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'keep').write_bytes(b'precious')
        link = self.bin / 'sub' / 'link'
        link.symlink_to(outside, target_is_directory=True)
        self.kept_whole(self.remove())
        self.assertEqual((outside / 'keep').read_bytes(), b'precious')
        link.unlink()
        # A link made after the last rule: the removal never follows it, and nothing under the folder goes.
        self.order.clear()
        self.kept_whole(self.remove(after_last_rule=lambda: link.symlink_to(outside, target_is_directory=True)))
        self.assertEqual((outside / 'keep').read_bytes(), b'precious')

    def test_a_path_off_the_drive_inside_the_folder_keeps_the_folder(self):
        """As its own unit each path proved its drive; under a folder unit each path still does, before any goes."""
        elsewhere = self.bin / 'sub' / 'c'
        row = self.remove(contains=lambda drive, path: Path(path) != elsewhere)
        self.kept_whole(row)
        self.assertIn('left the drive', row['error'])

    def test_a_file_written_into_the_folder_after_its_last_rule_keeps_the_folder(self):
        late = self.bin / 'sub' / 'late'
        row = self.remove(after_last_rule=lambda: late.write_bytes(b'new work'))
        self.kept_whole(row)
        self.assertIn('folder changed', row['error'])
        self.assertTrue(late.exists())

    def test_cut_short_folder_removal_counts_bytes_as_each_path_goes(self):
        real_unlink, refused = Path.unlink, self.bin / 'sub' / 'b'

        def unlink(path, *args, **kwargs):
            if Path(path) == refused:
                raise PermissionError('in use')
            return real_unlink(path, *args, **kwargs)
        with mock.patch.object(Path, 'unlink', unlink):
            row = self.remove()
        gone = [path for path in self.files if not path.exists()]
        self.assertEqual((row['action'], row['reason_code']), ('keep', 'output_evidence_unavailable'))
        self.assertIn('in use', row['error'])
        self.assertIn(refused, self.left())
        self.assertTrue(gone)
        self.assertEqual(sorted(size for kind, size in self.order if kind == 'reclaimed'),
                         sorted(path_size for path, path_size in zip(self.files, (3, 5, 7)) if path in gone))


def abandoned(*_, **__):
    raise StageAbandoned('the stage lost its reader')


class Copying(unittest.TestCase):
    def test_add_slices_before_copying(self):
        """Only the rows `add` keeps are copied: a long `ignored` is cut to the limit first."""
        copied = []

        class Name(str):
            def __deepcopy__(self, memo):
                copied.append(self)
                return str(self)
        run = bare_stage_run(limit=2)
        names = [Name(f'w/bin/{n}') for n in range(5)]
        row = dict(path='w/bin', action='keep', reason_code='declared_disposable', ignored=names,
                   dirty=[Name(name) for name in names])
        run.add(row)
        self.assertEqual(len(copied), 4)
        self.assertEqual((len(row['ignored']), len(row['dirty'])), (5, 5))     # the caller's row is not changed


if __name__ == '__main__':
    unittest.main()
