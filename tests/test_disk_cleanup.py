from tests.disk_fixture import DiskCase, empty_result


class Modes(DiskCase):
    def test_warn_stops_next_stage(self):
        def stage(side, request):
            if side == 'wsl':
                self.free = {1: 120., 2: 150.}.get(request['stage'], self.free)
            return empty_result()
        self.approve_report()
        self.calls.clear()
        result = self.run_cleanup(apply=True, real=False, handler=stage)
        stages = [request['stage'] for side, request in self.calls]
        self.assertEqual(sorted(set(stages)), [1, 2])
        self.assertEqual(stages, sorted(stages))
        self.assertFalse(result['exhausted'])
        self.assertTrue(result['applied'])

    def test_daily_only_one_through_four(self):
        for free in (80., 180.):
            with self.subTest(free=free):
                self.free = free
                self.approve_report()
                self.calls.clear()
                result = self.run_cleanup(mode='daily', apply=True, real=False)
                stages = [request['stage'] for side, request in self.calls]
                self.assertEqual(sorted(set(stages)), [1, 2, 3, 4])
                self.assertEqual(stages, sorted(stages))
                self.assertEqual(result['mode'], 'daily')

    def test_dry_run_has_no_cleanup_mutations(self):
        old = self.file(self.temp_root / 'old item')
        result = self.run_cleanup()
        self.assertTrue(old.exists())
        self.assertFalse(result['applied'])
        self.assertTrue(all(not request['apply'] for side, request in self.calls))
        self.assertFalse((self.state / 'disk-cleanup-manifest.jsonl').exists())
        self.assertFalse((self.state / 'disk-cleanup.lock').exists())
        self.assertTrue(all(item.get('action') != 'removed' for item in result['items']))
        self.assertTrue(all(not item.get('reclaimed_bytes', 0) for item in result['items']))

    def test_invalid_machine_repo_is_reported_without_authorizing_its_paths(self):
        bad = self.repos_dir / 'unreadable.toml'
        bad.write_text('invalid machine bytes')
        result = self.run_cleanup()
        self.assertTrue(any(str(bad) == error.get('path') for error in result['errors']))
