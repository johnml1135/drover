import tempfile
import unittest

from drover import placement
from tests import rcw_repo


class CheckoutKeyTests(unittest.TestCase):
    def test_every_spelling_of_a_checkout_maps_to_one_key(self):
        forms = ['H:\\Repos\\X.wt\\a', 'h:/Repos/X.wt/a', 'H:\\repos\\x.wt\\a\\', '/mnt/h/Repos/X.wt/a',
                 '/mnt/H/repos/x.wt/a/']
        self.assertEqual({rcw_repo.key(f) for f in forms}, {'/mnt/h/repos/x.wt/a'})

    def test_lookup_accepts_other_spellings_and_failure_names_the_path(self):
        with tempfile.TemporaryDirectory() as root:
            repo = rcw_repo.MemoryRepo(root)
            source = rcw_repo.MemoryRepository(repo)
            repo.checkouts[rcw_repo.key('H:\\r\\wt')] = 'b'
            for form in ('H:\\r\\wt', '/mnt/h/r/wt', 'h:/r/wt'):
                self.assertIs(source.fixture(form), repo)
            with self.assertRaises(AssertionError) as caught:
                source.fixture('/mnt/h/other')
            self.assertIn('/mnt/h/other', str(caught.exception))
            self.assertIn('/mnt/h/r/wt', str(caught.exception))

    def test_clean_tree_check_accepts_the_wsl_spelling(self):
        with tempfile.TemporaryDirectory() as root:
            repo = rcw_repo.MemoryRepo(root)
            source = rcw_repo.MemoryRepository(repo)
            for form in (str(repo.repo), placement.to_wsl_path(str(repo.repo))):
                self.assertTrue(source.reachable(form))
                self.assertEqual(source.dirty(form), '')
            repo.commit('x.txt')
            (repo.repo / 'y.txt').write_text('y\n')
            self.assertEqual(source.dirty(placement.to_wsl_path(str(repo.repo))), 'y.txt')


if __name__ == '__main__':
    unittest.main()
