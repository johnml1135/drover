"""Origin freshness keeps checked-out main consistent with its ref, through the `git=` object origin is given."""
import unittest
from pathlib import PurePath

from drover import origin
from drover.git_client import FETCH, LONG, Answer, GitDone, GitFailed, GitReply

REPO, OTHER = '/work/repo', '/work/main checkout with spaces'
OLD, NEW = 'a' * 40, 'b' * 40
SECRET = 'fatal: unable to access https://token@host/repo.git'     # what git's stderr can carry


def listing(*records):
    return ''.join(f'worktree {path}\0HEAD {head}\0' + (f'branch {branch}\0' if branch else 'detached\0') + '\0'
                   for path, head, branch in records)


def world(changes=None, remote='origin', main='main'):
    """A repo whose main is OLD, one behind its remote's NEW, checked out clean at REPO. `changes` overrides a row,
    keyed by the argv's leading words; a value is (exit, stdout), or 'timeout' or 'unavailable'."""
    table = {
        ('fetch',): (0, ''),
        ('rev-parse', '--verify', f'refs/remotes/{remote}/{main}^{{commit}}'): (0, NEW + '\n'),
        ('rev-parse', '--verify', f'refs/heads/{main}'): (0, OLD + '\n'),
        ('rev-parse', '--verify', f'refs/heads/{main}^{{commit}}'): (0, OLD + '\n'),
        ('rev-parse', '--verify', f'refs/remotes/{remote}/{main}'): (0, NEW + '\n'),
        ('merge-base', '--is-ancestor', NEW, OLD): (1, ''),
        ('merge-base', '--is-ancestor', OLD, NEW): (0, ''),
        ('worktree', 'list'): (0, listing((REPO, OLD, f'refs/heads/{main}'))),
        ('status',): (0, ''),
        ('rev-parse', 'HEAD'): (0, OLD + '\n'),
        ('symbolic-ref',): (0, f'refs/heads/{main}\n'),
        ('merge',): (0, ''),
        ('update-ref',): (0, ''),
        ('push',): (0, ''),
        ('rev-list', '--count'): (0, '1\n'),
        ('ls-remote',): (0, f'{NEW}\trefs/heads/{main}\n'),
    }
    table.update(changes or {})

    def answer(repo, args):
        for n in range(len(args), 0, -1):
            if args[:n] in table:
                return table[args[:n]]
        raise AssertionError(f'unexpected git {args}')
    return answer


class FakeGit:
    """`git=` for origin: git_client's ask, out and mutate over `answer(repo, args)`. Each call is kept as
    (verb, repo, args, timeout_s), and a failed run's stderr holds SECRET."""

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def _run(self, verb, repo, args, timeout_s):
        repo = PurePath(repo).as_posix()       # fast_forward hands a checkout on as a Path
        self.calls.append((verb, repo, args, timeout_s))
        found = self.answer(repo, args)
        if isinstance(found, str):
            return None, '', SECRET, found
        code, out = found
        return code, out, SECRET if code else '', None

    def ask(self, repo, *args, timeout_s, no):
        code, out, err, why = self._run('ask', repo, args, timeout_s)
        answer = Answer.YES if code == 0 else Answer.NO if code in no else Answer.UNKNOWN
        return GitReply(answer, out, err, code, why or 'exit' if answer is Answer.UNKNOWN else None)

    def out(self, repo, *args, timeout_s, error=None):
        code, out, err, why = self._run('out', repo, args, timeout_s)
        if code == 0:
            return out
        failed = GitFailed(args, why or 'exit', code, err, out)
        if error is None:
            raise failed
        raise error(failed) from failed

    def mutate(self, repo, *args, timeout_s):
        code, out, err, why = self._run('mutate', repo, args, timeout_s)
        return GitDone(code == 0, out, err, code, None if code == 0 else why or 'exit')

    def mutations(self):
        return [(repo, args) for verb, repo, args, _ in self.calls if verb == 'mutate']


def sync(changes=None, *args):
    git = FakeGit(world(changes, *args))
    return git, origin.sync(REPO, *args, git=git)


class SyncTests(unittest.TestCase):
    def test_sync_advances_checked_out_main_through_its_checkout(self):
        git, sha = sync()
        self.assertEqual(sha, NEW)
        self.assertEqual(git.mutations(), [
            (REPO, ('fetch', 'origin', 'main', '--refmap=+refs/heads/main:refs/remotes/origin/main')),
            (REPO, ('merge', '--ff-only', '--no-autostash', NEW))])

    def test_sync_advances_main_in_another_worktree(self):
        moved = listing((REPO, OLD, None), (OTHER, OLD, 'refs/heads/main'))
        git, sha = sync({('worktree', 'list'): (0, moved)})
        self.assertEqual(sha, NEW)
        self.assertEqual({repo for verb, repo, args, _ in git.calls
                          if args[0] in ('status', 'symbolic-ref', 'merge') or args == ('rev-parse', 'HEAD')}, {OTHER})

    def test_dirty_checkout_holds_sync_without_changing_owner_work(self):
        for status in (' M base.txt\n', 'M  base.txt\n', '?? owner.txt\n'):
            with self.subTest(status=status):
                git = FakeGit(world({('status',): (0, status)}))
                with self.assertRaisesRegex(origin.DirtyCheckout, 'main checkout dirty'):
                    origin.sync(REPO, git=git)
                self.assertEqual([args[0] for _, args in git.mutations()], ['fetch'])
                self.assertIn(('out', REPO, ('status', '--porcelain', '--untracked-files=normal'), LONG), git.calls)

    def test_moved_main_fails_cas(self):
        for name, changes in (('checkout rewound', {('rev-parse', 'HEAD'): (0, 'c' * 40)}),
                              ('checkout switched', {('symbolic-ref',): (0, 'refs/heads/other\n')}),
                              ('checkout detached', {('symbolic-ref',): (1, '')}),
                              ('ref moved, unchecked out', {('worktree', 'list'): (0, listing((REPO, OLD, None))),
                                                            ('update-ref',): (1, '')})):
            with self.subTest(name):
                git = FakeGit(world(changes))
                with self.assertRaisesRegex(origin.SyncError, 'main moved during origin sync') as caught:
                    origin.sync(REPO, git=git)
                self.assertIsInstance(caught.exception.__cause__, origin.MainMoved)
                self.assertNotIn('merge', [args[0] for _, args in git.mutations()])

    def test_unchecked_out_main_uses_update_ref_and_preserves_checkout(self):
        git, sha = sync({('worktree', 'list'): (0, listing((REPO, OLD, None)))})
        self.assertEqual(sha, NEW)
        self.assertEqual(git.mutations()[1:], [(REPO, ('update-ref', 'refs/heads/main', NEW, OLD))])
        self.assertFalse([args for _, _, args, _ in git.calls if args[0] in ('status', 'merge')])

    def test_divergence_refuses_without_moving_main(self):
        git = FakeGit(world({('merge-base', '--is-ancestor', OLD, NEW): (1, '')}))
        with self.assertRaisesRegex(origin.SyncError, 'main diverged from origin'):
            origin.sync(REPO, git=git)
        self.assertEqual([args[0] for _, args in git.mutations()], ['fetch'])

    def test_an_unknown_ancestry_is_not_read_as_divergence_or_as_behind(self):
        git = FakeGit(world({('merge-base', '--is-ancestor', OLD, NEW): 'timeout'}))
        with self.assertRaisesRegex(origin.SyncError, 'git merge-base failed') as caught:
            origin.sync(REPO, git=git)
        self.assertNotIn('token@host', str(caught.exception))
        self.assertEqual([args[0] for _, args in git.mutations()], ['fetch'])

    def test_local_unpushed_landings_are_preserved(self):
        git, sha = sync({('merge-base', '--is-ancestor', NEW, OLD): (0, '')})
        self.assertEqual(sha, OLD)
        self.assertEqual([args[0] for _, args in git.mutations()], ['fetch'])

    def test_offline_does_not_use_cached_remote(self):
        for failed in ((128, ''), 'timeout', 'unavailable'):
            with self.subTest(failed=failed):
                git = FakeGit(world({('fetch',): failed}))
                with self.assertRaisesRegex(origin.FetchFailed, 'fetch failed: origin/main'):
                    origin.sync(REPO, git=git)
                self.assertEqual(len(git.calls), 1)

    def test_configured_remote_and_main_are_fetched_on_the_fetch_limit(self):
        git, sha = sync(None, 'upstream', 'trunk')
        self.assertEqual(sha, NEW)
        self.assertEqual(git.calls[0], ('mutate', REPO, ('fetch', 'upstream', 'trunk',
                                                         '--refmap=+refs/heads/trunk:refs/remotes/upstream/trunk'), FETCH))
        self.assertEqual(git.mutations()[-1], (REPO, ('merge', '--ff-only', '--no-autostash', NEW)))

    def test_cas_on_main_stays_a_mutate(self):
        for held in (listing((REPO, OLD, 'refs/heads/main')), listing((REPO, OLD, None))):
            git, _ = sync({('worktree', 'list'): (0, held)})
            origin.push_main(REPO, git=git)
            changes = [(verb, args[0]) for verb, _, args, _ in git.calls if args[0] in ('fetch', 'merge', 'update-ref', 'push')]
            self.assertTrue(changes)
            self.assertEqual({verb for verb, _ in changes}, {'mutate'}, changes)

    def test_non_zero_update_ref_raises_main_moved(self):
        unchecked = (0, listing((REPO, OLD, None)))
        for failed in ((1, ''), (128, ''), 'timeout'):
            with self.subTest(failed=failed):
                git = FakeGit(world({('worktree', 'list'): unchecked, ('update-ref',): failed}))
                with self.assertRaisesRegex(origin.MainMoved, 'main moved during fast-forward; retry'):
                    origin.fast_forward(REPO, 'main', NEW, OLD, git=git)

    def test_a_listing_that_fails_is_not_read_as_no_checkout(self):
        for failed in ((128, ''), 'timeout', 'unavailable'):      # an empty listing would send main to the CAS
            with self.subTest(failed=failed):
                git = FakeGit(world({('worktree', 'list'): failed}))
                with self.assertRaisesRegex(origin.SyncError, '^git worktree failed$') as caught:
                    origin.fast_forward(REPO, 'main', NEW, OLD, git=git)
                self.assertNotIsInstance(caught.exception, origin.MainMoved)
                self.assertEqual(git.mutations(), [])

    def test_timed_out_merge_raises_sync_error_not_main_moved(self):
        for why in ('timeout', 'unavailable'):
            with self.subTest(why=why):
                git = FakeGit(world({('merge',): why}))
                with self.assertRaises(origin.SyncError) as caught:
                    origin.fast_forward(REPO, 'main', NEW, OLD, git=git)
                self.assertNotIsInstance(caught.exception, origin.MainMoved)
                self.assertIn('owner intervention required', str(caught.exception))
                self.assertIn(why, str(caught.exception))
                with self.assertRaises(origin.SyncError) as caught:
                    origin.sync(REPO, git=FakeGit(world({('merge',): why})))
                self.assertNotIn('moved', str(caught.exception))
        git = FakeGit(world({('merge',): (1, '')}))     # a merge git refused is still a moved main
        with self.assertRaises(origin.MainMoved):
            origin.fast_forward(REPO, 'main', NEW, OLD, git=git)

    def test_check_true_reads_and_push_main_raise_sync_error_with_no_stderr(self):
        reads = {('rev-parse', '--verify', 'refs/remotes/origin/main^{commit}'): 'git rev-parse failed',
                 ('rev-parse', '--verify', 'refs/heads/main'): 'git rev-parse failed',
                 ('worktree', 'list'): 'git worktree failed',
                 ('status',): 'git status failed',
                 ('rev-parse', 'HEAD'): 'git rev-parse failed'}
        for key, message in reads.items():
            for failed in ((128, ''), 'timeout', 'unavailable'):
                with self.subTest(read=key, failed=failed):
                    with self.assertRaises(origin.SyncError) as caught:
                        origin.sync(REPO, git=FakeGit(world({key: failed})))
                    self.assertEqual(str(caught.exception), message)
                    self.assertIsInstance(caught.exception.__cause__, GitFailed)
        for key, message in ((('rev-parse', '--verify', 'refs/heads/main^{commit}'), 'git rev-parse failed'),
                             (('push',), 'push failed: origin/main; local landing is retained')):
            for failed in ((1, ''), 'timeout'):
                with self.subTest(push=key, failed=failed):
                    git = FakeGit(world({key: failed}))
                    with self.assertRaises(origin.SyncError) as caught:
                        origin.push_main(REPO, git=git)
                    self.assertEqual(str(caught.exception), message)
                    self.assertNotIn('update-ref', [args[0] for _, args in git.mutations()])


class PushTests(unittest.TestCase):
    def test_push_uses_local_main_and_never_forces(self):
        git = FakeGit(world())
        origin.push_main(REPO, git=git)
        self.assertEqual(git.mutations(), [(REPO, ('push', 'origin', f'{OLD}:refs/heads/main')),
                                           (REPO, ('update-ref', 'refs/remotes/origin/main', OLD, NEW))])
        self.assertEqual(git.calls[-2][3], FETCH)

    def test_push_tracks_main_even_with_no_tracking_ref(self):
        for failed in ((128, ''), 'timeout'):
            git = FakeGit(world({('rev-parse', '--verify', 'refs/remotes/origin/main'): failed}))
            origin.push_main(REPO, git=git)
            self.assertEqual(git.mutations()[-1], (REPO, ('update-ref', 'refs/remotes/origin/main', OLD, '0' * 40)))

    def test_a_held_guard_stops_the_push(self):
        git = FakeGit(world())

        def guard():
            raise RuntimeError('held')
        with self.assertRaisesRegex(RuntimeError, 'held'):
            origin.push_main(REPO, git=git, guard=guard)
        self.assertEqual(git.mutations(), [])

    def test_ahead_counts_and_a_timeout_is_unknown(self):
        self.assertEqual(origin.ahead(REPO, git=FakeGit(world())), 1)
        self.assertIsNone(origin.ahead(REPO, git=FakeGit(world({('rev-list', '--count'): 'timeout'}))))
        self.assertEqual(origin.ahead(REPO, git=FakeGit(world({('rev-list', '--count'): (128, '')}))), 0)
        git = FakeGit(world())
        origin.ahead(REPO, timeout=30, git=git)
        self.assertEqual(git.calls, [('ask', REPO, ('rev-list', '--count', 'refs/remotes/origin/main..refs/heads/main'), 30)])
