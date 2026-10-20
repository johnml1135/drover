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
            "INSERT INTO landing_batches(at,tasks,outcome,tip,gates,gate_s,red,bisect_gates,decisions,dropped) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (ops.stamp(), json.dumps(r['batch']), r['outcome'], r['tip'], r['gates'], json.dumps(r['gate_s']),
             r['red'], r['bisect_gates'], r['decisions'], json.dumps(r['dropped'])))
        r['batch_id'] = cur.lastrowid
        conn.execute("UPDATE task_landed SET batch_id=? WHERE task_id IN (%s)" % ','.join('?' * len(r['landed'])),
                     (cur.lastrowid, *r['landed'])) if r['landed'] else None


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


def forget_base(conn, repo, sha):
    conn.execute("DELETE FROM base_gate WHERE repo=? AND sha=?", (repo, sha))


def base_status(conn, repo, sha):
    row = conn.execute("SELECT status FROM base_gate WHERE repo=? AND sha=?", (repo, sha)).fetchone()
    return row['status'] if row else None


def record_base(conn, repo, sha, status):
    if status not in ('red', 'green'):
        raise ValueError('invalid base cache status')
    with tx(conn):
        ops.event(conn, 'base-ci', 'landing', {'repo': repo, 'sha': sha, 'status': status})


def record_merged(conn, task, tip, receipt):
    conn.execute("INSERT OR REPLACE INTO task_landed(task_id,land_sha,tip,receipt,at) VALUES(?,?,?,?,?)",
                 (task['id'], task['last_sha'], tip, receipt, ops.stamp()))
