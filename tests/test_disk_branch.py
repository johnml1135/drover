"""Explicit branch approval remains a separate, restorable owner operation."""
import ast
import unittest
from pathlib import Path

from drover import config
from tests.gitfix import Repo, sh


class ApprovedBranch(unittest.TestCase):
    def test_explicit_approval_remains_separate(self):
        from drover import cleanup

        fix = Repo()
        self.addCleanup(fix.cleanup)
        self.addCleanup(config.reset)
        config.use(config.validate({'paths': {'state_dir': str(fix.root / 'state')}}))
        fix.git('remote', 'set-head', 'origin', 'main')
        tip = fix.branch('owner/topic', {'unique.txt': 'preserve this commit\n'})
        approved = {'branch': 'owner/topic', 'disposition': 'propose-delete', 'worktree': ''}
        before = fix.git('for-each-ref')
        refused = cleanup.delete_branch(fix.repo, 'owner/topic', {})
        self.assertFalse(refused['ok'], refused)
        self.assertEqual(fix.git('for-each-ref'), before)
        events = []
        result = cleanup.delete_branch(
            fix.repo, 'owner/topic', approved, now=1_800_000_000,
            rescue_root=fix.root / 'rescue', log=lambda step, **data: events.append(step))
        self.assertTrue(result['ok'], result)
        self.assertEqual(fix.git('rev-parse', result['rescue_ref']), tip)
        self.assertEqual(sh('git', '-C', fix.repo, 'show-ref', '--verify', '--quiet',
                            'refs/heads/owner/topic', check=False).returncode, 1)
        self.assertEqual(events, ['checked', 'rescue-ref', 'deleted'])
        self.assertEqual(fix.git('rev-parse', 'main'), fix.git('rev-parse', 'origin/main'))
        self.assertIsNone(result['bundle'])
        for path in (Path(__file__).resolve().parents[1] / 'drover').glob('disk*.py'):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and
                     ((isinstance(node.func, ast.Name) and node.func.id == 'delete_branch') or
                      (isinstance(node.func, ast.Attribute) and node.func.attr == 'delete_branch'))]
            self.assertEqual(calls, [], f'{path.name} must never perform branch disposition')

    def test_cleanup_lock_refuses_branch_mutation(self):
        from drover import cleanup, disk_state
        fix = Repo()
        self.addCleanup(fix.cleanup)
        self.addCleanup(config.reset)
        state = fix.root / 'state'
        config.use(config.validate({'paths': {'state_dir': str(state)}}))
        fix.git('remote', 'set-head', 'origin', 'main')
        fix.branch('locked/topic', {'only.txt': 'preserve\n'})
        before = fix.git('for-each-ref')
        with disk_state.CleanupLock(state):
            result = cleanup.delete_branch(fix.repo, 'locked/topic',
                {'branch': 'locked/topic', 'disposition': 'propose-delete'})
        self.assertFalse(result['ok'], result)
        self.assertIn('lock busy', result['reason'])
        self.assertEqual(fix.git('for-each-ref'), before)
