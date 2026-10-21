"""Round nine: conservative process evidence and default-branch disposal policy.

All process/Git evidence is supplied at the placement seam; no agents or sleeps.
"""
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

from drover import disk_paths, disk_state, disk_worktrees, placement
from tests.disk_fixture import DiskCase


class Processes(DiskCase):
    def test_new_pid_without_confirmed_creation_time_cannot_supply_foreign_evidence(self):
        request = dict(paths={}, state_dir=str(self.state), _inventory_id='round9')
        disk_state.save_file(self.state, 'processes-round9.json', dict(rows=[], identities={}))
        row = dict(pid=77, name='foreign.exe', same_user=False, cwd=None, cmd='')
        for before, after in ((None, None), (20, None)):
            with self.subTest(before=before, after=after), \
                    patch.object(placement, '_windows_processes', side_effect=[
                        {'77': ['foreign.exe', before]}, {'77': ['foreign.exe', after]}]), \
                    patch.object(placement, '_cim_rows', return_value=[row]):
                proof = placement.cleanup_processes(windows=True, request=request)
            self.assertTrue(proof.busy(r'H:\unrelated'))
            self.assertEqual(proof.reason, 'process visibility unavailable')
            self.assertEqual(proof.unreadable[0]['evidence'], 'process_identity_unavailable')

    def test_windows_command_line_preserves_apostrophes_and_quoted_backslashes(self):
        cases = [
            (r"tool.exe H:\repo'abc\input", r"H:\repo'abc"),
            (r'''tool.exe "H:\repo'abc\input"''', r"H:\repo'abc"),
            (r'''tool.exe --input="H:\repo space\input"''', r'H:\repo space'),
            (r'''tool.exe "H:\repo space\\"''', r'H:\repo space'),
            (r'''tool.exe H:\repo\a\"b\input''', r'H:\repo'),
        ]
        for command, root in cases:
            with self.subTest(command=command), patch.object(placement, 'IS_WINDOWS', True):
                proof = placement.cleanup_processes(windows=True, cim=lambda: [dict(
                    pid=77, name='foreign.exe', same_user=False, cwd=None, cmd=command)])
                self.assertTrue(proof.busy(root))
                self.assertFalse(proof.busy(r'H:\unrelated'))


class Approval(DiskCase):
    def test_approval_uses_single_character_windows_case_and_exact_linux_case(self):
        pairs = [('ﬁx', 'fix', False), ('ß', 'ss', False), ('ABC', 'abc', True)]
        for prefix, windows in (('H:/', True), ('/home/owner/', False)):
            for selected, approved, equal_on_windows in pairs:
                with self.subTest(prefix=prefix, selected=selected):
                    path = prefix + selected
                    request = dict(repos=[dict(repo=prefix + 'repo')], apply=False,
                                   approved_path=prefix + approved)
                    row = dict(path=path, reason_code='orphan', action='keep')
                    with patch.object(disk_worktrees, 'entries', return_value=[dict(worktree=path)]), \
                            patch.object(disk_worktrees, 'inspect', return_value=row), \
                            patch.object(disk_worktrees, 'quarantine', side_effect=lambda r, m, value: value):
                        answer = disk_worktrees.settled(request)
                    self.assertEqual(bool(answer['items']), windows and equal_on_windows)


class WindowsIdentity(DiskCase):
    def test_windows_identity_follows_ntfs_simple_bmp_upcase(self):
        same = lambda a, b: placement.cleanup_normalize('H:/' + a, True) == placement.cleanup_normalize('H:/' + b, True)
        self.assertFalse(same('\u0131', 'I'))
        self.assertFalse(same('\u017f', 'S'))
        self.assertFalse(same('\u212a', 'K'))
        self.assertTrue(same('\u00e4', '\u00c4'))
        self.assertFalse(same('\U00010428', '\U00010400'))

    def test_path_with_uncertain_character_is_retained_not_deleted(self):
        other = dict(pid=77, name='x.exe', same_user=False, cwd=r'H:\elsewhere', cmd='')
        for name in ('\u0131', '\u017f', '\U00010428'):
            with self.subTest(name=name):
                proof = placement.cleanup_processes(windows=True, cim=lambda: [other])
                self.assertTrue(proof.busy('H:/work/' + name))
        self.assertFalse(proof.busy('H:/work/\u00e4'))


class DefaultBranchPolicy(DiskCase):
    def setUp(self):
        super().setUp()
        self.repo = self.root / 'repo'
        self.wt = self.root / 'old-worktree'
        self.repo.mkdir()
        self.wt.mkdir()
        self.bin = self.file(self.wt / 'bin' / 'payload')
        self.src = self.file(self.wt / 'src' / 'precious')
        self.tracked = self.file(self.wt / 'bin' / 'tracked')
        self.main = 'schema_version=1\n[cleanup]\ndisposable=["bin"]\n'
        self.feature = 'schema_version=1\n'  # Worktree predates the declaration.
        self.machine = dict(name='alpha', repo=str(self.repo), default_branch='main')
        self.request = dict(repos=[self.machine], apply=True, now=self.now, paths={},
                            state_dir=str(self.state), drive=dict(level='critical'),
                            tasks=dict(at=self.now, active_worktrees=[], tasks=[]))
        self.entry = dict(worktree=str(self.wt), HEAD='old-head', locked=self.side)
        self.reads = []
        for context in (
                patch.object(placement, 'reachable', return_value=True),
                patch.object(placement, 'cleanup_contains', return_value=True),
                patch.object(placement, 'cleanup_processes', return_value=SimpleNamespace(busy=lambda p: False)),
                patch.object(placement, 'git', side_effect=self.git),
                patch.object(disk_worktrees, 'entries', return_value=[self.entry])):
            context.start()
            self.addCleanup(context.stop)

    def git(self, repo, *args, **kw):
        self.reads.append((str(repo), args))
        answer, code = '', 0
        if args[0] == 'rev-parse':
            if args[-1] == 'HEAD':
                answer = 'old-head'
            elif args[-1] == 'refs/heads/main^{commit}':
                answer = 'main-head'
            elif args[-1] == 'old-head^{commit}':
                answer = 'old-head'
            else:
                code = 1
        elif args[0] == 'ls-tree':
            answer = '100644 blob config\t.drover/config.toml'
        elif args[0] == 'show':
            if args[1].startswith('main-head:') and self.main is None:
                code = 1
            else:
                answer = self.main if args[1].startswith('main-head:') else self.feature
        elif args[0] == 'status':
            answer = ''.join('!! ' + name + '\0' for name in
                             ('bin/payload', 'bin/tracked', 'src/precious') if (self.wt / name).exists())
        elif args[0] == 'ls-files':
            answer = 'bin/tracked\0' if '--' not in args or args[-1] == 'bin/tracked' else ''
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, code, answer, 'default branch unreadable' if code else '')

    def test_old_worktree_uses_main_policy_and_own_tracked_index(self):
        answer = disk_paths.outputs(self.request)
        self.assertFalse(self.bin.exists(), answer)
        self.assertTrue(self.tracked.exists())
        self.assertTrue(self.src.exists())
        self.assertTrue(any(row['action'] == 'removed' for row in answer['items']))

    def test_feature_policy_cannot_widen_default_branch_declarations(self):
        self.feature = 'schema_version=1\n[cleanup]\ndisposable=["bin", "src/**"]\n'
        answer = disk_paths.outputs(self.request)
        self.assertTrue(self.src.exists(), answer)
        self.assertFalse(self.bin.exists())
        self.assertTrue(all('old-head:' not in str(args) for repo, args in self.reads))

    def test_missing_or_unreadable_default_policy_retains_and_reports(self):
        for branch, contents in (('missing', self.main), ('main', None), ('main', '<invalid policy>')):
            with self.subTest(branch=branch):
                self.machine['default_branch'], self.main = branch, contents
                answer = disk_paths.outputs(self.request)
                self.assertTrue(self.bin.exists(), answer)
                self.assertTrue(self.src.exists())
                self.assertEqual(answer['items'][0]['reason_code'], 'output_evidence_unavailable')
                self.assertIn('error', answer['items'][0])

    def test_quarantine_uses_default_policy_even_when_feature_has_no_declaration(self):
        row = dict(path=str(self.wt), repo=str(self.repo), ignored=[str(self.bin)],
                   head='old-head', reason_code='orphan', action='keep')
        answer = disk_worktrees.quarantine({**self.request, 'apply': False}, self.machine, row)
        self.assertEqual(answer['reason_code'], 'orphan')

    def test_remove_cannot_use_feature_declarations_to_discard_source(self):
        self.feature = 'schema_version=1\n[cleanup]\ndisposable=["bin", "src/**"]\n'
        selected = dict(path=str(self.wt), repo=str(self.repo), head='old-head',
                        reason_code='settled', action='candidate', ignored=[str(self.src)])
        with patch.object(disk_worktrees, 'inspect', return_value=selected.copy()):
            answer = disk_worktrees.remove(self.request, selected)
        self.assertEqual(answer['reason_code'], 'ignored_content')
        self.assertTrue(self.src.exists())

    def test_explicit_local_default_branch_ref_has_the_same_policy(self):
        self.machine['default_branch'] = 'refs/heads/main'
        answer = disk_paths.outputs(self.request)
        self.assertFalse(self.bin.exists(), answer)
        self.assertTrue(self.src.exists())

