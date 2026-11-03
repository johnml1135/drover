"""One stage run's evidence, held once.

Slice 5 lands this holding only the inventory, the listings, and the process proof with its report rows; slice 6a adds
the reads and the fresh proofs. A listing selects and a proof selects: neither authorises a deletion here.
"""
from . import git_worktrees, placement
from .disk_inventory import Inventory
from .disk_stage import StageClock, StageRequest


class StageEvidence:
    """What one stage run knows. `processes` binds at definition: a test passes its own, as
    `partial(StageEvidence, processes=fake)` in place of `disk_side.StageEvidence`."""

    def __init__(self, request: StageRequest, clock: StageClock, *, listings=git_worktrees.Listings,
                 prove=git_worktrees.prove, processes=placement.cleanup_processes):
        self._request = request
        self._inventory = Inventory(request.now, request.side or placement.cleanup_side(), request.drive['path'],
                                    request.state_dir, request.inventory_id)
        # Each lazy listing takes its patience when it starts, so the clamp to the worker timeout applies.
        self._listings = listings([machine['repo'] for machine in request.repos],
                                  timeout_s=lambda: clock.patience(git_worktrees.LISTING_TIMEOUT_S))
        self._prove = prove                  # slice 6a's fresh_registrations
        self._query = processes
        self._proof = None
        # Every proof's diagnostics, the first pass's and each fresh one's, by (pid, name), and its query failures.
        self._diagnostics, self._failures = {}, []

    @property
    def inventory(self) -> Inventory:
        return self._inventory

    def listed(self, repo: str) -> git_worktrees.Listing:
        """The first pass's listing of `repo`; its failure raised."""
        return self._listings.of(repo)

    def every_registered(self) -> tuple[tuple[str, ...], ...]:
        """cleanup_parts of every listed row in every repo; any repo's failure raised."""
        return self._listings.reach()

    def row(self, repo: str, path):
        """The listed row a removal is compared with; it never authorises one."""
        return self._listings.of(repo).row(path)

    def took(self, repo: str, path) -> None:
        """This stage's own `worktree remove` took this row."""
        self._listings.removed(repo, path)

    def repaired(self, repo: str, path) -> None:
        """This stage's own `reregister` added this pointer."""
        self._listings.repaired(repo, path)

    def processes(self):
        """The first pass's process proof: one snapshot per stage run."""
        if self._proof is None:
            request = self._request
            self._proof = self._noted(self._query(
                paths=request.policy, state_dir=request.state_dir, inventory_id=request.inventory_id,
                stage=request.stage, drive=request.drive, budget=bool(request.budget)))
        return self._proof

    def _noted(self, proof):
        _, unreadable, failure = placement.proof_diagnostics(proof)
        self._diagnostics.update(((row['pid'], row['name']), row) for row in unreadable)
        if failure:
            self._failures.append(failure)
        return proof

    def process_rows(self) -> list[dict]:
        """The report rows of the proofs taken: processes out of scope, unreadable ones, and failed queries. None
        before the first pass's proof."""
        if self._proof is None:
            return []
        rows = []
        foreign = placement.proof_diagnostics(self._proof)[0]
        if foreign:
            rows.append(dict(action='report', reason_code='process_out_of_scope', count=foreign))
        unreadable = list(self._diagnostics.values())
        if unreadable:
            rows.append(dict(action='report', reason_code='process_visibility_unavailable', count=len(unreadable),
                             processes=unreadable))
        rows.extend(dict(action='report', **failure) for failure in self._failures)
        return rows
