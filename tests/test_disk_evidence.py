"""One stage run's evidence, held once: the inventory, the listings with the stage clock's patience, and the process
proof with its report rows."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from drover import git_worktrees, placement_paths
from drover.disk_evidence import StageEvidence
from drover.disk_inventory import Inventory
from drover.disk_stage import StageClock, StageRequest
from drover.git_worktrees import Listing, RegistrationsUnknown


def request(**changes):
    value = dict(stage=4, side='wsl', mode=None, apply=False, now=100.0, drive=dict(path='/mnt/h'),
                 repos=(dict(repo='/r/one'), dict(repo='/r/two')), policy=dict(worker_timeout_s=1800),
                 state_dir='/state', tasks={}, inventory_id='inv')
    value.update(changes)
    return StageRequest(**value)


def clock_at(times):
    """A StageClock started at times[0]; the test moves time by appending."""
    return StageClock(1800, clock=lambda: times[-1])


class Listings(unittest.TestCase):
    def test_listings_take_patience_when_each_listing_starts(self):
        """3M3: worker timeout 1800 and the listing's 400 s cap: 240 s at 1200 s elapsed, 10 s at the 80% line."""
        times, asked = [0.0], []

        def listing(repo, *, timeout_s, side=None):
            asked.append((repo, timeout_s))
            return Listing(repo, ())

        evidence = StageEvidence(request(), clock_at(times), processes=None)
        with patch.object(git_worktrees, 'listing', listing):
            times.append(1200.0)
            evidence.listed('/r/one')
            times.append(1440.0)
            evidence.listed('/r/two')
            evidence.listed('/r/one')                        # listed once: its first patience stands
        self.assertEqual(asked, [('/r/one', 240.0), ('/r/two', 10.0)])

    def test_listing_failure_is_raised_from_listed(self):
        def listing(repo, **_):
            if repo == '/r/two':
                raise RegistrationsUnknown('fatal: not a git repository')
            return Listing(repo, (dict(worktree='/w/a'),))

        evidence = StageEvidence(request(), clock_at([0.0]), processes=None)
        with patch.object(git_worktrees, 'listing', listing):
            self.assertEqual(evidence.row('/r/one', '/w/a'), dict(worktree='/w/a'))
            with self.assertRaisesRegex(RegistrationsUnknown, 'not a git repository'):
                evidence.listed('/r/two')
            with self.assertRaises(RegistrationsUnknown):
                evidence.every_registered()

    def test_took_and_repaired_reach_the_one_listing(self):
        evidence = StageEvidence(request(), clock_at([0.0]), processes=None)
        with patch.object(git_worktrees, 'listing', lambda repo, **_: Listing(repo, (dict(worktree=repo + '/w'),))):
            self.assertEqual(evidence.every_registered(),
                             tuple(placement_paths.cleanup_parts(p) for p in ('/r/one/w', '/r/two/w')))
            evidence.took('/r/one', '/r/one/w')
            evidence.repaired('/r/two', '/r/two/p')
            self.assertIsNone(evidence.row('/r/one', '/r/one/w'))
            self.assertEqual(len(evidence.listed('/r/one').rows), 0)


class Processes(unittest.TestCase):
    def test_processes_is_one_snapshot_and_process_rows_match_disk_side_21_32(self):
        """The rows today's disk_side.serve appended after compact (disk_side.py:21-32 at e09ef3e)."""
        calls = []
        unreadable = [dict(pid=7, name='a', side='wsl'), dict(pid=8, name='b', side='wsl')]
        failure = dict(reason_code='process_query_failed', side='windows', error='timed out')

        def processes(**options):
            calls.append(options)
            return SimpleNamespace(out_of_scope=3, unreadable=unreadable, query_failure=failure)

        evidence = StageEvidence(request(budget=dict(target_gb=5)), clock_at([0.0]), processes=processes)
        self.assertEqual(evidence.process_rows(), [])                 # no proof taken, no rows
        proof = evidence.processes()
        self.assertIs(evidence.processes(), proof)
        self.assertEqual(calls, [dict(paths=dict(worker_timeout_s=1800), state_dir='/state', inventory_id='inv',
                                      stage=4, drive=dict(path='/mnt/h'), budget=True)])
        self.assertEqual(evidence.process_rows(), [
            dict(action='report', reason_code='process_out_of_scope', count=3),
            dict(action='report', reason_code='process_visibility_unavailable', count=2, processes=unreadable),
            dict(action='report', **failure)])

    def test_a_proof_with_nothing_to_say_adds_no_rows(self):
        evidence = StageEvidence(request(), clock_at([0.0]), processes=lambda **_: SimpleNamespace(busy=lambda p: False))
        evidence.processes()
        self.assertEqual(evidence.process_rows(), [])


class Inventories(unittest.TestCase):
    def test_inventory_is_read_only(self):
        evidence = StageEvidence(request(inventory_id=None), clock_at([0.0]), processes=None)
        self.assertIsInstance(evidence.inventory, Inventory)
        self.assertEqual(evidence.inventory.now, 100.0)
        with self.assertRaises(AttributeError):
            evidence.inventory = Inventory(0)


if __name__ == '__main__':
    unittest.main()
