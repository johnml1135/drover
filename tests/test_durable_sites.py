"""Each writer moved to durable keeps the sync, mode and encoding it had, and gains the wait section 5.4 gives it."""
import contextlib
import io
import os
from unittest.mock import patch

from drover import allowance, config, disk_cleanup, durable, handoff, placement_git, wizard_files
from drover.durable import ORDINARY
from tests.test_durable import Base, Flaky, refused_site


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
             ['chmod'], b'{"plan": "claude", "remaining_pct": 50.0, "set_at": 1.0}'),
            ('handoff.md', lambda: handoff.write(self.dir / 'handoff.md', 'a\n'), ['chmod'], b'a\n'),
            ('w.toml', lambda: wizard_files.write_file(self.dir, self.dir / 'w.toml', 'a\n', lambda: 0), ['chmod'], b'a\n'),
            ('config.toml', migrate, ['fsync', 'fsync'], config.dumps(dict(a=1)).replace('\n', os.linesep).encode()),
            ('tag.json', lambda: placement_git._native_metadata_io(self.dir, 'tag.json', 'a\n'), ['fsync'], native),
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
