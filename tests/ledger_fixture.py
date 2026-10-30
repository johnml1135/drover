"""Recorded engine evidence for tests that begin after a lifecycle stage."""
from drover import ci, contract
from drover.ledger import ops
from drover.ledger.ops_stages import advance_stage, land_stage

SHA = 'a' * 40


def claim(conn, task_id):
    task = ops.get_task(conn, task_id)
    if task['status'] != 'ready':
        raise ValueError('fixture claim requires ready')
    # Isolated ledger fixture: scope admission is exercised by claim_next tests.
    with ops.tx(conn):
        conn.execute("UPDATE tasks SET status='claimed' WHERE id=?", (task_id,))
        ops.event(conn, 'status', 'dispatcher', {'from': 'ready', 'to': 'claimed', 'reason': 'claim'}, task_id=task_id)


def stage(conn, task_id, target, sha=SHA):
    task = ops.get_task(conn, task_id)
    if task['status'] == 'ready':
        claim(conn, task_id)
    if target == 'claimed':
        return
    if ops.get_task(conn, task_id)['status'] == 'claimed':
        worker = ops.start_run(conn, task_id, 'worker', 'local')
        if target == 'running':
            return worker
    if ops.get_task(conn, task_id)['status'] == 'running':
        workers = [r for r in ops.list_runs(conn, task_id) if r['role'] == 'worker']
        worker = workers[-1]['id'] if workers and not workers[-1]['ended'] else ops.start_run(conn, task_id, 'worker', 'local')
        ops.event(conn, 'worker-done', 'dispatcher', {'run': worker, 'sha': sha}, task_id=task_id)
        ops.finish_run(conn, worker, 'done')
        gate = ops.start_run(conn, task_id, 'gate', 'local')
        ops.event(conn, 'gate-started', 'dispatcher', {'run': worker, 'gate_run': gate, 'sha': sha, 'platform': 'windows'}, task_id=task_id)
        ops.finish_run(conn, gate, 'passed')
        ops.amend_run(conn, worker, outcome='gated', gate_receipt=contract.gate_receipt(True, receipt={'sha': sha, 'status': 'passed'}))
        advance_stage(conn, task_id, 'gated', worker, sha, platforms=['windows'])
    if target == 'gated':
        return
    if ops.get_task(conn, task_id)['status'] == 'gated':
        worker = next(r for r in reversed(ops.list_runs(conn, task_id)) if r['role'] == 'worker')
        review = ops.start_run(conn, task_id, 'review', 'local')
        ops.event(conn, 'review-started', 'dispatcher', {'run': review, 'worker_run': worker['id'], 'sha': sha}, task_id=task_id)
        ops.finish_run(conn, review, 'land', review_verdict='LAND@' + sha)
        advance_stage(conn, task_id, 'reviewed', review, sha)
    if target == 'landed':
        tip = 'b' * 40
        land_stage(conn, {'id': task_id, 'head': sha, 'last_sha': tip}, tip,
                   {'version': ci.VERSION, 'passed': True, 'sha': tip, 'log': 'fixture'})
