"""Legacy question associations for the waiting migration."""
import json
import re

from . import ops, states
from .waiting import Waiting, cause_key, repo_key, OWNER_ANSWER, BASE, OPEN, RESOLVED, AUTHORIZED
from .waiting_migration_common import _event, _events, _task, _stage, _diagnostic, _request, _boundary


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
