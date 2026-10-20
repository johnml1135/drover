"""Atomic, snapshot-only conversion of legacy waiting episodes.

All associations are staged before evaluating a resolver. Legacy text is evidence,
never a runtime release predicate. No machine configuration or checkout is read.
"""
import json
import math
import re
import time
from datetime import datetime, timezone

from .. import contract
from . import ops, states
from .db import tx
from .waiting import (Waiting, cause_key, repo_key, TASK_TERMINAL, OWNER_ANSWER,
                      REPLAN, MANUAL, DEADLINE, BASE, OPEN, RESOLVED, AUTHORIZED)


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


def _holds(conn, report) -> None:
    rows = conn.execute('SELECT * FROM tasks WHERE blocker IS NOT NULL OR status=?', (states.BLOCKED,)).fetchall()
    # Capture every resume before requests can add their source subject.
    for row in rows:
        task = dict(row)
        raw = task['blocker']
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            payload = None
        prev = payload.get('prev') if isinstance(payload, dict) else None
        _subject(conn, ('task', task['id']), prev if prev in states.STATES else None)
    history = _events(conn)
    deleted = {e['data']['id'] for e in history if e['kind'] == 'task.deleted' and
               isinstance(e['data'], dict) and isinstance(e['data'].get('id'), str)}
    for row in rows:
        task = dict(row)
        tid, raw = task['id'], task['blocker']
        location, subject = f'tasks/{tid}/blocker', ('task', tid)
        if task['status'] in states.TERMINAL:
            report['retired'].append({'location': location, 'disposition': 'terminal-history'})
            conn.execute('UPDATE tasks SET blocker=NULL WHERE id=?', (tid,))
            continue
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError()
            prev = payload.get('prev', states.READY if task['approved_at'] else states.PROPOSED)
            if prev not in states.STATES:
                raise ValueError()
            holds = payload.get('holds', [payload])
            if not isinstance(holds, list) or not holds:
                raise ValueError()
        except (TypeError, ValueError):
            _diagnostic(conn, report, location, 'invalid-blocker', raw, [subject])
            conn.execute('UPDATE tasks SET blocker=NULL WHERE id=?', (tid,))
            continue
        if task['status'] != states.BLOCKED:
            if _boundary(conn, tid, 0):
                report['retired'].append({'location': location, 'disposition': 'stale-history'})
            else:
                _diagnostic(conn, report, location, 'inactive-blocker', raw, [subject])
            conn.execute('UPDATE tasks SET blocker=NULL WHERE id=?', (tid,))
            continue
        for n, hold in enumerate(holds):
            loc = location + (f'/holds/{n}' if 'holds' in payload else '')
            if not isinstance(hold, dict) or not isinstance(hold.get('reason'), str) or not isinstance(hold.get('source', 'manual'), str):
                _diagnostic(conn, report, loc, 'invalid-hold', raw, [subject])
                continue
            source, reason = hold.get('source', 'manual'), hold['reason']
            source_task = _task(conn, source)
            if reason == 'replan' and source_task and source_task['status'] not in states.TERMINAL:
                episodes = [e['id'] for e in history if
                            (e['kind'] == 'replan-request' and
                             (e['task_id'] == source or (source_task['epic_id'] and e['epic_id'] == source_task['epic_id']))) or
                            (e['kind'] == 'hold' and e['task_id'] == source and isinstance(e['data'], dict) and
                             e['data'].get('reason') == 'replan' and isinstance(e['data'].get('held'), list) and tid in e['data']['held'])]
                if episodes and _boundary(conn, source, max(episodes)):
                    report['retired'].append({'location': loc, 'disposition': 'stale-replan-hold'})
                    continue
                rid = _request(conn, report, source)
                _stage(conn, report, subject, REPLAN, cause_key(rid), cause_key('replan', rid),
                       {'request': rid, 'source': source, 'mode': 'blocked'})
            elif reason == 'urgent' and (source_task or conn.execute('SELECT 1 FROM epics WHERE id=?', (source,)).fetchone()):
                target = ('task' if source_task else 'epic', source)
                cause = cause_key('urgent', *target)
                _stage(conn, report, subject, OWNER_ANSWER, cause_key(cause), cause, {'mode': 'blocked'})
                w = Waiting(conn)
                if not conn.execute('SELECT question_id FROM wait_causes WHERE cause=?', (cause,)).fetchone()[0]:
                    qid = _question(conn, target, 'Resume work?', ['Resume work', 'Keep holding'])
                    conn.execute('UPDATE wait_causes SET question_id=? WHERE cause=?', (qid, cause))
                    report['converted']['questions'] += 1
            elif source == 'manual' and reason not in ('urgent', 'replan'):
                _stage(conn, report, subject, MANUAL, cause_key(tid), cause_key('manual', tid), {'note': reason, 'mode': 'blocked'})
            elif source_task or source in deleted:
                _stage(conn, report, subject, TASK_TERMINAL, cause_key(source), cause_key('task', source), {'source': source, 'mode': 'blocked'})
            else:
                _diagnostic(conn, report, loc, 'missing-source', raw, [subject])
                continue
            report['converted']['holds'] += 1
        conn.execute('UPDATE tasks SET blocker=NULL WHERE id=?', (tid,))


def _qid(value):
    if isinstance(value, str) and value.isdecimal() and int(value) > 0:
        return int(value)
    if type(value) is int and value > 0:
        return value
    raise ValueError('invalid question id')


def _link(conn, report, cause, qid, family, *, repo=None, task=None):
    q = ops.get_question(conn, _qid(qid))
    if task is not None and (q['target_kind'], q['target_id']) != ('task', task):
        raise ValueError('question target mismatch')
    if family == 'script' and q['target_kind'] != 'task':
        raise ValueError('script question requires a repo task')
    if repo is not None and q['target_kind'] == 'task':
        target = _task(conn, q['target_id'])
        if not target or repo_key(target['repo']) != repo:
            raise ValueError('question repo mismatch')
    labels = {o['label'] for o in q['options']}
    expected = {'origin': {'Retry origin sync'}, 'dirty': {'Retry landing'},
                'base': {'Retry landing', 'Keep holding'}, 'borrow': {'Approve borrowing', 'Keep default seat'}}
    if family in expected and (not labels or labels - expected[family]):
        raise ValueError('invalid legacy options')
    if family == 'script' and (labels or not q['allow_text']):
        raise ValueError('invalid script question')
    w = Waiting(conn)
    w._cause(cause)
    conn.execute('UPDATE wait_causes SET question_id=? WHERE cause=?', (qid, cause))
    report['converted']['questions'] += 1
    if q['status'] == 'answered':
        answers = [e for e in _events(conn, 'question-answered')
                   if isinstance(e['data'], dict) and e['data'].get('question') == qid]
        event = answers[-1] if answers else None
        if not event or event['actor'] != 'owner' or event['data'].get('choice') != q['answer_choice']:
            raise ValueError('unproven owner answer')
        answer = w._owner_answer(qid, q['answer_choice'], q['answer_text'], event['actor'])
        answer['event'] = event['id']
        phase = OPEN if answer['choice'] == 'Keep holding' else AUTHORIZED if family in ('origin', 'dirty', 'base') else RESOLVED
        # An attempted retry cannot be replayed as a fresh grant after migration.
        attempt_kinds = {'origin': ('origin-sync-failed', 'origin-synced'),
                         'dirty': ('landing-dirty', 'landing-clean', 'landing-batch'),
                         'base': ('base-red', 'base-ci', 'landing-batch')}
        if phase == AUTHORIZED and any(e['id'] > event['id'] and e['kind'] in attempt_kinds[family]
                                       and (not isinstance(e['data'], dict) or e['data'].get('repo', repo) == repo)
                                       for e in _events(conn)):
            phase = OPEN
        conn.execute('UPDATE wait_causes SET state=?,answer=?,accepted_event=? WHERE cause=?',
                     (phase, json.dumps(answer), event['id'], cause))
        if phase == OPEN:
            w._reopen(cause)
    return q


def _repo_subjects(conn, repo):
    return [('task', r['id']) for r in conn.execute('SELECT id,repo,status FROM tasks')
            if r['repo'] and repo_key(r['repo']) == repo and r['status'] == states.NEEDS_OWNER]


def _attach_owner(conn, report, cause, subject, mode='blocked'):
    _stage(conn, report, subject, OWNER_ANSWER, cause_key(cause), cause, {'mode': mode})


def _proposal_identity(conn, question):
    """Decode only documented legacy grammars, corroborated by ledger evidence."""
    if not isinstance(question['options'], list):
        return None, None
    labels = {o['label'] for o in question['options'] if isinstance(o, dict) and isinstance(o.get('label'), str)}
    grammars = (
        ('validate', r'Validate (.+) \(criteria ([^()]+)\): no rule baseline', {'Start validation', 'Not now'}),
        ('promote', r'Promote (.+) \(criteria ([^()]+)\) from shadow to live\?', {'Promote to live', 'Keep shadow'}),
        ('criteria', r'Review criteria text for (.+) \(criteria ([^()]+)\)\?', {'Run a Sonnet criteria review', 'Leave as is'}),
        ('pin', r'Pin a rule: stop routing (.+) to (.+)\?', {'Pin the rule', 'Keep routing'}),
        ('brief', r'Review the brief template for (.+) on (.+)\?', {'Run a Sonnet brief review', 'Leave as is'}))
    for family, pattern, options in grammars:
        match = re.fullmatch(pattern, question['title'])
        if not match or labels != options or len(question['options']) != len(options):
            continue
        first, second = match.groups()
        if family in ('validate', 'promote', 'criteria'):
            identity = [family, first, second]
            proven = conn.execute('SELECT 1 FROM decisions WHERE question_name=? AND criteria_version=?',
                                  (first, second)).fetchone()
            return (identity if proven else None), identity
        from .. import config
        backends = {backend for backend, ladder in config.DEFAULTS['models']['ladders'].items() if second in ladder}
        # Persisted run identities also cover models absent from built-in ladders.
        for run in conn.execute('SELECT r.role,r.backend,t.size FROM runs r JOIN tasks t ON t.id=r.task_id WHERE r.model=?', (second,)):
            route = run['role'] if run['role'] in ('review', 'director') else 'director' if run['role'] == 'planner' else 'worker:' + (run['size'] or 'M')
            if route == first:
                backends.add(run['backend'])
        subset = [family, first, None, second]
        return ([family, first, next(iter(backends)), second] if len(backends) == 1 else None), subset
    return None, None


def _tuner_questions(conn, report, epic):
    if not epic:
        return set()
    managed = set()
    for record in conn.execute("SELECT id FROM questions WHERE target_kind='epic' AND target_id=? ORDER BY id", (epic,)).fetchall():
        q = ops.get_question(conn, record['id'])
        managed.add(q['id'])
        identity, subset = _proposal_identity(conn, q)
        if identity is not None:
            cause = cause_key('tuner', *identity)
            conn.execute('SAVEPOINT tuner_link')
            try:
                _link(conn, report, cause, q['id'], 'tuner')
            except (ValueError, TypeError, KeyError):
                # Preserve a previously proven canonical proposal on invalid duplicate evidence.
                conn.execute('ROLLBACK TO tuner_link')
                conn.execute('RELEASE tuner_link')
            else:
                conn.execute('RELEASE tuner_link')
                continue
        cause = cause_key('tuner', 'legacy', q['id'])
        data = {'proposal_kind': 'legacy', 'identity': [str(q['id'])]}
        if subset:
            data['legacy_subset'] = subset
        conn.execute('INSERT OR IGNORE INTO wait_causes(cause,handler,state,data,question_id) VALUES(?,?,?,?,?)',
                     (cause, 'proposal', OPEN if q['status'] == 'open' else RESOLVED, json.dumps(data), q['id']))
        report['converted']['questions'] += 1
        diagnostic = {'location': 'questions/' + str(q['id']), 'code': 'legacy-proposal-identity',
                      'raw': {**q, **({'known_identity': subset} if subset else {})}, 'subjects': []}
        report['diagnostics'].append(diagnostic)
        _event(conn, 'waiting.migration-diagnostic', diagnostic)
    return managed


def _questions(conn, report) -> None:
    settings = dict(conn.execute('SELECT key,value FROM settings'))
    managed = set()
    # Resolve associations before their paired command evidence, independent of row insertion order.
    for key, raw in sorted(settings.items(), key=lambda item: item[0].startswith('gate_cmd_')):
        prefix = next((p for p in ('_gate_question_', '_origin_pause_', '_land_dirty_q_', 'gate_cmd_') if key.startswith(p)), None)
        if not prefix:
            continue
        if not conn.execute('SELECT 1 FROM settings WHERE key=?', (key,)).fetchone():
            continue
        repo = repo_key(key[len(prefix):])
        family = {'_gate_question_': 'script', '_origin_pause_': 'origin', '_land_dirty_q_': 'dirty', 'gate_cmd_': 'script'}[prefix]
        subjects = _repo_subjects(conn, repo)
        if not raw.strip():
            report['retired'].append({'location': 'settings/' + key, 'disposition': 'empty-tombstone'})
            conn.execute('DELETE FROM settings WHERE key=?', (key,))
            continue
        cause = cause_key(family, repo)
        try:
            if prefix == '_origin_pause_':
                value = json.loads(raw)
                if not isinstance(value, dict) or set(value) != {'question'}:
                    raise ValueError('invalid origin association')
                qid = _qid(value['question'])
            elif prefix == 'gate_cmd_':
                raise ValueError('command without question association')
            else:
                qid = _qid(raw)
            q = ops.get_question(conn, qid)
            managed.add(qid)
            cmd_key = 'gate_cmd_' + key[len(prefix):]
            if family == 'script' and cmd_key in settings and settings[cmd_key].strip():
                value = json.loads(settings[cmd_key])
                if not isinstance(value, dict) or set(value) != {'cmd', 'question', 'actor'} or value['question'] != qid or value['actor'] != 'owner' or value['cmd'] != q['answer_text'] or q['status'] != 'answered':
                    raise ValueError('untrusted script command')
            _link(conn, report, cause, qid, family, repo=repo)
            if family in ('origin', 'dirty'):
                _attach_owner(conn, report, cause, ('merge_queue', repo))
            if q['target_kind'] == 'task':
                target = _task(conn, q['target_id'])
                if target and target['status'] not in states.TERMINAL:
                    # Dirty queues leave reviewed tasks in their existing stage.
                    if family != 'dirty':
                        _attach_owner(conn, report, cause, ('task', target['id']), 'needs-owner')
                    subjects = [s for s in subjects if s[1] != target['id']]
            for subject in subjects:
                if conn.execute("SELECT 1 FROM questions WHERE target_kind='task' AND target_id=? AND status='open'", (subject[1],)).fetchone():
                    continue  # a separate structured question already explains this task's park
                _attach_owner(conn, report, cause, subject, 'blocked')
                _diagnostic(conn, report, 'tasks/' + subject[1] + '/blocker', 'ambiguous-question-association', raw, [subject])
        except (ValueError, TypeError, KeyError, ops.NotFound):
            # No invalid answer survives even when validation failed after linking.
            if Waiting(conn).state(cause) is not None:
                conn.execute('UPDATE wait_causes SET answer=NULL,accepted_event=NULL,state=? WHERE cause=?', (OPEN, cause))
                Waiting(conn)._reopen(cause)
            _diagnostic(conn, report, 'settings/' + key, 'invalid-question-association', raw,
                        subjects or [('merge_queue', repo)])
            if family == 'script':
                cmd_key = 'gate_cmd_' + key[len(prefix):]
                if cmd_key in settings and settings[cmd_key].strip():
                    _diagnostic(conn, report, 'settings/' + cmd_key, 'untrusted-command', settings[cmd_key], subjects)
        conn.execute('DELETE FROM settings WHERE key=?', (key,))
        if family == 'script':
            conn.execute('DELETE FROM settings WHERE key=?', ('gate_cmd_' + key[len(prefix):],))

    base_events = [e for e in _events(conn, 'base-red') if isinstance(e['data'], dict)]
    groups = {}
    for e in base_events:
        data = e['data']
        tasks = [_task(conn, tid) for tid in data.get('tasks', []) if isinstance(tid, str)] if isinstance(data.get('tasks', []), list) else []
        tasks = [t for t in tasks if t]
        try:
            q = ops.get_question(conn, _qid(data.get('question')))
            managed.add(q['id'])
            target = _task(conn, q['target_id']) if q['target_kind'] == 'task' else None
            repos = {repo_key(t['repo']) for t in tasks + ([target] if target else [])}
            sha = data.get('sha')
            import re
            if len(repos) != 1 or not isinstance(sha, str) or not re.fullmatch('[a-fA-F0-9]{40}', sha):
                raise ValueError('ambiguous base identity')
            groups.setdefault((repos.pop(), sha), []).append((e, q))
        except (ValueError, TypeError, ops.NotFound):
            _diagnostic(conn, report, 'task_events/' + str(e['id']), 'invalid-base-association', data,
                        [('merge_queue', repo_key(t['repo'])) for t in tasks])
    for (repo, sha), entries in groups.items():
        active_q = settings.get('landing_hold_question') if settings.get('landing_hold') == sha else None
        canonical = next((q for e, q in entries if str(q['id']) == active_q), None)
        canonical = canonical or max((q for e, q in entries), key=lambda q: (q['status'] == 'open', q['id']))
        cause = cause_key('base', repo, sha)
        try:
            _link(conn, report, cause, canonical['id'], 'base', repo=repo)
            live = any((t := _task(conn, tid)) and t['status'] not in states.TERMINAL
                       for e, q in entries for tid in e['data'].get('tasks', []))
            if live:
                _stage(conn, report, ('merge_queue', repo), BASE, cause_key(repo, sha), cause,
                       {'repo': repo, 'sha': sha, 'mode': 'blocked'})
            for e, q in entries:
                if q['id'] != canonical['id']:
                    conn.execute("UPDATE questions SET status='answered',answered_at=COALESCE(answered_at,?) WHERE id=?", (ops.stamp(), q['id']))
                    _event(conn, 'question-superseded', {'question': q['id'], 'canonical': canonical['id']})
        except (ValueError, ops.NotFound):
            _diagnostic(conn, report, 'questions/' + str(canonical['id']), 'invalid-base-question', canonical,
                        [('merge_queue', repo)])
    for key in ('landing_hold', 'landing_hold_question'):
        if key in settings:
            if settings[key].strip() and not groups:
                reviewed = [('merge_queue', repo_key(r[0])) for r in conn.execute('SELECT DISTINCT repo FROM tasks WHERE status=?', (states.REVIEWED,))]
                _diagnostic(conn, report, 'settings/' + key, 'unassociated-base-hold', settings[key], reviewed)
            conn.execute('DELETE FROM settings WHERE key=?', (key,))

    # Only current request episodes are executable. Generic run-finished is not a boundary.
    for e in _events(conn, 'replan-request'):
        tasks = [_task(conn, e['task_id'])] if e['task_id'] else [dict(t) for t in conn.execute('SELECT * FROM tasks WHERE epic_id=?', (e['epic_id'],))]
        for task in tasks:
            if not task or task['status'] in states.TERMINAL or _boundary(conn, task['id'], e['id']):
                report['retired'].append({'location': 'task_events/' + str(e['id']), 'disposition': 'request-history'})
                continue
            if not isinstance(e['data'], dict):
                _diagnostic(conn, report, 'task_events/' + str(e['id']), 'invalid-replan-request', e['data'], [('task', task['id'])])
                continue
            _request(conn, report, task['id'], e['id'])

    latest = {}
    for e in _events(conn, 'director-borrow'):
        if not isinstance(e['data'], dict) or not isinstance(e['data'].get('seat'), str):
            task = _task(conn, e['task_id'])
            current = task and task['status'] not in states.TERMINAL and not _boundary(conn, task['id'], e['id'])
            _diagnostic(conn, report, 'task_events/' + str(e['id']), 'invalid-borrow', e['data'],
                        [('task', task['id'])] if current else [])
            continue
        latest[(e['task_id'], e['data']['seat'])] = e
    # Backend seats are the stable existing director-seat names.
    seats = {'claude', 'codex', 'personal'}
    for (tid, seat), e in latest.items():
        task = _task(conn, tid)
        current = task and task['status'] not in states.TERMINAL and not _boundary(conn, tid, e['id'])
        try:
            if not task or e['actor'] != 'dispatcher' or seat not in seats:
                raise ValueError('invalid borrow identity')
            qid = _qid(e['data'].get('question'))
            managed.add(qid)
            cause = cause_key('borrow', tid, seat)
            q = _link(conn, report, cause, qid, 'borrow', task=tid)
            if current and q['status'] == 'open':
                _attach_owner(conn, report, cause, ('task', tid), 'needs-owner')
        except (ValueError, TypeError, ops.NotFound):
            _diagnostic(conn, report, 'task_events/' + str(e['id']), 'invalid-borrow', e['data'], [('task', tid)] if current else [])

    managed.update(_tuner_questions(conn, report, settings.get('tuner_epic')))
    for q in ops.open_questions(conn):
        if q['id'] in managed:
            continue
        if q['target_kind'] == 'task':
            task = _task(conn, q['target_id'])
            if task and task['status'] == states.NEEDS_OWNER:
                cause = cause_key('question', q['id'])
                _attach_owner(conn, report, cause, ('task', task['id']), 'needs-owner')
                conn.execute('UPDATE wait_causes SET question_id=? WHERE cause=?', (q['id'], cause))


def _epoch(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('timestamp requires timezone')
    return result.timestamp()


def _deadlines(conn, report) -> None:
    raw = ops.get_setting(conn, '_dispatch_backoff')
    if raw is not None:
        try:
            timers = json.loads(raw) if raw.strip() else {}
            if not isinstance(timers, dict):
                raise ValueError()
        except (TypeError, ValueError):
            _diagnostic(conn, report, 'settings/_dispatch_backoff', 'invalid-backoff', raw)
            timers = {}
        for tid, value in timers.items():
            task = _task(conn, tid)
            if not task or task['status'] != states.READY:
                report['retired'].append({'location': 'settings/_dispatch_backoff', 'disposition': 'obsolete-timer/' + tid})
                continue
            try:
                if not isinstance(value, dict) or set(value) != {'until', 'min'} or type(value['until']) not in (int, float) or not math.isfinite(value['until']) or type(value['min']) is not int or not 1 <= value['min'] <= 30:
                    raise ValueError()
                cause = cause_key('deadline', tid, 'worker', 'admission')
                _stage(conn, report, ('task', tid), DEADLINE, cause_key(tid, 'worker', 'admission'), cause,
                       {'role': 'worker', 'reason_code': 'admission', 'minutes': value['min'], 'mode': 'preserve'},
                       resume=states.READY, timeout=value['until'])
                conn.execute('UPDATE wait_subjects SET parked=0 WHERE subject_kind=? AND subject_key=?', ('task', tid))
                report['converted']['deadlines'] += 1
            except (ValueError, TypeError):
                _diagnostic(conn, report, 'settings/_dispatch_backoff', 'invalid-backoff-member', raw, [('task', tid)])
        conn.execute("DELETE FROM settings WHERE key='_dispatch_backoff'")
    for task in conn.execute('SELECT * FROM tasks WHERE status=?', (states.GATED,)).fetchall():
        run = conn.execute("SELECT * FROM runs WHERE task_id=? AND role='review' ORDER BY id DESC LIMIT 1", (task['id'],)).fetchone()
        if not run or run['outcome'] != contract.PROVIDER_ERROR or not run['ended']:
            continue
        tid = task['id']
        try:
            timeout = _epoch(run['ended']) + 300
            cause = cause_key('deadline', tid, 'review', 'provider')
            _stage(conn, report, ('task', tid), DEADLINE, cause_key(tid, 'review', 'provider'), cause,
                   {'role': 'review', 'reason_code': 'provider', 'minutes': 5, 'mode': 'preserve'}, resume=states.GATED, timeout=timeout)
            conn.execute('UPDATE wait_subjects SET parked=0 WHERE subject_kind=? AND subject_key=?', ('task', tid))
            report['converted']['deadlines'] += 1
        except (ValueError, TypeError):
            _diagnostic(conn, report, 'tasks/' + tid + '/blocker', 'invalid-review-time', dict(run), [('task', tid)])
    for row in conn.execute('SELECT * FROM base_gate_legacy').fetchall():
        row = dict(row)
        sha = row['sha']
        code = 'cache-unscoped'
        try:
            if not re.fullmatch('[a-fA-F0-9]{40}', sha) or row['status'] not in ('red', 'green'):
                raise ValueError()
            _epoch(row['at'])
            repos = set()
            for e in _events(conn, 'base-red'):
                if isinstance(e['data'], dict) and e['data'].get('sha') == sha:
                    repos.update(repo_key(t['repo']) for tid in e['data'].get('tasks', []) if (t := _task(conn, tid)))
            if len(repos) == 1:
                conn.execute('INSERT OR REPLACE INTO base_gate(repo,sha,status,at) VALUES(?,?,?,?)', (repos.pop(), sha, row['status'], row['at']))
                report['converted']['cache'] += 1
                continue
        except (ValueError, TypeError):
            code = 'cache-invalid'
        # A cache miss is safe; no owner wait is invented from an unscoped cache.
        item = {'location': 'base_gate/' + sha, 'code': code, 'raw': row, 'subjects': []}
        report['diagnostics'].append(item)
        _event(conn, 'waiting.migration-diagnostic', item)
    conn.execute('DROP TABLE base_gate_legacy')


def _finish(conn):
    w = Waiting(conn, 'migration')
    evidence = _events(conn)
    # A historical base episode cannot outlive its later typed resolution.
    for row in [r for r in w.pending() if r['kind'] == BASE]:
        repo, sha = row['data']['repo'], row['data']['sha']
        reds = [e['id'] for e in evidence if isinstance(e['data'], dict) and e['data'].get('sha') == sha and
                ((e['kind'] == 'base-ci' and e['data'].get('repo') == repo and e['data'].get('status') == 'red') or
                 (e['kind'] == 'base-red' and (e['data'].get('question') == row['question_id'] or
                  any((t := _task(conn, tid)) and repo_key(t['repo']) == repo
                      for tid in e['data'].get('tasks', []) if isinstance(tid, str)))))]
        later = [e for e in evidence if isinstance(e['data'], dict) and e['id'] > max(reds, default=0) and
                 e['data'].get('repo') == repo and
                 ((e['kind'] == 'base-observed' and isinstance(e['data'].get('sha'), str) and
                   re.fullmatch('[0-9a-fA-F]{40}', e['data']['sha']) and e['data']['sha'] != sha) or
                  (e['kind'] == 'base-ci' and e['data'].get('sha') == sha and e['data'].get('status') == 'green'))]
        if later:
            conn.execute('UPDATE wait_causes SET state=? WHERE cause=?', (RESOLVED, row['cause']))
            w._clear_cause(row['cause'], later[-1]['id'])
    for row in w.pending():
        if w._satisfied(row):
            w._release_rows([row], row['created_event'], reset=row['kind'] not in (MANUAL, DEADLINE, REPLAN))
    for subject in conn.execute('SELECT subject_kind,subject_key FROM wait_subjects').fetchall():
        if w.pending(tuple(subject)):
            w._park_subject(tuple(subject))
        elif subject[0] == 'task' and (task := _task(conn, subject[1])) and task['status'] == states.BLOCKED:
            eid = _event(conn, 'waiting.migration-restored', {'subject': list(subject)}, subject[1])
            w._restore_subject(tuple(subject), eid)


def migrate_waiting(conn) -> dict:
    report = {'converted': {name: 0 for name in ('holds', 'causes', 'questions', 'requests', 'deadlines', 'cache')},
              'retired': [], 'diagnostics': []}
    # Already converted databases must be logically immutable on repeated calls.
    if conn.execute('SELECT 1 FROM schema_version WHERE version=7').fetchone():
        return report
    with tx(conn):
        before_causes = conn.execute('SELECT count(*) FROM wait_causes').fetchone()[0]
        _holds(conn, report)
        _questions(conn, report)
        _deadlines(conn, report)
        _finish(conn)
        Waiting(conn, 'migration').expire(time.time())
        report['converted']['causes'] = conn.execute('SELECT count(*) FROM wait_causes').fetchone()[0] - before_causes
    return report


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


def main(argv=None) -> int:
    import argparse
    import hashlib
    import os
    import sqlite3
    from pathlib import Path
    from .db import connect, migrate
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--copy', required=True)
    parser.add_argument('--verify-copy', action='store_true')
    args = parser.parse_args(argv)
    source, destination = Path(args.source), Path(args.copy)
    src = dest = None
    try:
        if source.resolve() == destination.resolve() or destination.exists() or destination.is_symlink():
            raise ValueError('copy must be a fresh, distinct path')
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        src = sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True, isolation_level=None)
        src.row_factory = sqlite3.Row
        source_logical = _fingerprint(src)
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        dest = sqlite3.connect(destination, isolation_level=None)
        src.backup(dest)
        dest.close()
        dest = None
        src.close()
        src = None
        dest = connect(destination, migrate_db=False)
        reports = []
        # Capture the converter report while retaining the exact migration transaction.
        from unittest.mock import patch
        from . import waiting_migration
        converter = waiting_migration.migrate_waiting
        def capture(conn):
            report = converter(conn)
            reports.append(report)
            return report
        with patch.object(waiting_migration, 'migrate_waiting', side_effect=capture):
            migrate(dest)
        integrity = dest.execute('PRAGMA integrity_check').fetchone()[0]
        foreign_keys = [list(r) for r in dest.execute('PRAGMA foreign_key_check')]
        before = _fingerprint(dest)
        migrate(dest)
        if _fingerprint(dest) != before or integrity != 'ok' or foreign_keys:
            raise ValueError('copy failed integrity or idempotence checks')
        probes = _probes(dest) if args.verify_copy else []
        src = sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True, isolation_level=None)
        if hashlib.sha256(source.read_bytes()).hexdigest() != source_hash or _fingerprint(src) != source_logical:
            raise ValueError('source changed during rehearsal; supply an immutable backup')
        print(json.dumps({'source_sha256': source_hash, 'source_fingerprint': source_logical,
                          'copy_fingerprint': before, 'integrity_check': integrity, 'foreign_key_check': foreign_keys,
                          'idempotent': True, 'probes': probes, 'migration': reports}, sort_keys=True))
        return 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({'error': str(exc), 'copy': str(destination)}))
        return 2
    finally:
        if src is not None:
            src.close()
        if dest is not None:
            dest.close()


if __name__ == '__main__':
    raise SystemExit(main())
