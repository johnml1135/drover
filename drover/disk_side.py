"""One parent-authorized stage run in the cleanup worker. No ledger or coordinator lock here."""
from dataclasses import replace
from typing import BinaryIO

from . import placement, process
from .disk_budget import purge
from .disk_evidence import StageEvidence
from .disk_inventory import LIMIT, root_of
from .disk_paths import caches, outputs, report
from .disk_stage import StageClock, StageRequest, StageRun
from .disk_temp import temporary
from .disk_worktrees import settled

PROVEN = (2, 3, 4, 5)        # the stages that take a process proof before they start


def serve(wire: dict, *, out: BinaryIO, clock_factory=StageClock) -> None:
    """Run the stage `wire` asks for, writing its rows to `out` as lines. Clock, scope, evidence, proof, stage,
    `finish`: the clock starts first, so the process proof is inside the stage's time (section 3, item 4)."""
    request = StageRequest.from_wire(wire)
    # An owner-approved removal is never cut off, as today: its clock has no line.
    clock = clock_factory(request.policy.get('worker_timeout_s', 1800), exempt=bool(request.approved_path))
    policy, scope_errors = placement.cleanup_scope(request.drive['path'], request.policy)
    request = replace(request, policy=policy)
    run = StageRun(request, clock, out=out, root_of=root_of(policy, request.repos), limit=LIMIT)
    run.scope_errors.extend(scope_errors)
    run.evidence = StageEvidence(request, clock)
    if request.stage in PROVEN:
        run.evidence.processes()
    extra = STAGES.get(request.stage, _nothing)(run) or {}
    if not request.apply and request.stage < 6:
        run.evidence.inventory.save()
    for row in run.evidence.process_rows():
        run.add(row)
    run.finish(peak_intermediate_bytes=run.evidence.inventory.peak_intermediate_bytes, **extra)


def _nothing(run):
    return {}


def trim(run) -> None:
    """Stage 1: the VHDX report, and a trim when applied."""
    argv = placement.trim_argv()
    for row in placement.cleanup_vhdx_report():
        run.add(row)
    run.checkpoint()        # the report crosses before the trim's 120 s
    if run.request.apply and argv:
        done = process.run(process.Command(argv, 120))
        if done.kind != 'exited':
            run.error(dict(reason_code='trim_unavailable', error=done.detail))
        elif done.code:
            run.error(dict(reason_code='trim_unavailable', output=done.stderr[-2000:]))


def _outputs(run):
    """Stage 5: the budget purge when the request carries a budget, else the output stage."""
    return purge(run) if run.request.budget else outputs(run)


STAGES = {1: trim, 2: temporary, 3: caches, 4: settled, 5: _outputs, 6: report}
