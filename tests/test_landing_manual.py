"""The checks kept from the manual-land branch, against real git."""
import subprocess
import unittest
from pathlib import Path

from drover import landing
from drover.git_client import LONG, Answer
from tests.fakeci import git_spawns
from tests.gitfix import Repo
from tests.landing_fixture import ProcessFreeCase
from tests.tiers import integration


@integration
class ManualLandHelpers(unittest.TestCase):
    def setUp(self):
        self.r = Repo()
        self.addCleanup(self.r.cleanup)

    def test_contained_after_fast_forward_and_after_replay(self):
        self.r.branch('ff', {'ff.txt': 'x'})
        self.r.branch('replay', {'r.txt': 'y'})
        self.r.branch('open', {'o.txt': 'z'})
        self.r.git('checkout', '-q', 'main')
        self.r.git('merge', '-q', '--ff-only', 'ff')
        self.r.git('cherry-pick', 'replay')           # same patch, new sha
        main = self.r.git('rev-parse', 'main')
        self.assertIs(landing.contained(self.r.repo, 'ff', main), Answer.YES)
        self.assertIs(landing.contained(self.r.repo, 'replay', main), Answer.YES)
        self.assertIs(landing.contained(self.r.repo, 'open', main), Answer.NO)
        self.assertIs(landing.contained(self.r.repo, 'no-such-branch', main), Answer.NO)

    def test_verify_manual_land_needs_a_branch_and_main_sha_on_the_default_branch(self):
        self.r.branch('ff', {'ff.txt': 'x'})
        tip = self.r.git('rev-parse', 'ff')
        with self.assertRaisesRegex(ValueError, 'no branch'):
            landing.verify_manual_land(self.r.repo, '', tip, 'main')
        with self.assertRaisesRegex(ValueError, 'not on main'):     # the branch tip is its own ancestor
            landing.verify_manual_land(self.r.repo, 'ff', tip, 'main')
        with self.assertRaisesRegex(ValueError, 'not a commit'):
            landing.verify_manual_land(self.r.repo, 'ff', 'f' * 40, 'main')
        self.r.git('checkout', '-q', 'main')
        self.r.git('merge', '-q', '--ff-only', 'ff')
        self.assertEqual(landing.verify_manual_land(self.r.repo, 'ff', tip[:10], 'main'), tip)


class ManualLandTests(ProcessFreeCase):
    """verify_manual_land at the one process fake, in the default tier."""
    def test_landing_256_raises_as_not_contained_when_unknown(self):
        full, tip, timeouts = 'f' * 40, 'e' * 40, []

        def reply(repo, *args, timeout):
            timeouts.append((args[0], timeout))
            if args[:2] == ('merge-base', '--is-ancestor') and args[2] == 'feat':
                raise subprocess.TimeoutExpired(['git', *args], timeout)     # git cannot say: unknown, not no
            return subprocess.CompletedProcess(args, 0, (full if 'abc' in args[-1] else tip) + '\n', '')
        git_spawns(self, reply)
        with self.assertRaisesRegex(ValueError, "commits of 'feat' are not all contained"):
            landing.verify_manual_land(Path('repo'), 'feat', 'abc', 'main')
        self.assertEqual(timeouts[2:], [('rev-parse', LONG), ('merge-base', LONG)])     # contained's reads, at :256


if __name__ == '__main__':
    unittest.main()
