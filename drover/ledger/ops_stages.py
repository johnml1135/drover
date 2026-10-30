"""Evidence-checked engine stage transitions."""
import re

from .. import ci, contract
from . import states
from .ops_common import events, tx
from .ops_tasks import _set_status, get_task


def read_only_result(conn, task):
    """The latest run's findings, when they are eligible for owner acceptance."""
    if task['write'] or task['status'] != 'needs-owner':
        return None
    run = conn.execute("SELECT * FROM runs WHERE task_id=? ORDER BY id DESC LIMIT 1", (task['id'],)).fetchone()
    if run and run['role'] == 'worker' and run['ended'] and run['outcome'] == 'read-only-done':
        return dict(run)
    return None


def accept_read_only(conn, task_id, actor='owner'):
    """Complete accepted findings without a commit, gate or landing queue entry."""
    if actor != 'owner':
        raise PermissionError('only the owner may accept read-only findings')
    with tx(conn):
        task = get_task(conn, task_id)
        run = read_only_result(conn, task)
        if run is None:
            raise ValueError('task has no completed read-only findings to accept')
        return _set_status(conn, task_id, 'landed', actor, reason='Owner accepted read-only findings',
                          data={'read_only': True, 'run': run['id']})


def start_running(conn, task_id, run_id, actor):
    with tx(conn):
        run = conn.execute("SELECT role, ended FROM runs WHERE id=? AND task_id=?", (run_id, task_id)).fetchone()
        if not run or run['role'] != 'worker' or run['ended']:
            raise ValueError('running stage requires an open worker run')
        return _set_status(conn, task_id, states.RUNNING, actor, reason='run started', data={'run': run_id})


def resume_gate(conn, task_id, run_id, actor='dispatcher'):
    """Owner-approved gate retry: put a ready task back to running on its finished worker run."""
    with tx(conn):
        runs = conn.execute("SELECT id, role, outcome, ended FROM runs WHERE task_id=? ORDER BY id", (task_id,)).fetchall()
        last = next((r for r in reversed(runs) if r['role'] != 'gate'), None)
        if (get_task(conn, task_id)['status'] != states.READY or not last or last['id'] != run_id
                or last['role'] != 'worker' or last['outcome'] != contract.DONE
                or any(r['ended'] is None or (r['role'] == 'gate' and r['id'] > run_id and r['outcome'] == 'passed') for r in runs)):
            raise ValueError('gate resume requires a ready task whose latest worker run is done, with no passed gate, and idle')
        _set_status(conn, task_id, states.CLAIMED, actor, reason='owner continued gate recovery')
        return _set_status(conn, task_id, states.RUNNING, actor, reason='resume gate for accepted worker output', data={'run': run_id})


def advance_stage(conn, task_id, stage, run_id, sha, actor='dispatcher', platforms=None):
    if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise ValueError('stage requires an exact commit SHA')
    with tx(conn):
        task = get_task(conn, task_id)
        run = conn.execute('SELECT * FROM runs WHERE id=? AND task_id=?', (run_id, task_id)).fetchone()
        if not run or not run['ended']:
            raise ValueError('stage requires a finished run for this task')
        if stage == states.GATED:
            receipt = contract.parse_gate_receipt(run['gate_receipt']) or {}
            summary = receipt.get('receipt') or {}
            starts = [e['data'] for e in events(conn, task_id=task_id, kind='gate-started')]
            passed = {e.get('platform') for e in starts if e.get('run') == run_id and e.get('sha') == sha
                      and conn.execute("SELECT 1 FROM runs WHERE id=? AND task_id=? AND role='gate' AND ended IS NOT NULL AND outcome='passed'", (e.get('gate_run'), task_id)).fetchone()}
            if not platforms or not set(platforms) <= passed:
                raise ValueError('gate stage requires passed runs for every required platform')
            done = any(e['data'].get('run') == run_id and e['data'].get('sha') == sha
                       for e in events(conn, task_id=task_id, kind='worker-done'))
            if (run['role'] != 'worker' or run['outcome'] != 'gated' or not receipt.get('ok')
                    or summary.get('sha') != sha or summary.get('status') != 'passed' or not done):
                raise ValueError('gate stage requires a passed gate run and matching receipt')
        elif stage == states.REVIEWED:
            starts = [e['data'] for e in events(conn, task_id=task_id, kind='review-started')]
            start = next((e for e in reversed(starts) if e.get('run') == run_id), {})
            worker = conn.execute('SELECT * FROM runs WHERE id=? AND task_id=?', (start.get('worker_run'), task_id)).fetchone()
            receipt = contract.parse_gate_receipt(worker['gate_receipt']) if worker else None
            if (run['role'] != 'review' or run['outcome'] != contract.LAND or run['review_verdict'] != 'LAND@' + sha
                    or start.get('sha') != sha or not worker or worker['outcome'] != 'gated'
                    or not receipt or not receipt.get('ok') or (receipt.get('receipt') or {}).get('sha') != sha):
                raise ValueError('review stage requires a matching gated commit and LAND verdict')
        else:
            raise ValueError('unsupported engine stage')
        reason = 'gate passed' if stage == states.GATED else f'review LAND@{sha[:12]}'
        return _set_status(conn, task_id, stage, actor, reason=reason, data={'run': run_id, 'sha': sha})


def land_stage(conn, task, tip, receipt):
    from .landing import record_merged
    from ..landing_batch import review_landed
    with tx(conn):
        current = get_task(conn, task['id'])
        if (current['status'] != states.REVIEWED or not review_landed(conn, task['id'], task['head'])
                or not re.fullmatch(r'[0-9a-f]{40}', tip or '') or not re.fullmatch(r'[0-9a-f]{40}', task['last_sha'] or '')
                or receipt.get('sha') != tip or ci.status_problems(receipt)):
            raise ValueError('landing requires an exact review and passed merge receipt')
        result = _set_status(conn, task['id'], states.LANDED, 'system', reason='fast-forwarded',
                             data={'land_sha': task['last_sha'], 'tip': tip, 'receipt': receipt.get('log')})
        record_merged(conn, task, tip, receipt.get('log'))
        return result
