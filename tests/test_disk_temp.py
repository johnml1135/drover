from tests.disk_fixture import DiskCase, DAY


class Temporary(DiskCase):
    def test_seven_days_newest_descendant_and_busy(self):
        boundary = self.file(self.temp_root / 'seven days exactly', age=7 * DAY)
        young = self.file(self.temp_root / 'young', age=7 * DAY - 1)
        busy = self.file(self.temp_root / 'busy old')
        mixed = self.file(self.temp_root / 'mixed' / 'old')
        recent_child = self.file(mixed.parent / 'recent', age=DAY)
        self.age(mixed.parent, 8 * DAY)
        self.age(recent_child, DAY)
        self.busy_paths.add(str(busy))
        self.approve_report()
        result = self.run_cleanup(apply=True)
        self.assertFalse(boundary.exists())
        for path in (young, busy, recent_child):
            self.assertTrue(path.exists(), str(path))
        self.assertTrue(any(item['stage'] == 2 and item['path'] == str(boundary)
                            for item in result['items']))

    def test_declared_roots_and_links(self):
        declared = self.file(self.temp_root / 'old declared')
        undeclared = self.file(self.root / 'undeclared' / 'old')
        target = self.file(self.root / 'target' / 'valuable')
        link = self.temp_root / 'linked tree'
        link.symlink_to(target.parent, target_is_directory=True)
        sibling = self.file(self.root / (self.temp_root.name + '-sibling') / 'old')
        self.approve_report()
        self.run_cleanup(apply=True)
        self.assertFalse(declared.exists())
        for path in (undeclared, target, sibling):
            self.assertTrue(path.exists(), str(path))
        self.assertTrue(link.is_symlink())
        self.assertTrue(self.temp_root.is_dir())

    def test_exact_scratch_scope_preserves_ledger_runs_and_state(self):
        scratch = self.file(self.state / 'scratch' / 'old payload')
        protected = [self.file(self.state / name, 'protected bytes') for name in
                     ('ledger.db', 'ledger.db-wal', 'ledger.db-shm', 'runs/old/log',
                      'runs/active/log', 'repos/keep.toml', 'config.toml', 'policy.json',
                      'disk/sentinel')]
        protected.append(self.file(self.state / 'disk-cleanup-manifest.jsonl', ''))
        before = {path: path.read_bytes() for path in protected}
        self.cfg['disk_cleanup']['temp_roots'] += [str(self.state), str(self.root)]
        self.approve_report()
        self.assertTrue(scratch.exists())
        self.run_cleanup(apply=True)
        self.assertFalse(scratch.exists())
        self.assertEqual({path: path.read_bytes() for path in protected}, before)
        self.cfg['disk_cleanup']['temp_roots'] = [str(self.state)]
        outside = self.file(self.root / 'outside' / 'payload')
        (self.state / 'scratch').rmdir()
        (self.state / 'scratch').symlink_to(outside.parent, target_is_directory=True)
        self.approve_report()
        self.run_cleanup(apply=True)
        self.assertEqual(outside.read_text(), 'sentinel')
