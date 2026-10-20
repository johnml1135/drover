from unittest.mock import patch
from tests.disk_fixture import DiskCase


class Reading(DiskCase):
    def test_real_drive_never_vhdx_free_space(self):
        from drover import disk, placement
        seen = []
        # Explicit physical drives need no VHDX/home free-space fallback or WSL probe.
        self.cfg['trackers'][0]['path'] = 'H:\\'
        self.cfg['trackers'].append(dict(self.cfg['trackers'][0], name='disabled', enabled=False))
        with patch('subprocess.run', side_effect=AssertionError('reader must not start a process')):
            rows = disk.read(self.cfg, reader=lambda path: seen.append(path) or 151.)
        physical = placement.native('H:\\')
        self.assertEqual(seen, [physical])
        self.assertEqual(rows, [dict(name='test-drive', path=physical, free_gb=151., level='ok')])
        self.assertFalse(any('vhdx' in str(p).lower() or p == str(self.root.home()) for p in seen))

    def test_level_actions_and_exact_edges(self):
        from drover import disk
        for levels in ((150, 100, 50, 25), (60, 40, 25, 15)):
            self.cfg['trackers'][0].update(zip(('warn_gb', 'queue_gb', 'hold_gb', 'critical_gb'), levels))
            warn, queue, hold, critical = levels
            cases = [(warn, 'ok', 'go', False), (warn-.01, 'warn', 'go', False),
                     (queue, 'warn', 'go', False), (queue-.01, 'queue', 'queue', False),
                     (hold, 'queue', 'queue', False), (hold-.01, 'hold', 'hold', False),
                     (critical, 'hold', 'hold', False), (critical-.01, 'critical', 'hold', True)]
            for value, level, verdict, pause in cases:
                with self.subTest(levels=levels, value=value):
                    rows = disk.read(self.cfg, reader=lambda path: value)
                    self.assertEqual(rows[0]['level'], level)
                    result = disk.admission(rows)
                    self.assertEqual((result['verdict'], result['pause']), (verdict, pause))
            result = disk.admission([dict(level='queue', name='a'), dict(level='critical', name='b')])
            self.assertEqual((result['verdict'], result['pause']), ('hold', True))

    def test_unknown_never_blocks(self):
        from drover import disk
        for value in (None, float('nan'), float('inf'), -1, True, '100'):
            with self.subTest(value=value):
                rows = disk.read(self.cfg, reader=lambda path: value)
                self.assertEqual(rows[0]['level'], 'unknown')
                self.assertIsNone(rows[0]['free_gb'])
                self.assertEqual(disk.admission(rows)['verdict'], 'go')
                self.assertFalse(disk.admission(rows)['pause'])
