import json

from tests.disk_fixture import DiskCase


class Progress(DiskCase):
    def lines(self):
        path = self.state / 'disk' / 'progress.log'
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]

    def test_each_stage_start_and_finish_is_logged(self):
        self.run_cleanup()
        lines = self.lines()
        starts = [(l['drive'], l['stage'], l['side']) for l in lines if l['event'] == 'start']
        finishes = [(l['drive'], l['stage'], l['side']) for l in lines if l['event'] == 'finish']
        expected = [('test-drive', stage, side) for stage in range(1, 7) for side in ('windows', 'wsl')]
        self.assertEqual(starts, expected)
        self.assertEqual(finishes, expected)
        for line in lines:
            if line['event'] == 'finish':
                self.assertIn('elapsed_s', line)
                self.assertIn('items', line)
        self.assertEqual(lines[-1]['event'], 'done')

    def test_state_records_the_current_stage_while_running_and_clears_after(self):
        from drover import disk_state
        seen = []

        def handler(side, request):
            seen.append(disk_state.read(self.state).get('progress'))
            return {'items': [], 'errors': [], 'reclaimed_bytes': 0}
        self.run_cleanup(handler=handler)
        self.assertEqual((seen[0]['drive'], seen[0]['stage'], seen[0]['side']), ('test-drive', 1, 'windows'))
        self.assertEqual(seen[3]['stage'], 2)
        self.assertIsNone(disk_state.read(self.state)['progress'])

    def test_failed_stage_is_logged_as_error(self):
        def handler(side, request):
            if request['stage'] == 2 and side == 'wsl':
                raise RuntimeError('boom')
            return {'items': [], 'errors': [], 'reclaimed_bytes': 0}
        self.run_cleanup(handler=handler)
        failed = [l for l in self.lines() if l['event'] == 'finish' and l['outcome'] == 'error']
        self.assertEqual([(l['stage'], l['side']) for l in failed], [(2, 'wsl')])
