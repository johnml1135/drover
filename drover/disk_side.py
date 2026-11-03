"""One parent-authorized stage run in the cleanup worker. No ledger or coordinator lock here."""
from dataclasses import replace
from typing import BinaryIO

from . import placement, process
from .disk_budget import purge
from .disk_evidence import StageEvidence
from .disk_inventory import LIMIT, root_of
from .disk_paths import caches, outputs, report, stage_dict
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


def _trim(request):
    """Stage 1: the VHDX report, and a trim when applied."""
    argv = placement.trim_argv()
    result = dict(items=placement.cleanup_vhdx_report(), errors=[], reclaimed_bytes=0)
    if request['apply'] and argv:
        done = process.run(process.Command(argv, 120))
        if done.kind != 'exited':
            result['errors'].append(dict(reason_code='trim_unavailable', error=done.detail))
        elif done.code:
            result['errors'].append(dict(reason_code='trim_unavailable', output=done.stderr[-2000:]))
    return result


def _outputs(run):
    """Stage 5: the budget purge when the request carries a budget, else the output stage."""
    return purge(run) if run.request.budget else outputs(run)


def _bridge(run, stage_fn):
    """Temporary, until Task 35: today's request dict built from `run`, today's stage on it, and its rows added. The
    dict holds the evidence's own listings and diagnostics, so a fresh proof taken at removal still reports its
    unreadable processes."""
    answer = stage_fn(stage_dict(run, proven=run.request.stage in PROVEN))
    for row in answer['items']:
        run.add(row)
    for row in answer['errors']:
        run.error(row)
    run.reclaimed(answer['reclaimed_bytes'])
    run.checkpoint()


STAGES = {
    1: lambda run: _bridge(run, _trim),
    2: lambda run: _bridge(run, temporary),
    3: lambda run: _bridge(run, caches),
    4: settled,
    5: _outputs,
    6: lambda run: _bridge(run, report),
}
