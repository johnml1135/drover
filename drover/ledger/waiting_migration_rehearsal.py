"""Rehearsal inspection helpers for the waiting migration."""
import json

from . import ops, states
from .waiting import Waiting, TASK_TERMINAL, OWNER_ANSWER, REPLAN, MANUAL, DEADLINE, BASE
from .waiting_migration_common import _task


def _logical(conn):
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    return {name: sorted([list(r) for r in conn.execute('SELECT * FROM "' + name.replace('"', '""') + '"')], key=repr) for name in tables}


def _fingerprint(conn):
    import hashlib
    return hashlib.sha256(json.dumps(_logical(conn), sort_keys=True).encode()).hexdigest()


def _probes(conn):
    results = []
    w = Waiting(conn, 'owner')
    representatives = {}
    for row in w.pending():
        representatives.setdefault(row['kind'], row)
    for kind, row in representatives.items():
        before = _fingerprint(conn)
        conn.execute('SAVEPOINT release_probe')
        try:
            if kind == TASK_TERMINAL:
                task = _task(conn, row['data']['source'])
                if task and task['status'] not in states.TERMINAL:
                    ops.set_status(conn, task['id'], states.CANCELLED, actor='owner')
            elif kind == MANUAL:
                w.release(row['cause'])
            elif kind == BASE:
                ops.event(conn, 'base-ci', 'owner', {**{k: row['data'][k] for k in ('repo', 'sha')}, 'status': 'green'})
            elif kind == DEADLINE:
                w.expire(row['timeout_at'])
            elif kind == REPLAN:
                w._request_done(row['data']['request'], 'owner-resolved')
            elif kind == OWNER_ANSWER:
                q = ops.get_question(conn, row['question_id'])
                labels = [o['label'] for o in q['options']]
                choice = next((x for x in labels if x != 'Keep holding'), None)
                text = '.drover/scripts/rehearsal.ps1' if json.loads(row['cause'])[0] == 'script' else 'Acknowledge migration rehearsal'
                ops.answer_question(conn, q['id'], choice=choice, text=None if choice else text)
            if conn.execute('SELECT released_event FROM waits WHERE id=?', (row['id'],)).fetchone()[0] is None:
                raise ValueError('release probe did not resolve ' + kind)
            results.append({'kind': kind, 'released': True})
        finally:
            conn.execute('ROLLBACK TO release_probe')
            conn.execute('RELEASE release_probe')
        if _fingerprint(conn) != before:
            raise ValueError('release rehearsal changed the copy')
    return results
