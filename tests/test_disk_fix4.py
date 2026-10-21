"""Fourth review regressions through the public cleanup interface."""
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

from drover import ownership, placement
from drover.disk_side import serve
from tests import test_disk_fix2 as fix2
from tests.disk_fixture import DiskCase
from tests.test_disk_worktrees import WorktreeCase


class CurrentOwner(WorktreeCase):
    def test_terminal_path_match_cannot_settle_another_owner(self):
        protected = []
        records = []
        for name, lease in (('current', self.now + 86400), ('expired', self.now - 1)):
            path = self.worktree(name)
            self.r.commit('unmerged', cwd=path)
            ownership.tag(str(path), 'alpha', 'drover:new-' + name, lease, now=self.now)
            records.append(dict(id='old-' + name, terminal=True, live=False, worktree=str(path)))
            protected.append(path)
        control = self.worktree('terminal current owner')
        self.r.commit('unmerged', cwd=control)
        ownership.tag(str(control), 'alpha', 'drover:done', self.now + 86400, now=self.now)
        records.append(dict(id='done', terminal=True, live=False, worktree=str(control)))
        self.tasks = dict(at=self.now, active_worktrees=[], tasks=records)
        self.cleanup()
        result = self.cleanup(apply=True)
        self.assertFalse(control.exists(), 'matching terminal owner is eligible')
        for path in protected:
            self.assertTrue(path.exists(), result)
            self.reported(result, path)
        self.now += 4 * 86400
        self.tasks['at'] = self.now
        result = self.cleanup(apply=True)
        for path in protected:
            self.assertTrue(path.exists(), 'unknown current owner cannot enter quarantine')
            self.reported(result, path)

    def test_untagged_terminal_path_match_uses_orphan_quarantine(self):
        path = self.worktree('orphan', lease=False)
        self.r.commit('unmerged', cwd=path)
        self.tasks = dict(at=self.now, active_worktrees=[], tasks=[dict(
            id='old-owner', terminal=True, live=False, worktree=str(path))])
        self.cleanup()
        result = self.cleanup(apply=True)
        self.assertTrue(path.exists())
        self.assertTrue(any(i['path'] == str(path) and i['reason_code'] == 'orphan'
                            for i in result['items']))
        self.now += 4 * 86400
        self.tasks['at'] = self.now
        self.cleanup(apply=True)
        self.assertFalse(path.exists(), 'the orphan grace period remains the resolver')

    def test_live_path_match_keeps_terminal_current_owner(self):
        path = self.worktree('live path')
        self.r.commit('unmerged', cwd=path)
        ownership.tag(str(path), 'alpha', 'drover:done', self.now + 86400, now=self.now)
        self.tasks = dict(at=self.now, active_worktrees=[], tasks=[
            dict(id='done', terminal=True, live=False),
            dict(id='other', terminal=False, live=True, worktree=str(path))])
        self.cleanup()
        result = self.cleanup(apply=True)
        self.assertTrue(path.exists())
        self.reported(result, path)


class UncertaintyReports(WorktreeCase):
    def test_unknown_process_visibility_and_status_are_reported(self):
        for unknown in ('process', 'status', 'status-timeout', 'listing'):
            with self.subTest(unknown=unknown):
                path = self.worktree('unknown-' + unknown)
                self.r.commit('unmerged', cwd=path)
                target = self.file(path / 'generated' / 'valuable')
                control_tree = self.worktree('control-' + unknown)
                control = self.file(control_tree / 'generated' / 'disposable')
                request = fix2.PathSafety.request(self, 5)
                request['repos'] = [dict(name='alpha', repo=str(self.r.repo),
                    worktrees=str(self.trees), remote='origin', default_branch='main')]
                real_git = placement.git
                def git(repo, *args, **kwargs):
                    if unknown == 'status-timeout' and str(repo) == str(path) and 'status' in args:
                        raise subprocess.TimeoutExpired(['git', 'status'], 60)
                    if unknown == 'status' and str(repo) == str(path) and 'status' in args or \
                            unknown == 'listing' and str(repo) == str(self.r.repo) and 'list' in args:
                        return subprocess.CompletedProcess(['git'], 128, '', 'repository unavailable')
                    return real_git(repo, *args, **kwargs)
                proof = SimpleNamespace(busy=lambda p: unknown == 'process' and p == str(path),
                                        reason='process visibility unavailable', unknown=True)
                with patch.object(placement, 'physical_disk_path', return_value=str(self.root)), \
                        patch.object(placement, 'cleanup_processes', return_value=proof), \
                        patch.object(placement, 'git', side_effect=git):
                    result = serve(request)
                self.assertTrue(target.exists(), result)
                reported_path = str(self.r.repo if unknown == 'listing' else path)
                self.assertTrue(any(i['path'] == reported_path and i['action'] == 'keep'
                                    and i.get('reason_code') for i in result['items']), result)
                if unknown != 'listing':
                    self.assertFalse(control.exists(), 'independent worktree remains eligible')
                else:
                    self.assertTrue(control.exists(), 'unknown listing retains all its worktrees')


class FixtureEncoding(DiskCase):
    def test_bom_git_config_fixture_uses_utf8_with_non_utf8_locale(self):
        code = '''from tests.disk_fixture import DiskCase
case = DiskCase()
case.setUp()
try:
    path = case.file(case.temp_root / 'config', '\\ufeff[core]\\n bare = true\\n')
    assert path.read_bytes().startswith(b'\\xef\\xbb\\xbf')
    assert path.read_text(encoding='utf-8') == '\\ufeff[core]\\n bare = true\\n'
finally:
    case.doCleanups()
'''
        done = subprocess.run([sys.executable, '-c', code], capture_output=True, timeout=30,
            text=True, encoding='utf-8', env=dict(os.environ, PYTHONUTF8='0',
                PYTHONCOERCECLOCALE='0', LC_ALL='C'))
        self.assertEqual(done.returncode, 0, done.stderr)
