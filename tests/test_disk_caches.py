import os
import sys
import tempfile
from pathlib import Path

from drover import process
from tests.disk_fixture import DiskCase
from tests.fakeci import FakeCommands, process_options


class Caches(DiskCase):
    def commands(self):
        """Two declared cache commands, the first failing: argv that a shell would mangle, run from the temp root."""
        suffix = ['space value', "quote'\"", 'semi;colon', 'line\nbreak', '$(literal)']
        self.cfg['disk_cleanup']['caches'] = [
            dict(argv=[sys.executable, str(self.root / 'cache recorder.py'), code, *suffix],
                 cwd=str(self.temp_root), placement=self.side)
            for code in ('7', '0')]
        return [entry['argv'] for entry in self.cfg['disk_cleanup']['caches']]

    def test_declared_argv_verbatim_and_failure_continues(self):
        commands = self.commands()
        self.approve_report()
        fake = FakeCommands(self.root / 'commands')
        for argv, code in zip(commands, (7, 0)):     # verbatim, from cwd, with the hub's own environment (env=None)
            fake.expect(argv, returncode=code, **process_options(120, cwd=str(self.temp_root)))
        with fake.patch(process):
            result = self.run_cleanup(apply=True)
        self.assertEqual([(e['argv'], e['returncode']) for e in result['errors'] if e.get('stage') == 3],
                         [(commands[0], 7)])
        self.assertIn(4, [request['stage'] for side, request in self.calls])

    def test_dry_run_executes_no_commands(self):
        self.commands()
        with FakeCommands(self.root / 'commands').patch(process):     # no expectation: any spawn fails the test
            result = self.run_cleanup()
        self.assertTrue(any(item['stage'] == 3 for item in result['items']))
        self.assertFalse(result['applied'])

    def test_r8_folder_routine_age_and_aggressive_lru_stop(self):
        folder = self.root / 'folder cache'
        old = self.file(folder / 'old', age=31 * 86400)
        young = self.file(folder / 'young', age=2 * 86400)
        self.cfg['disk_cleanup']['caches'] = [
            dict(path=str(folder), placement=self.side, max_age_days=30)]
        self.approve_report()
        self.run_cleanup(apply=True, mode='daily')
        self.assertFalse(old.exists())
        self.assertTrue(young.exists())
        oldest = self.file(folder / 'oldest', age=5 * 86400)
        newer = self.file(folder / 'newer', age=86400)
        self.approve_report()
        runtime = self.runtime(real=True)
        from drover import disk_cleanup, placement
        from unittest.mock import patch
        from types import SimpleNamespace
        def read(path):
            return 80 if oldest.exists() else 150
        from drover.disk_cleanup import CleanupRuntime
        with patch.object(placement, 'cleanup_processes', return_value=SimpleNamespace(busy=self.busy)),                 patch.object(placement, 'physical_disk_path', return_value=str(self.root)),                 patch.object(placement, 'cleanup_free', side_effect=read), \
                patch('tempfile.gettempdir', return_value=str(self.temp_root)):
            result = disk_cleanup.run(self.cfg, apply=True,
                runtime=CleanupRuntime(runtime.now, read, runtime.call, runtime.toast))
        self.assertFalse(oldest.exists())
        self.assertTrue(young.exists())
        self.assertTrue(newer.exists())
        self.assertFalse(result['exhausted'])

    def test_r8_folder_atime_and_mtime_fallback(self):
        folder = self.root / 'cache clocks'
        fallback = self.file(folder / 'fallback', age=31 * 86400)
        used = self.file(folder / 'used', age=40 * 86400)
        os.utime(used, (self.now - 86400, self.now - 40 * 86400))
        self.cfg['disk_cleanup']['caches'] = [dict(path=str(folder), placement=self.side)]
        self.approve_report()
        self.run_cleanup(apply=True, mode='daily')
        self.assertFalse(fallback.exists())
        self.assertTrue(used.exists())

    def test_folder_cache_cannot_delete_repository_or_state(self):
        repo = self.repository() / 'repo'
        source = repo / 'source.txt'
        source.write_text('valuable')
        secret = self.file(self.state / 'private')
        self.cfg['disk_cleanup']['caches'] = [dict(path=str(p), placement=self.side)
                                             for p in (repo, self.state)]
        from drover import placement
        from drover.disk_side import serve
        from types import SimpleNamespace
        from unittest.mock import patch
        request = dict(stage=3, mode='triggered', apply=True, now=self.now, repos=[], tasks={},
                       state_dir=str(self.state), paths=self.cfg['disk_cleanup'],
                       drive=dict(path=str(self.root), level='queue', warn_gb=150))
        with patch.object(placement, 'physical_disk_path', return_value=str(self.root)), \
                patch.object(placement, 'cleanup_free', return_value=80), \
                patch.object(placement, 'cleanup_processes', return_value=SimpleNamespace(busy=lambda p: False)):
            result = serve(request)
        self.assertEqual(source.read_text(), 'valuable')
        self.assertTrue(secret.exists())
        self.assertTrue(any(i['reason_code'] == 'protected' for i in result['items']))

    def test_config_rejects_folder_cache_overlapping_configured_repo(self):
        from drover import config, disk_preflight
        root = self.repository()
        (self.repos_dir / 'repo.toml').write_text(config.dumps(dict(
            name='repo', repo=str(root / 'repo'), worktrees=str(root / 'trees'))))
        for folder in (root / 'repo' / 'cache', root, self.state / 'cache', root / 'trees'):
            with self.subTest(folder=folder):
                self.cfg['disk_cleanup']['caches'] = [dict(path=str(folder), placement=self.side)]
                with self.assertRaisesRegex(config.ConfigError, 'overlap|protected'):
                    disk_preflight.check_cache_folders(config.validate(self.cfg))

    def test_cache_validation_checks_default_worktree_root_without_loading_config(self):
        from drover import config, disk_preflight
        from unittest.mock import patch
        repo = self.repository() / 'repo'
        (self.repos_dir / 'repo.toml').write_text(config.dumps(dict(name='repo', repo=str(repo))))
        self.cfg['paths']['worktrees'] = ''
        self.cfg['disk_cleanup']['caches'] = [dict(path=str(repo) + '.worktrees/cache', placement=self.side)]
        with patch.object(config, 'get', side_effect=AssertionError('validation loaded global config')):
            with self.assertRaisesRegex(config.ConfigError, 'overlap|protected'):
                disk_preflight.check_cache_folders(config.validate(self.cfg))

    def repository(self):
        """A root outside the cleanup tree holding `repo`, a checkout as the protection rules see one: a `.git`
        directory. Real git is setup here, not the point."""
        temporary = tempfile.TemporaryDirectory(prefix='disk-cache-repo-')
        self.addCleanup(temporary.cleanup)
        (Path(temporary.name) / 'repo' / '.git').mkdir(parents=True)
        return Path(temporary.name)
