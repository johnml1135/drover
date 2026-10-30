"""Ownership lives outside tracked files and task acquisition renews its lease."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from drover import config, pipeline, repos
from tests.gitfix import Repo

NOW = 1_800_000_000.0


class NativeMetadata(unittest.TestCase):
    def setUp(self):
        from drover import placement_git as placement
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.admin = Path(self.tmp.name) / 'admin with spaces'
        self.admin.mkdir()
        self.common = self.admin.parent / 'common'
        self.responses = []

        def git(worktree, *args, **kwargs):
            self.responses.append(args)
            paths = [self.admin if arg == '--absolute-git-dir' else self.common
                     for arg in args if arg in ('--absolute-git-dir', '--git-common-dir')]
            return SimpleNamespace(returncode=0, stdout=''.join(str(p) + '\n' for p in paths))

        for context in (patch.object(placement, 'git', side_effect=git),
                        patch.object(placement, 'routes_through_wsl', return_value=False),
                        patch.object(placement.subprocess, 'run', side_effect=AssertionError('native metadata spawned a process'))):
            context.start()
            self.addCleanup(context.stop)

    def test_native_metadata_uses_one_git_query_and_no_python_children(self):
        from drover import placement
        text = 'literal "quotes", $shell, newline\n雪'
        self.assertIsNone(placement.worktree_metadata('worktree', 'drover-tag.json'))
        self.assertEqual(placement.worktree_metadata('worktree', 'drover-tag.json', text), text)
        self.assertEqual(placement.worktree_metadata('worktree', 'drover-tag.json'), text)
        self.assertEqual((self.admin / 'drover-tag.json').read_text(encoding='utf-8'), text)
        self.assertEqual(len(self.responses), 3, 'one discovery query per operation; never cache ownership')

    def test_native_failed_replace_preserves_previous_record_and_removes_temp_file(self):
        from drover import placement
        target = self.admin / 'drover-tag.json'
        target.write_text('previous', encoding='utf-8')

        def fail_replace(source, destination):
            self.assertEqual(Path(source).parent, self.admin)
            self.assertEqual(Path(source).read_text(encoding='utf-8'), 'replacement')
            self.assertEqual(Path(destination), target)
            raise OSError('fixture replace failure')

        with patch.object(os, 'replace', side_effect=fail_replace), self.assertRaisesRegex(OSError, 'replace failure'):
            placement.worktree_metadata('worktree', target.name, 'replacement')
        self.assertEqual(target.read_text(encoding='utf-8'), 'previous')
        self.assertEqual(list(self.admin.iterdir()), [target])

    def test_common_directory_is_never_written(self):
        from drover import placement
        self.common = self.admin
        self.assertIsNone(placement.worktree_metadata('main checkout', 'drover-tag.json'))
        with self.assertRaisesRegex(OSError, 'linked worktree'):
            placement.worktree_metadata('main checkout', 'drover-tag.json', 'forbidden')
        self.assertEqual(list(self.admin.iterdir()), [])


class Ownership(unittest.TestCase):
    def test_tag_lease_and_task_creation(self):
        from drover import ownership

        fix = Repo()
        self.addCleanup(fix.cleanup)
        self.addCleanup(config.reset)
        config.use(config.validate({'paths': {'state_dir': str(fix.root / 'state')}}))
        tree = fix.root / 'trees with spaces' / 'linked'
        fix.git('worktree', 'add', '-q', '-b', 'linked', tree)
        admin = Path(fix.git('rev-parse', '--absolute-git-dir', cwd=tree))
        # Relative admin pointers are common after cross-side worktree creation.
        (tree / '.git').chmod(0o666)
        (tree / '.git').unlink()
        (tree / '.git').write_text('gitdir: ' + os.path.relpath(admin, tree) + '\n')
        record = ownership.tag(str(tree), 'alpha', 'session:literal', NOW + 86400, now=NOW)
        self.assertEqual(record, {'v': 2, 'repo': 'alpha', 'owner': 'session:literal',
                                  'created': NOW, 'lease_until': NOW + 86400})
        self.assertEqual(ownership.read(str(tree)), record)
        self.assertTrue(ownership.owned_by(str(tree), 'session:literal'))
        self.assertFalse(ownership.owned_by(str(tree), 'drover:other'))
        admin = Path(fix.git('rev-parse', '--absolute-git-dir', cwd=tree))
        self.assertEqual(json.loads((admin / 'drover-tag.json').read_text()), record)
        self.assertFalse((tree / 'drover-tag.json').exists())
        self.assertEqual(fix.git('status', '--porcelain', '--untracked-files=all', cwd=tree), '')
        for owner, lease, now in (('', NOW + 1, NOW), ('x', float('nan'), NOW),
                                   ('x', float('inf'), NOW), ('x', NOW + 1, float('nan'))):
            with self.subTest(owner=owner, lease=lease, now=now):
                with self.assertRaises(ValueError):
                    ownership.tag(str(tree), 'alpha', owner, lease, now=now)
                self.assertEqual(ownership.read(str(tree)), record)
        legacy = {'v': 1, 'repo': 'alpha', 'owner': 'lane:old', 'created': NOW - 100}
        (admin / 'drover-tag.json').write_text(json.dumps(legacy))
        read = ownership.read(str(tree))
        self.assertEqual(read['owner'], 'lane:old')
        self.assertFalse(read.get('lease_until'))
        (admin / 'drover-tag.json').write_text(json.dumps({**record, 'v': 999}))
        self.assertIsNone(ownership.read(str(tree)))
        (admin / 'drover-tag.json').write_text('{broken')
        self.assertIsNone(ownership.read(str(tree)))
        self.assertFalse(ownership.owned_by(str(tree), 'lane:old'))

        fix.commit('.drover/config.toml', 'schema_version=1\n[scripts]\ntest=".drover/scripts/test.ps1"\n[gate]\nplatform="wsl"\n')
        fix.git('push', '-q', 'origin', 'HEAD:main')
        task = {'id': 't-lease', 'title': 'lease acquisition', 'repo': 'alpha'}
        waiting = SimpleNamespace(state=lambda cause: None, answer=lambda cause: None)
        port = SimpleNamespace(get_task=lambda tid: dict(task), waiting=waiting,
                               update_task=lambda tid, **values: task.update(values),
                               append_event=lambda *args, **kwargs: None)
        prog = repos.load({'name': 'alpha', 'repo': str(fix.repo), 'worktrees': str(fix.root / 'task trees')})
        pipe = pipeline.Pipeline(port, repos_map={'alpha': prog}, runs_dir=fix.root / 'state' / 'runs')
        with patch.object(time, 'time', return_value=NOW):
            acquired = pipe.worktree(task)
        tag = ownership.read(acquired)
        self.assertEqual((tag['v'], tag['repo'], tag['owner'], tag['lease_until']),
                         (2, 'alpha', 'drover:t-lease', NOW + 86400))
        ownership.tag(acquired, 'alpha', 'drover:t-lease', NOW - 1, now=NOW - 86401)
        (Path(acquired) / 'base.txt').write_text('unfinished task work\n')
        with patch.object(time, 'time', return_value=NOW + 100):
            self.assertEqual(pipe.worktree(task), acquired)
        self.assertEqual(ownership.read(acquired)['lease_until'], NOW + 86500)
        self.assertEqual((Path(acquired) / 'base.txt').read_text(), 'unfinished task work\n')

    def test_metadata_io_crosses_placement_and_preserves_literal_content(self):
        from drover import ownership, placement
        fix = Repo()
        self.addCleanup(fix.cleanup)
        tree = fix.root / 'trees with spaces' / 'metadata'
        fix.git('worktree', 'add', '-q', '-b', 'metadata', tree)
        # Exercise the composed remote worker locally; no WSL process is started.
        # Only the side transport is replaced, so argv, stdin and atomic IO run.
        calls = []
        def local_transport(adapter, argv, **kwargs):
            calls.append(list(argv))
            local = list(argv)
            if argv[0] == 'git':
                local[local.index('-C') + 1] = placement.native(local[local.index('-C') + 1])
            elif argv[0:2] == ['python3', '-c']:
                local[0] = sys.executable
                local[3] = placement.native(local[3])  # worker directory; payload stays literal
            return local
        with patch.object(placement, 'routes_through_wsl', return_value=True) as _placement_patch_1, patch('drover.placement_git.routes_through_wsl', _placement_patch_1), patch('drover.placement_paths.routes_through_wsl', _placement_patch_1), \
                patch.object(placement.WSL, 'command', new=local_transport):
            record = ownership.tag(str(tree), 'alpha', 'session: "quote"\n雪', NOW + 1, now=NOW)
            self.assertEqual(ownership.read(str(tree)), record)
        workers = [call for call in calls if call[0] != 'git']
        self.assertEqual(len(workers), 3)
        self.assertTrue(all(call[0:2] == ['python3', '-c'] for call in workers))
        self.assertEqual(fix.git('status', '--porcelain', cwd=tree), '')
        self.assertIsNone(ownership.read(str(fix.repo)))
        with self.assertRaises(OSError):
            ownership.tag(str(fix.repo), 'alpha', 'session:x', NOW + 1, now=NOW)
        self.assertFalse((fix.repo / '.git' / 'drover-tag.json').exists())

    def test_clean_existing_worktree_acquisition_records_task_lease(self):
        from drover import ownership
        fix = Repo()
        self.addCleanup(fix.cleanup)
        self.addCleanup(config.reset)
        config.use(config.validate({'paths': {'state_dir': str(fix.root / 'state')}}))
        tree = fix.root / 'existing tree'
        fix.git('worktree', 'add', '-q', '-b', 'existing', tree)
        task = {'id': 't-adopt', 'repo': 'alpha', 'title': 'adopt clean worktree',
                'branch': 'existing', 'worktree': str(tree)}
        port = SimpleNamespace(get_task=lambda tid: dict(task),
                               waiting=SimpleNamespace(answer=lambda cause: None),
                               update_task=lambda tid, **values: task.update(values),
                               append_event=lambda *args, **kwargs: None)
        prog = repos.load({'name': 'alpha', 'repo': str(fix.repo), 'worktrees': str(fix.root / 'task trees'), 'gate': {'platform': 'wsl'}})
        pipe = pipeline.Pipeline(port, repos_map={'alpha': prog}, runs_dir=fix.root / 'state' / 'runs')
        with patch.object(time, 'time', return_value=NOW):
            self.assertEqual(pipe.worktree(task), str(tree))
        self.assertEqual(ownership.read(str(tree)),
                         {'v': 2, 'repo': 'alpha', 'owner': 'drover:t-adopt',
                          'created': NOW, 'lease_until': NOW + 86400})
