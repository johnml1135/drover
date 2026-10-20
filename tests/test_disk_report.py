"""Excluded categories produce inventory rather than deletion authorization."""
import json
import os

from tests.test_disk_worktrees import WorktreeCase


class Report(WorktreeCase):
    def test_reports_every_nonautomatic_category_without_mutation(self):
        dirty = self.worktree('dirty inventory')
        self.r.commit('pending.txt', cwd=dirty)
        note = self.worktree('lock note', lock='review with owner')
        paths = [dirty / 'scratch.txt', dirty / '.venv' / 'keep',
                 dirty / 'data' / 'large', self.report_root / 'unknown' / 'keep',
                 self.report_root / 'model-v1' / 'weights',
                 self.report_root / 'model-v2' / 'weights',
                 self.report_root / 'loose.log', self.report_root / 'change.patch',
                 self.report_root / 'debug.dump', self.report_root / 'archive.zip']
        for index, path in enumerate(paths):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(('sentinel-' + str(index)).encode())
            os.utime(path, (self.now - 10 * 86400, self.now - 10 * 86400))
        self.cfg['disk_cleanup']['report_roots'] = [str(self.report_root)]
        before = {str(p): p.read_bytes() for p in paths}
        registrations = self.registrations()
        result = self.cleanup()
        for path in paths[:3] + paths[6:]:
            self.reported(result, path)
        for name in ('unknown', 'model-v1', 'model-v2'):
            self.reported(result, self.report_root / name)
        self.reported(result, note)
        inventory = [i for i in result['items'] if i['stage'] == 6]
        self.assertTrue(inventory)
        for item in inventory:
            self.assertTrue({'owner', 'files', 'age_s', 'bytes', 'reason_code'} <= item.keys())
            self.assertGreaterEqual(item['files'], 0)
            self.assertGreaterEqual(item['bytes'], 0)
        self.assertEqual({str(p): p.read_bytes() for p in paths}, before)
        self.assertEqual(self.registrations(), registrations)
        self.assertEqual(result.get('applied'), False)
