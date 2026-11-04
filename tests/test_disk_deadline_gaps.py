"""Removal guards at the stage level: a cache file off the drive, and a clock whose `can_mutate` refuses while `late()`
is still false (too little time left), so the stage's own stop and the removal's refusal must agree."""
import itertools
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from drover import disk_paths, disk_removal, disk_temp, placement
from drover.disk_stage import StageClock
from tests.disk_fixture import answer, stage_run

NOW = 10 ** 10      # every file is older than any age the stages ask for


class Refusing(StageClock):
    """A clock at 0 that is never late yet cannot finish a removal, as when too little of the worker timeout is left."""
    def __init__(self, pattern=(False,)):
        super().__init__(100, clock=lambda: 0.0)
        self.pattern = itertools.cycle(pattern)

    def can_mutate(self):
        return next(self.pattern)


class Stage(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).absolute()

    def files(self, *names):
        made = []
        for name in names:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'x' * 4)
            made.append(path)
        return made

    def run_stage(self, stage, clock, **paths):
        state = self.root / 'state'
        state.mkdir()
        run = stage_run(dict(stage=2, apply=True, now=NOW, drive=dict(path=str(self.root)), repos=[], paths=paths,
                             state_dir=str(state)))
        run.clock = clock
        with patch.object(placement, 'cleanup_processes', return_value=SimpleNamespace(busy=lambda path: False, reason=None)), \
                patch.object(placement, 'physical_disk_path', return_value=str(self.root)):
            stage(run)
        return answer(run)

    def reasons(self, out):
        return [(row['reason_code'], row['action']) for row in out['items'] if row['reason_code'] != 'temp_root_elapsed']


class CacheFileDrive(Stage):
    def removal(self, name):
        """The drive is `root/drive`; `name` is where the cache file lies: under it, or beside it."""
        [path] = self.files(name)
        row = dict(path=str(path), action='candidate')
        run = stage_run(dict(apply=True, now=NOW, drive=dict(path=str(self.root / 'drive'))))
        home = str(self.root / 'drive')
        with patch.object(placement, 'physical_disk_path', lambda p: 'on' if str(p).startswith(home) else 'off'):
            disk_removal.remove_cache_file(run, row, path, path.stat())
        return path, row, run

    def test_a_cache_file_off_the_stage_drive_is_kept(self):
        path, row, run = self.removal('beside/cache/old')
        self.assertTrue(path.exists())
        self.assertEqual((row['action'], run.reclaimed_bytes), ('candidate', 0))

    def test_a_cache_file_on_the_stage_drive_is_removed(self):
        path, row, run = self.removal('drive/cache/old')
        self.assertFalse(path.exists())
        self.assertEqual((row['action'], run.reclaimed_bytes), ('removed', 4))


class RefusedBeforeTheLine(Stage):
    def test_a_stale_temp_entry_the_clock_refuses_stops_the_stage_with_removal_deadline(self):
        old = self.files('temp/one', 'temp/two')
        out = self.run_stage(disk_temp.temporary, Refusing(), temp_roots=[str(self.root / 'temp')])
        self.assertEqual(self.reasons(out), [('removal_deadline', 'keep'), ('temp_deadline', 'report')])
        self.assertEqual([path.exists() for path in old], [True, True])

    def test_a_folder_cache_removal_the_clock_refuses_stops_the_stage_with_removal_deadline(self):
        old = self.files('cache/one', 'cache/two')
        out = self.run_stage(disk_paths.caches, Refusing([True, False]), caches=[dict(path=str(self.root / 'cache'))])
        self.assertEqual(self.reasons(out), [('removal_deadline', 'keep'), ('cache_deadline', 'report')])
        self.assertEqual([path.exists() for path in old], [True, True])


if __name__ == '__main__':
    unittest.main()
