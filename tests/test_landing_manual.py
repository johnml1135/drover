"""The checks kept from the manual-land branch, against real git."""
import unittest

from drover import landing
from tests.gitfix import Repo
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
        self.assertTrue(landing.contained(self.r.repo, 'ff', main))
        self.assertTrue(landing.contained(self.r.repo, 'replay', main))
        self.assertFalse(landing.contained(self.r.repo, 'open', main))
        self.assertFalse(landing.contained(self.r.repo, 'no-such-branch', main))

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


if __name__ == '__main__':
    unittest.main()
