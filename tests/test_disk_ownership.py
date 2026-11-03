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

from drover import config, pipeline, process, repos
from tests.fakeci import git_answers, git_spy

NOW = 1_800_000_000.0
METADATA = ('rev-parse', '--path-format=absolute', '--absolute-git-dir', '--git-common-dir')


class Trees:
    """A main checkout and its linked worktrees as git keeps them on disk: a `.git` file in each tree pointing at its
    admin directory. It answers the one query ownership makes, and stands in for the pipeline's worktree facts. Git is
    setup here, not the point: what is tested is where the tag lives and how its lease is renewed."""
    def __init__(self, root):
        self.root, self.repo = root, root / 'repo'
        self.common = self.repo / '.git'
        (self.common / 'worktrees').mkdir(parents=True)
        self.branches = {}

    def add(self, tree, branch):
        tree = Path(tree)
        admin = self.common / 'worktrees' / tree.name
        admin.mkdir()
        tree.mkdir(parents=True, exist_ok=True)
        (tree / '.git').write_text(f'gitdir: {admin}\n', encoding='utf-8')
        self.branches[os.path.normcase(tree)] = branch
        return admin

    def admin(self, place):
        """The query's two lines for `place`; None where git would fail."""
        place = Path(place)
        if place == self.repo:
            return f'{self.common}\n{self.common}\n'
        pointer = place / '.git'
        if not pointer.is_file():
            return None
        admin = (place / pointer.read_text(encoding='utf-8').removeprefix('gitdir: ').strip()).resolve()
        return f'{admin}\n{self.common}\n'

    def answer(self, repo, *args, timeout=None):
        if args != METADATA:
            raise AssertionError(f'unexpected git {args!r}')
        found = self.admin(repo)
        return SimpleNamespace(returncode=0 if found else 128, stdout=found or '', stderr='' if found else 'fatal')


class Facts(pipeline.Repository):
    """`pipeline.Repository` facts over `Trees`; `tag`, `owned` and `repair` stay real, so ownership runs as in use."""
    def __init__(self, trees):
        self.trees = trees

    def load(self, prog, revision='HEAD', checkout=None):
        return prog

    def sync(self, repo, remote, default):
        pass

    def start_ref(self, repo, branch, base, remote):
        return base

    def add(self, repo, dest, branch, start_ref, remote='origin'):
        self.trees.add(dest, branch)

    def inventory(self, repo, side=None):
        return [dict(worktree=path, branch='refs/heads/' + branch) for path, branch in self.trees.branches.items()]

    def branch(self, path):
        return self.trees.branches.get(os.path.normcase(Path(path)), 'main')

    def changes(self, repo):
        return []

    def dirty(self, path):
        return ''


class NativeMetadata(unittest.TestCase):
    def setUp(self):
        from drover import placement_git as placement
        from tests.fakeci import git_answers
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

        for context in (git_answers(git),           # anything but git, a Python child included, is refused
                        patch.object(placement, 'routes_through_wsl', return_value=False)):
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
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.fix = Trees(Path(temporary.name))
        self.addCleanup(config.reset)
        config.use(config.validate({'paths': {'state_dir': str(self.fix.root / 'state')}}))

    def pipe(self, task, **machine):
        port = SimpleNamespace(get_task=lambda tid: dict(task),
                               waiting=SimpleNamespace(state=lambda cause: None, answer=lambda cause: None),
                               update_task=lambda tid, **values: task.update(values),
                               append_event=lambda *args, **kwargs: None)
        prog = repos.normalize({'name': 'alpha', 'repo': str(self.fix.repo),
                                'worktrees': str(self.fix.root / 'task trees'), 'gate': {'platform': 'wsl'}, **machine})
        return pipeline.Pipeline(port, repos_map={'alpha': prog}, runs_dir=self.fix.root / 'state' / 'runs',
                                 repository=Facts(self.fix))

    def test_tag_lease_and_task_creation(self):
        from drover import ownership
        fix = self.fix
        tree = fix.root / 'trees with spaces' / 'linked'
        admin = fix.add(tree, 'linked')
        # Relative admin pointers are common after cross-side worktree creation.
        (tree / '.git').write_text('gitdir: ' + os.path.relpath(admin, tree) + '\n')
        with git_answers(fix.answer):
            record = ownership.tag(str(tree), 'alpha', 'session:literal', NOW + 86400, now=NOW)
            self.assertEqual(record, {'v': 2, 'repo': 'alpha', 'owner': 'session:literal',
                                      'created': NOW, 'lease_until': NOW + 86400})
            self.assertEqual(ownership.read(str(tree)), record)
            self.assertTrue(ownership.owned_by(str(tree), 'session:literal'))
            self.assertFalse(ownership.owned_by(str(tree), 'drover:other'))
            self.assertEqual(json.loads((admin / 'drover-tag.json').read_text()), record)
            self.assertEqual([p.name for p in tree.iterdir()], ['.git'])     # nothing in the tree for git to see
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

            task = {'id': 't-lease', 'title': 'lease acquisition', 'repo': 'alpha'}
            pipe = self.pipe(task, scripts={'test': '.drover/scripts/test.ps1'})
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
        """The WSL worker is real Python on purpose: its atomic IO, argv and stdin are what is tested. Git is faked."""
        from drover import ownership, placement
        fix = self.fix
        tree = fix.root / 'trees with spaces' / 'metadata'
        fix.add(tree, 'metadata')
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

        def git(repo, *args):
            found = fix.answer(repo, *args)
            return process.Result('exited', found.returncode, found.stdout.encode(), found.stderr.encode(), 0.0, '')
        with patch.object(placement, 'routes_through_wsl', return_value=True) as _placement_patch_1, patch('drover.placement_git.routes_through_wsl', _placement_patch_1), patch('drover.placement_paths.routes_through_wsl', _placement_patch_1), \
                patch.object(placement.WSL, 'command', new=local_transport), git_spy(git):
            record = ownership.tag(str(tree), 'alpha', 'session: "quote"\n雪', NOW + 1, now=NOW)
            self.assertEqual(ownership.read(str(tree)), record)
            workers = [call for call in calls if call[0] != 'git']
            self.assertEqual(len(workers), 3)
            self.assertTrue(all(call[0:2] == ['python3', '-c'] for call in workers))
            self.assertEqual([p.name for p in tree.iterdir()], ['.git'])
            self.assertIsNone(ownership.read(str(fix.repo)))
            with self.assertRaises(OSError):
                ownership.tag(str(fix.repo), 'alpha', 'session:x', NOW + 1, now=NOW)
        self.assertFalse((fix.repo / '.git' / 'drover-tag.json').exists())

    def test_clean_existing_worktree_acquisition_records_task_lease(self):
        from drover import ownership
        tree = self.fix.root / 'existing tree'
        self.fix.add(tree, 'existing')
        task = {'id': 't-adopt', 'repo': 'alpha', 'title': 'adopt clean worktree',
                'branch': 'existing', 'worktree': str(tree)}
        pipe = self.pipe(task)
        with patch.object(time, 'time', return_value=NOW), git_answers(self.fix.answer):
            self.assertEqual(pipe.worktree(task), str(tree))
            self.assertEqual(ownership.read(str(tree)),
                             {'v': 2, 'repo': 'alpha', 'owner': 'drover:t-adopt',
                              'created': NOW, 'lease_until': NOW + 86400})
