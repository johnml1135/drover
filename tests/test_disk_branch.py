"""Explicit branch approval remains a separate, restorable owner operation."""
import ast
import subprocess
import tempfile
import unittest
from pathlib import Path

from drover import config
from tests.fakeci import git_answers

TIP, MAIN = 'a' * 40, 'b' * 40


class Refs:
    """The refs of one repo, answering the git commands `cleanup.delete_branch` runs: git is setup here, not the point
    (test_garden_branch runs delete_branch against real git)."""
    def __init__(self, repo, branch):
        self.repo = repo
        self.refs = {'refs/heads/main': MAIN, 'refs/remotes/origin/main': MAIN, f'refs/heads/{branch}': TIP}

    def __call__(self, repo, *args, timeout):
        def done(code=0, out=''):
            return subprocess.CompletedProcess(args, code, out, '')
        if args[:2] == ('check-ref-format', '--branch'):
            return done()
        if args == ('symbolic-ref', '--short', 'refs/remotes/origin/HEAD'):
            return done(0, 'origin/main\n')
        if args[:3] == ('rev-parse', '--verify', '-q'):
            sha = self.refs.get(args[3].removesuffix('^{commit}'))
            return done(0, sha + '\n') if sha else done(1)
        if args[:2] == ('worktree', 'list'):
            return done(0, f'worktree {self.repo}\0HEAD {MAIN}\0branch refs/heads/main\0\0')
        if args[:2] == ('merge-base', '--is-ancestor'):
            return done(1)
        if args[0] == 'cherry':
            return done(0, f'+ {args[2]}\n')
        if args[:2] == ('update-ref', '-d'):
            if self.refs.get(args[2]) != args[3]:
                return done(1)
            del self.refs[args[2]]
            return done()
        if args[0] == 'update-ref' and args[3] == '0' * 40:
            if args[1] in self.refs:
                return done(1)
            self.refs[args[1]] = args[2]
            return done()
        raise AssertionError(f'unexpected git {args!r}')


class ApprovedBranch(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.addCleanup(config.reset)
        config.use(config.validate({'paths': {'state_dir': str(self.root / 'state')}}))

    def test_explicit_approval_remains_separate(self):
        from drover import cleanup
        fake = Refs(self.repo, 'owner/topic')
        approved = {'branch': 'owner/topic', 'disposition': 'propose-delete', 'worktree': ''}
        before = dict(fake.refs)
        with git_answers(fake):
            refused = cleanup.delete_branch(self.repo, 'owner/topic', {})
        self.assertFalse(refused['ok'], refused)
        self.assertEqual(fake.refs, before)
        events = []
        with git_answers(fake):
            result = cleanup.delete_branch(
                self.repo, 'owner/topic', approved, now=1_800_000_000,
                rescue_root=self.root / 'rescue', log=lambda step, **data: events.append(step))
        self.assertTrue(result['ok'], result)
        self.assertEqual(fake.refs[result['rescue_ref']], TIP)
        self.assertNotIn('refs/heads/owner/topic', fake.refs)
        self.assertEqual(events, ['checked', 'rescue-ref', 'deleted'])
        self.assertEqual((fake.refs['refs/heads/main'], fake.refs['refs/remotes/origin/main']), (MAIN, MAIN))
        self.assertIsNone(result['bundle'])
        for path in (Path(__file__).resolve().parents[1] / 'drover').glob('disk*.py'):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and
                     ((isinstance(node.func, ast.Name) and node.func.id == 'delete_branch') or
                      (isinstance(node.func, ast.Attribute) and node.func.attr == 'delete_branch'))]
            self.assertEqual(calls, [], f'{path.name} must never perform branch disposition')

    def test_cleanup_lock_refuses_branch_mutation(self):
        from drover import cleanup, disk_state
        fake = Refs(self.repo, 'locked/topic')
        before = dict(fake.refs)
        with disk_state.CleanupLock(config.state_dir()), git_answers(fake):
            result = cleanup.delete_branch(self.repo, 'locked/topic',
                                           {'branch': 'locked/topic', 'disposition': 'propose-delete'})
        self.assertFalse(result['ok'], result)
        self.assertIn('lock busy', result['reason'])
        self.assertEqual(fake.refs, before)
