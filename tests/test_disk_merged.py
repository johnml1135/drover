"""`disk_worktrees.merged` decides "merged", the deletion direction: an ancestry or merge-tree read that did not end
with an answer raises, and the stage keeps the row. Git is faked below git_client."""
import subprocess
import unittest
from types import SimpleNamespace

from drover import disk_worktrees
from tests.fakeci import git_answers

BASE, HEAD = 'b' * 40, 'h' * 40
MACHINE = dict(repo='/repo', remote='origin', default_branch='main', name='alpha')


def git(merge_base, merge_tree=(0, 't' * 40 + '\n')):
    """Answers for one `merged` call: `merge_base` and `merge_tree` are `(code, out)`, or an exception to raise."""
    def reply(repo, *args, timeout):
        def done(code, out=''):
            return subprocess.CompletedProcess(args, code, out, 'fatal: odd\n' if code > 1 else '')
        if args[0] == 'rev-parse':
            return done(0, ('t' * 40 if args[-1].endswith('^{tree}') else BASE) + '\n')
        if args[0] == 'cherry':
            return done(0, f'+ {HEAD}\n')
        answer = {'merge-base': merge_base, 'merge-tree': merge_tree}[args[0]]
        if isinstance(answer, BaseException):
            raise answer
        return done(*answer)
    return git_answers(reply)


TIMEOUT = subprocess.TimeoutExpired(['git'], 60)
NO_GIT = FileNotFoundError(2, 'No such file or directory')


class Merged(unittest.TestCase):
    def test_an_ancestry_read_that_did_not_answer_raises(self):
        for name, answer in (('timeout', TIMEOUT), ('unavailable', NO_GIT), ('exit 128', (128, ''))):
            with self.subTest(name), git(answer), self.assertRaisesRegex(ValueError, '^ancestry unavailable$'):
                disk_worktrees.merged(MACHINE, HEAD)

    def test_a_merge_tree_read_that_did_not_answer_raises(self):
        for name, answer in (('timeout', TIMEOUT), ('unavailable', NO_GIT), ('exit 128', (128, ''))):
            with self.subTest(name), git((1, ''), answer), self.assertRaisesRegex(ValueError, '^merge-tree unavailable$'):
                disk_worktrees.merged(MACHINE, HEAD)

    def test_exit_1_on_both_reads_is_not_merged(self):
        with git((1, ''), (1, 'c' * 40 + '\n')):
            self.assertIs(disk_worktrees.merged(MACHINE, HEAD), False)

    def test_an_ancestor_is_merged(self):
        with git((0, '')):
            self.assertIs(disk_worktrees.merged(MACHINE, HEAD), True)

    def test_the_stage_keeps_the_row_when_ancestry_is_unknown(self):
        path = '/repo.worktrees/T1'

        class Evidence:     # observes as inspect does for a leased, unfinished tree: it asks `merged`
            def observe(self, request):
                disk_worktrees.merged(MACHINE, HEAD)
                raise AssertionError('merged answered')

        request = dict(repos=[MACHINE], apply=False, approved_path=path, now=0, state_dir=None,
                       _listings=SimpleNamespace(of=lambda repo: SimpleNamespace(rows=[dict(worktree=path)])))
        runtime = SimpleNamespace(state=object(), evidence=Evidence())
        with git(TIMEOUT):
            out = disk_worktrees.settled(request, runtime=runtime)
        [row] = out['items']
        self.assertEqual((row['action'], row['reason_code'], row['error']), ('keep', 'unavailable', 'ancestry unavailable'))
        self.assertEqual(out['reclaimed_bytes'], 0)


if __name__ == '__main__':
    unittest.main()
