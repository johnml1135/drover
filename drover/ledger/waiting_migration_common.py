"""Shared helpers for the waiting migration."""
import json

from . import ops, states
from .waiting import Waiting, cause_key, OWNER_ANSWER, REPLAN


def _event(conn, kind, data, task=None):
    # Deliberately bypass event dispatch until the entire inventory has been staged.
    return conn.execute('INSERT INTO task_events(kind,actor,data,task_id,at) VALUES(?,?,?,?,?)',
                        (kind, 'migration', json.dumps(data), task, ops.stamp())).lastrowid


def _events(conn, kind=None):
    sql = 'SELECT * FROM task_events'
    rows = conn.execute(sql + (' WHERE kind=?' if kind else '') + ' ORDER BY id', (kind,) if kind else ())
    result = []
    for row in rows:
        row = dict(row)
        try:
            row['data'] = json.loads(row['data'])
        except (ValueError, TypeError):
            row['data'] = None
        result.append(row)
    return result


def _task(conn, tid):
    if not isinstance(tid, str):
        return None
    row = conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()
    return dict(row) if row else None


def _subject(conn, subject, resume=None):
    task = _task(conn, subject[1]) if subject[0] == 'task' else None
    if task:
        resume = resume or (states.PROPOSED if not task['approved_at'] else states.READY)
    conn.execute('INSERT OR IGNORE INTO wait_subjects(subject_kind,subject_key,resume_state,parked) VALUES(?,?,?,?)',
                 (*subject, resume, int(task is not None)))


def _stage(conn, report, subject, kind, key, cause, data, *, resume=None, timeout=None):
    _subject(conn, subject, resume)
    w = Waiting(conn, 'migration')
    fresh = w.state(cause) is None
    w._cause(cause)
    if fresh:
        report['converted']['causes'] += 1
    if conn.execute('SELECT 1 FROM waits WHERE subject_kind=? AND subject_key=? AND kind=? AND key=? AND cause=? AND released_event IS NULL',
                    (*subject, kind, key, cause)).fetchone():
        return
    eid = _event(conn, 'waiting.registered', {'subject': list(subject), 'kind': kind, 'key': key, 'cause': cause},
                 subject[1] if subject[0] == 'task' else None)
    conn.execute('INSERT INTO waits(subject_kind,subject_key,kind,key,cause,data,timeout_at,created_event) VALUES(?,?,?,?,?,?,?,?)',
                 (*subject, kind, key, cause, json.dumps(data), timeout, eid))


def _question(conn, target, title, options=()):
    return conn.execute('INSERT INTO questions(target_kind,target_id,title,body,options,allow_text,asked,status) VALUES(?,?,?,?,?,?,?,?)',
                        (*target, title, '', json.dumps([{'label': x, 'description': '', 'recommended': False} for x in options]),
                         int(not options), ops.stamp(), 'open')).lastrowid


def _alert(conn):
    row = conn.execute("SELECT id FROM epics WHERE title='Waiting migration diagnostics' ORDER BY id LIMIT 1").fetchone()
    if row:
        return ('epic', row[0])
    # Migration-local epic creation does not invoke production lifecycle operations.
    import uuid
    eid = 'E-' + uuid.uuid4().hex[:8]
    conn.execute('INSERT INTO epics(id,repo,title,rank,created,updated) VALUES(?,?,?,?,?,?)',
                 (eid, '', 'Waiting migration diagnostics', 0, ops.stamp(), ops.stamp()))
    return ('epic', eid)


def _diagnostic(conn, report, location, code, raw, subjects=()) -> None:
    subjects = list(dict.fromkeys(tuple(s) for s in subjects))
    item = {'location': location, 'code': code, 'raw': raw, 'subjects': [list(s) for s in subjects]}
    report['diagnostics'].append(item)
    _event(conn, 'waiting.migration-diagnostic', item)
    target = next((s for s in subjects if s[0] == 'task' and _task(conn, s[1])), None) or _alert(conn)
    qid = _question(conn, target, 'Acknowledge legacy waiting state: ' + location)
    report['converted']['questions'] += 1
    cause = cause_key('question', qid)
    for subject in subjects:
        task = _task(conn, subject[1]) if subject[0] == 'task' else None
        if subject[0] == 'task' and (not task or task['status'] in states.TERMINAL):
            continue
        _stage(conn, report, subject, OWNER_ANSWER, cause_key(cause), cause, {'mode': 'needs-owner' if task else 'blocked'})
    Waiting(conn)._cause(cause)
    conn.execute('UPDATE wait_causes SET question_id=? WHERE cause=?', (qid, cause))


def _request(conn, report, source, eid=None):
    existing = conn.execute("SELECT id FROM replan_requests WHERE task_id=? AND state<>'done'", (source,)).fetchone()
    if existing:
        return existing[0]
    eid = eid or _event(conn, 'replan-request', {'trigger': 'legacy-blocker'}, source)
    rid = conn.execute('INSERT INTO replan_requests(task_id,requesting_event) VALUES(?,?)', (source, eid)).lastrowid
    report['converted']['requests'] += 1
    _stage(conn, report, ('task', source), REPLAN, cause_key(rid), cause_key('replan', rid),
           {'request': rid, 'source': source, 'mode': 'blocked'})
    return rid


def _boundary(conn, tid, after):
    for row in conn.execute(
            "SELECT kind,actor,data FROM task_events WHERE task_id=? AND id>? AND kind IN ('status','replan-completed')",
            (tid, after)):
        if row['kind'] == 'replan-completed':
            return True
        try:
            data = json.loads(row['data'])
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and (data.get('to') in states.TERMINAL or
                (data.get('to') == states.READY and row['actor'] in ('owner', 'director'))):
            return True
    return False
