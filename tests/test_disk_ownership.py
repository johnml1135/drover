"""Ownership lives outside tracked files and task acquisition renews its lease."""
import json
import os
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from drover import config, pipeline, repos
from tests.gitfix import Repo

NOW = 1_800_000_000.0


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

        fix.commit('.drover/config.toml', 'schema_version=1\n[scripts]\ntest=".drover/scripts/test.ps1"\n')
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
