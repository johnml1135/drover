"""Storage for merge groups and their CI results. Tables are created by migrations."""
import json
import time
from . import ops
from .db import tx

LOCK_TTL_S = 900


class LockHeld(RuntimeError):
    pass


def take_lock(conn, holder, at=None):
    at = time.time() if at is None else at
    with tx(conn):
        row = conn.execute("SELECT * FROM landing_lock WHERE id=1").fetchone()
        if row and at - row['since'] < LOCK_TTL_S:
            raise LockHeld(f'refused: landing lock held by {row["holder"]}')
        conn.execute("INSERT INTO landing_lock(id,holder,since) VALUES(1,?,?) "
                     "ON CONFLICT(id) DO UPDATE SET holder=excluded.holder, since=excluded.since", (holder, at))


def drop_lock(conn, holder):
    """Release the lock only if `holder` still owns it."""
    conn.execute("DELETE FROM landing_lock WHERE id=1 AND holder=?", (holder,))


def record_batch(conn, r):
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO landing_batches(at,tasks,outcome,tip,gates,gate_s,red,bisect_gates,decisions,dropped,detail) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (ops.stamp(), json.dumps(r['batch']), r['outcome'], r['tip'], r['gates'], json.dumps(r['gate_s']),
             r['red'], r['bisect_gates'], r['decisions'], json.dumps(r['dropped']), json.dumps(r.get('detail', {}))))
        r['batch_id'] = cur.lastrowid
        conn.execute("UPDATE task_landed SET batch_id=? WHERE task_id IN (%s)" % ','.join('?' * len(r['landed'])),
                     (cur.lastrowid, *r['landed'])) if r['landed'] else None
        for task_id in r['batch']:
            ops.event(conn, 'landing.outcome', 'landing',
                      {'batch_id': r['batch_id'], 'outcome': r['outcome'],
                       'reason': r.get('detail', {}).get('reason') or r.get('error') or r['outcome'],
                       'detail': r.get('detail', {})}, task_id=task_id)



def land_sha(conn, task_id):
    row = conn.execute("SELECT land_sha FROM task_landed WHERE task_id=?", (task_id,)).fetchone()
    return row[0] if row else None



def policy_data(conn, version):
    row = conn.execute("SELECT data FROM policy_versions WHERE version=?", (version,)).fetchone() if version else None
    return json.loads(row[0]) if row else {}


def batches(conn):
    return [dict(row) for row in conn.execute("SELECT * FROM landing_batches ORDER BY id")]


def last_outcome(conn):
    row = conn.execute("SELECT outcome FROM landing_batches ORDER BY id DESC LIMIT 1").fetchone()
    return row[0] if row else None


def last_detail(conn):
    row = conn.execute('SELECT detail FROM landing_batches ORDER BY id DESC LIMIT 1').fetchone()
    return json.loads(row[0]) if row else {}


def forget_base(conn, repo, sha):
    with tx(conn):
        conn.execute("DELETE FROM base_gate WHERE repo=? AND sha=?", (repo, sha))
        conn.execute("DELETE FROM base_diagnoses WHERE repo=? AND sha=?", (repo, sha))


def base_status(conn, repo, sha, platform=None, signature=None):
    if platform is not None:
        row = conn.execute('SELECT status FROM base_diagnoses WHERE repo=? AND sha=? AND platform=? AND signature=?',
                           (repo, sha, platform, signature or '')).fetchone()
        return row['status'] if row else None
    row = conn.execute("SELECT status FROM base_gate WHERE repo=? AND sha=?", (repo, sha)).fetchone()
    return row['status'] if row else None


def record_base(conn, repo, sha, status, platform=None, signature=None):
    if status not in ('red', 'green'):
        raise ValueError('invalid base cache status')
    with tx(conn):
        if platform is not None:
            conn.execute('INSERT INTO base_diagnoses(repo,sha,platform,signature,status,at) VALUES(?,?,?,?,?,?) '
                         'ON CONFLICT(repo,sha,platform,signature) DO UPDATE SET status=excluded.status,at=excluded.at',
                         (repo, sha, platform, signature or '', status, ops.stamp()))
            # One platform's green result does not certify the whole base.
            ops.event(conn, 'base-diagnosis', 'landing',
                      dict(repo=repo, sha=sha, platform=platform, signature=signature, status=status))
            return
        ops.event(conn, 'base-ci', 'landing', {'repo': repo, 'sha': sha, 'status': status})


def record_merged(conn, task, tip, receipt):
    with tx(conn):
        previous = conn.execute('SELECT land_sha,tip,receipt FROM task_landed WHERE task_id=?', (task['id'],)).fetchone()
        conn.execute("INSERT OR REPLACE INTO task_landed(task_id,land_sha,tip,receipt,at) VALUES(?,?,?,?,?)",
                     (task['id'], task['last_sha'], tip, receipt, ops.stamp()))
        if (previous is None or tuple(previous) != (task['last_sha'], tip, receipt)) and conn.execute(
                'SELECT 1 FROM tasks WHERE id=?', (task['id'],)).fetchone():
            ops.event(conn, 'task.landed', 'landing',
                      {'land_sha': task['last_sha'], 'tip': tip, 'receipt': receipt}, task_id=task['id'])
        from ..notifications.groups import changed
        changed(conn, task['id'])
