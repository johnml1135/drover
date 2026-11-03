"""Each writer moved to durable keeps the sync, mode and encoding it had, and gains the wait section 5.4 gives it."""
import ast
import contextlib
import io
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from drover import allowance, config, disk_cleanup, durable, handoff, placement_git, wizard_files
from drover.durable import ORDINARY
from tests.test_durable import Base, Flaky, refused_site

DROVER = Path(__file__).resolve().parent.parent / 'drover'
# Section 5.4's ordinary run files: no fsync and the ordinary wait, in source order by enclosing function. The three
# runner_supervise exceptions are the table's own rows: Status.routine, Status.terminal and record.json.
RUN_FILE_WRITES = {
    'ci': [(name, 'ORDINARY') for name in ('prepare', 'stop_active', 'defer', 'run', 'run', 'run', 'run', '_supervise',
                                           '_classify', 'refuse', '_execute')],
    'dispatcher_finish': [('_continue_worker', 'ORDINARY')],
    'landing_batch': [('build_tip', 'ORDINARY')],
    'runner': [('detach', 'ORDINARY')],
    'runner_supervise': [('set', 'ORDINARY'), ('routine', 'ROUTINE'), ('terminal', 'TERMINAL'),
                         ('request_stop', 'ORDINARY'), ('request_stop', 'ORDINARY'), ('_supervise', 'TERMINAL')],
}


def durable_writes(module):
    """(enclosing function, wait) for each `durable.*` call in `module`; fails on any sync other than a literal False."""
    found = []

    def visit(node, function):
        for child in ast.iter_child_nodes(node):
            inner = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else function
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) \
                    and isinstance(child.func.value, ast.Name) and child.func.value.id == 'durable':
                given = {keyword.arg: ast.unparse(keyword.value) for keyword in child.keywords}
                if child.func.attr != 'write_json' or given.get('sync') != 'False':
                    raise AssertionError(f'{module}:{child.lineno} {ast.unparse(child)[:80]}: not an unsynced write_json')
                found.append((inner, given.get('wait', 'durable.ORDINARY').removeprefix('durable.')))
            visit(child, inner)
    visit(ast.parse((DROVER / f'{module}.py').read_text(encoding='utf-8')), None)
    return found


class RunFileWaits(unittest.TestCase):
    def test_every_ordinary_run_file_write_is_unsynced_with_the_ordinary_wait(self):
        """A NO_WAIT here loses a run file to a brief Windows reader; a sync adds an fsync on /mnt/c to every write."""
        for module, expected in RUN_FILE_WRITES.items():
            with self.subTest(module):
                self.assertEqual(durable_writes(module), expected)


class Sites(Base):
    def test_each_writer_keeps_its_sync_wait_and_mode(self):
        (self.dir / 'config.toml').write_text('old = 1\n', encoding='utf-8')
        native = 'a\n'.replace('\n', os.linesep).encode()

        def migrate():
            with patch.object(config, 'home', return_value=self.dir), contextlib.redirect_stdout(io.StringIO()), \
                    patch.object(config, 'migrate_disk_config', return_value=dict(a=1)):
                config._migrate_disks(True)

        rows = (
            ('manual-claude.json', lambda: allowance.set_manual('claude', 50, state=str(self.dir), now=1.0),
             ['temp 666'], b'{"plan": "claude", "remaining_pct": 50.0, "set_at": 1.0}'),
            ('handoff.md', lambda: handoff.write(self.dir / 'handoff.md', 'a\n'), ['temp 666'], b'a\n'),
            ('w.toml', lambda: wizard_files.write_file(self.dir, self.dir / 'w.toml', 'a\n', lambda: 0), ['temp 666'], b'a\n'),
            ('config.toml', migrate, ['fsync', 'temp 600', 'fsync'], config.dumps(dict(a=1)).replace('\n', os.linesep).encode()),
            ('tag.json', lambda: placement_git._native_metadata_io(self.dir, 'tag.json', 'a\n'), ['temp 600', 'fsync'], native),
        )
        for name, write, steps, content in rows:
            with self.subTest(name):
                done, offered, elapsed = refused_site(write, name)
                self.assertEqual((done, offered), (steps, content))
                self.assertAlmostEqual(elapsed, ORDINARY.seconds, delta=ORDINARY.step_s * 1.5)
                self.assertEqual(self.temps(), [])

    def test_the_handoff_and_progress_moves_are_one_attempt(self):
        log = self.dir / 'disk' / 'progress.log'
        log.parent.mkdir()
        log.write_text('old\n', encoding='utf-8')
        old, new = self.dir / 'handoff-1.md', self.dir / 'handoff-2.md'
        old.write_text('kept', encoding='utf-8')
        follow = lambda: handoff.follow(None, 'role', 1, 2)
        for move, stayed in ((follow, old), (lambda: disk_cleanup._Progress(self.dir)._log(event='x'), log)):
            refused = Flaky(blocked=lambda: True)
            with patch.object(handoff, 'path_for', side_effect=[old, new]), patch.object(handoff, 'valid', return_value=True), \
                    patch.object(disk_cleanup._Progress, 'LIMIT', 0), patch.object(durable.os, 'replace', refused), \
                    patch.object(durable, 'move', wraps=durable.move) as moved:
                self.assertIn(move(), (False, None))
            moved.assert_called_once()
            self.assertEqual((refused.calls, self.clock.slept), (1, []))
            self.assertTrue(stayed.exists())
