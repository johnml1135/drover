"""Durable waits and their event-driven release, in the ledger transaction."""
import json
import math
import re
import time
import uuid

from .. import contract, placement
from . import states
from .db import tx

TASK_TERMINAL = 'task-terminal'
OWNER_ANSWER = 'owner-answer'
BASE = 'base'
REPLAN = 'replan'
DEADLINE = 'deadline'
MANUAL = 'manual'
OPEN, AUTHORIZED, ATTEMPTING, RESOLVED, CLOSED = 'open', 'authorized', 'attempting', 'resolved', 'closed'
PROCESS_SESSION = uuid.uuid4().hex

QUEUED, RUNNING, OWNER, DONE = 'queued', 'running', 'owner', 'done'


def cause_key(*parts: str | int) -> str:
    if not parts or any(type(p) not in (str, int) or p == '' for p in parts):
        raise ValueError('identity requires nonempty string/integer parts')
    return json.dumps(parts, ensure_ascii=True, separators=(',', ':'))


def _identity(key):
    try:
        parts = json.loads(key)
        if not isinstance(parts, list) or cause_key(*parts) != key:
            raise ValueError('noncanonical identity')
        return parts
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError('invalid identity') from exc


def repo_key(name, checkout=None) -> str:
    if name and name.strip():
        return name.strip().casefold()
    if checkout is None:
        raise ValueError('unnamed repository needs a checkout')
    return cause_key(*placement.path_parts(placement.config_path(checkout, checkout)))


class Waiting:
    def __init__(self, conn, actor="system"):
        self.conn, self.actor = conn, actor

    def _cause(self, cause, data=None) -> dict:
        parts = _identity(cause)
        family, *ids = parts
        sizes = {'task': 1, 'manual': 1, 'replan': 1, 'script': 1, 'origin': 1,
                 'dirty': 1, 'base': 2, 'borrow': 2, 'urgent': 2, 'replan-owner': 1,
                 'question': 1, 'timeout': 1, 'deadline': 3}
        if family == 'tuner':
            if len(ids) < 2 or any(not isinstance(i, str) for i in ids):
                raise ValueError('invalid tuner identity')
        elif family not in sizes or len(ids) != sizes[family]:
            raise ValueError('unknown cause family or identity')
        if family in ('replan', 'replan-owner', 'question', 'timeout') and (type(ids[0]) is not int or ids[0] < 1):
            raise ValueError('expected positive record id')
        integer_families = ('replan', 'replan-owner', 'question', 'timeout')
        if family not in integer_families and any(not isinstance(i, str) for i in ids):
            raise ValueError('family requires string identity')
        if family == 'urgent' and ids[0] not in ('task', 'epic'):
            raise ValueError('invalid urgent target')
        if family == 'base' and not re.fullmatch('[0-9a-fA-F]{40}', str(ids[1])):
            raise ValueError('invalid base SHA')
        handler = {'script': 'script', 'origin': 'retry', 'dirty': 'retry', 'base': 'retry',
                   'borrow': 'choice', 'urgent': 'choice', 'replan-owner': 'text',
                   'question': 'text', 'timeout': 'text', 'tuner': 'proposal'}.get(family, 'manual')
        fields = {'script': ('repo',), 'origin': ('repo',), 'dirty': ('repo',), 'base': ('repo', 'sha'),
                  'borrow': ('task', 'seat'), 'urgent': ('target_kind', 'target_id'),
                  'question': ('question',), 'timeout': ('wait',), 'replan-owner': ('request',),
                  'task': ('source',), 'manual': ('task',), 'replan': ('request',),
                  'deadline': ('task', 'role', 'reason_code')}
        inferred = ({'proposal_kind': ids[0], 'identity': ids[1:]} if family == 'tuner'
                    else dict(zip(fields[family], ids)))
        if family == 'replan-owner':
            row = self.conn.execute('SELECT task_id FROM replan_requests WHERE id=?', (ids[0],)).fetchone()
            if row is None:
                from .ops import NotFound
                raise NotFound(f'replan {ids[0]}')
            inferred['source'] = row[0]
        row = self.conn.execute('SELECT * FROM wait_causes WHERE cause=?', (cause,)).fetchone()
        if row is None:
            self.conn.execute('INSERT INTO wait_causes(cause,handler,data) VALUES(?,?,?)',
                              (cause, handler, json.dumps(inferred)))
            row = self.conn.execute('SELECT * FROM wait_causes WHERE cause=?', (cause,)).fetchone()
        result = dict(row)
        result['data'] = json.loads(result['data'])
        return result

    def state(self, cause) -> str | None:
        _identity(cause)
        row = self.conn.execute('SELECT state FROM wait_causes WHERE cause=?', (cause,)).fetchone()
        return row[0] if row else None

    def pending(self, subject=None) -> list[dict]:
        sql = ('SELECT w.*,s.resume_state,c.state AS cause_state,c.question_id,c.handler '
               'FROM waits w JOIN wait_subjects s USING(subject_kind,subject_key) '
               'JOIN wait_causes c USING(cause) WHERE w.released_event IS NULL')
        args = ()
        if subject is not None:
            sql += ' AND w.subject_kind=? AND w.subject_key=?'
            args = tuple(subject)
        rows = [dict(r) for r in self.conn.execute(sql + ' ORDER BY w.id', args)]
        for row in rows:
            row['data'] = json.loads(row['data'])
        return rows

    def wait(self, subject, kind, key, *, cause, resume=None, timeout_at=None, data=None) -> int:
        from . import ops
        data = dict(data or {})
        identities, parts = _identity(key), _identity(cause)
        if not isinstance(subject, (list, tuple)) or len(subject) != 2 or subject[0] not in ('task', 'merge_queue') or not isinstance(subject[1], str) or not subject[1]:
            raise ValueError('invalid subject')
        if timeout_at is not None and (type(timeout_at) not in (float, int) or not math.isfinite(timeout_at)):
            raise ValueError('invalid timeout')
        fields = {TASK_TERMINAL: {'source', 'mode'}, OWNER_ANSWER: {'mode'}, BASE: {'repo', 'sha', 'mode'},
                  REPLAN: {'request', 'source', 'mode'}, DEADLINE: {'role', 'reason_code', 'minutes', 'mode'}, MANUAL: {'note', 'mode'}}
        if kind not in fields or set(data) - (fields[kind] | {'note'}) or fields[kind] - set(data):
            raise ValueError('invalid wait kind or payload')
        if 'note' in data and not isinstance(data['note'], str):
            raise ValueError('note must be text')
        mode = data['mode']
        if mode not in ('blocked', 'needs-owner', 'preserve') or (kind == OWNER_ANSWER and mode == 'preserve') or (kind in (TASK_TERMINAL, REPLAN, MANUAL) and mode != 'blocked'):
            raise ValueError('invalid wait mode')
        expected = {TASK_TERMINAL: [data.get('source')], OWNER_ANSWER: [cause], BASE: [data.get('repo'), data.get('sha')],
                    REPLAN: [data.get('request')], MANUAL: [subject[1]],
                    DEADLINE: [subject[1], data.get('role'), data.get('reason_code')]}[kind]
        families = {TASK_TERMINAL: ('task', 'replan'), OWNER_ANSWER: ('script', 'origin', 'dirty', 'borrow', 'urgent', 'replan-owner', 'question', 'tuner', 'timeout'),
                    BASE: ('base',), REPLAN: ('replan',), MANUAL: ('manual',), DEADLINE: ('deadline',)}
        if identities != expected or parts[0] not in families[kind]:
            raise ValueError('wait identity does not match payload')
        if kind == TASK_TERMINAL and parts[0] == 'task' and parts[1:] != [data['source']]:
            raise ValueError('terminal source mismatch')
        if kind == BASE and parts[1:] != identities:
            raise ValueError('base identity mismatch')
        if kind in (MANUAL, DEADLINE, REPLAN) and parts[1:] != identities:
            raise ValueError('cause identity mismatch')
        if kind == DEADLINE and (timeout_at is None or data['mode'] != 'preserve' or data['role'] not in ('worker', 'gate', 'review', 'planner') or type(data['minutes']) is not int or data['minutes'] < 0 or not isinstance(data['reason_code'], str)):
            raise ValueError('invalid deadline')
        if resume is not None and resume not in states.STATES:
            raise ValueError('invalid resume state')
        with tx(self.conn):
            task = ops.get_task(self.conn, subject[1]) if subject[0] == 'task' else None
            if kind in (TASK_TERMINAL, REPLAN):
                ops.get_task(self.conn, data['source'])
            family, *ids = parts
            if family in ('replan', 'replan-owner'):
                request = self.conn.execute('SELECT task_id FROM replan_requests WHERE id=?', (ids[0],)).fetchone()
                if request is None:
                    raise ops.NotFound(f'replan {ids[0]}')
                if kind == REPLAN and request[0] != data['source']:
                    raise ValueError('request source mismatch')
            if family == 'question':
                ops.get_question(self.conn, ids[0])
            if family == 'timeout' and not self.conn.execute('SELECT 1 FROM waits WHERE id=?', (ids[0],)).fetchone():
                raise ops.NotFound(f'wait {ids[0]}')
            self._cause(cause)
            if self.state(cause) == CLOSED:
                self._reopen(cause)
            existing = self.conn.execute('SELECT id FROM waits WHERE subject_kind=? AND subject_key=? AND kind=? AND key=? AND cause=? AND released_event IS NULL', (*subject, kind, key, cause)).fetchone()
            if existing:
                return existing[0]
            active = self.pending(subject)
            self.conn.execute('INSERT OR IGNORE INTO wait_subjects(subject_kind,subject_key) VALUES(?,?)', subject)
            if not active:
                self.conn.execute('UPDATE wait_subjects SET resume_state=?,parked=0,reset_external=0 WHERE subject_kind=? AND subject_key=?',
                                  (resume or (task['status'] if task else None), *subject))
            event_id = ops.event(self.conn, 'waiting.registered', self.actor,
                                 {'subject': list(subject), 'kind': kind, 'key': key, 'cause': cause},
                                 task_id=subject[1] if task else None)
            cur = self.conn.execute('INSERT INTO waits(subject_kind,subject_key,kind,key,cause,timeout_at,data,created_event) VALUES(?,?,?,?,?,?,?,?)',
                                    (*subject, kind, key, cause, timeout_at, json.dumps(data), event_id))
            ident = cur.lastrowid
            row = next(r for r in self.pending(subject) if r['id'] == ident)
            if self._satisfied(row):
                self._release_rows([row], event_id, reset=kind not in (MANUAL, DEADLINE, REPLAN))
            else:
                self._park_subject(subject)
            return ident

    def _status(self, task, new):
        from . import ops
        old = task['status']
        if old == new:
            return
        states.check(old, new, self.actor)
        self.conn.execute('UPDATE tasks SET status=?,updated=? WHERE id=?', (new, ops.stamp(), task['id']))
        ops.event(self.conn, 'status', self.actor, {'from': old, 'to': new, 'reason': 'waiting'}, task_id=task['id'], epic_id=task['epic_id'])
        task['status'] = new

    def _park_subject(self, subject) -> None:
        from . import ops
        if subject[0] != 'task':
            return
        task = ops.get_task(self.conn, subject[1])
        if task['status'] in states.TERMINAL or self.conn.execute('SELECT 1 FROM runs WHERE task_id=? AND ended IS NULL', (subject[1],)).fetchone():
            return
        modes = {r['data']['mode'] for r in self.pending(subject)}
        target = states.NEEDS_OWNER if 'needs-owner' in modes else states.BLOCKED if 'blocked' in modes else None
        if target is None:
            return
        if target == states.BLOCKED and task['status'] == states.NEEDS_OWNER:
            self._status(task, states.READY if task['approved_at'] else states.DRAFT)
        self.conn.execute('UPDATE wait_subjects SET parked=1 WHERE subject_kind=? AND subject_key=?', subject)
        if target == states.NEEDS_OWNER and task['status'] in states.CREATION_STATES:
            self._status(task, states.BLOCKED)
        self._status(task, target)

    def _satisfied(self, row) -> bool:
        if row['kind'] == TASK_TERMINAL:
            task = self.conn.execute('SELECT status FROM tasks WHERE id=?', (row['data']['source'],)).fetchone()
            return task is None or task[0] in states.TERMINAL
        if row['kind'] == BASE:
            if self.state(row['cause']) in (AUTHORIZED, RESOLVED):
                return True
            cached = self.conn.execute('SELECT status FROM base_gate WHERE repo=? AND sha=?', (row['data']['repo'], row['data']['sha'])).fetchone()
            return cached is not None and cached[0] == 'green'
        if row['kind'] == OWNER_ANSWER:
            return self.state(row['cause']) in (AUTHORIZED, RESOLVED)
        return False

    def _release_rows(self, rows, event_id, *, reset=False) -> list[tuple[str, str]]:
        from . import ops
        if not self.conn.in_transaction:
            raise RuntimeError('wait release requires transaction')
        subjects = []
        for row in rows:
            subject = (row['subject_kind'], row['subject_key'])
            release = ops.event(self.conn, 'waiting.released', self.actor, {'wait': row['id'], 'cause': row['cause']}, task_id=subject[1] if subject[0] == 'task' else None)
            self.conn.execute('UPDATE waits SET released_event=? WHERE id=? AND released_event IS NULL', (release, row['id']))
            if reset:
                self.conn.execute('UPDATE wait_subjects SET reset_external=1 WHERE subject_kind=? AND subject_key=?', subject)
            if subject not in subjects:
                subjects.append(subject)
        for subject in subjects:
            if not self.pending(subject):
                self._restore_subject(subject, event_id)
            else:
                self._park_subject(subject)
        return subjects

    def _restore_subject(self, subject, event_id) -> None:
        from . import ops
        flags = self.conn.execute('SELECT * FROM wait_subjects WHERE subject_kind=? AND subject_key=?', subject).fetchone()
        self.conn.execute('UPDATE wait_subjects SET resume_state=NULL,reset_external=0,parked=0 WHERE subject_kind=? AND subject_key=?', subject)
        if subject[0] != 'task':
            return
        task = self.conn.execute('SELECT * FROM tasks WHERE id=?', (subject[1],)).fetchone()
        if task is None or task['status'] in states.TERMINAL:
            return
        task = dict(task)
        target = flags['resume_state']
        if flags['parked'] and target not in states.CREATION_STATES:
            target = states.READY if task['approved_at'] else states.PROPOSED
        if target == states.READY and not task['approved_at']:
            raise ops.NotApproved(subject[1])
        if flags['reset_external']:
            marker = self.conn.execute('SELECT COALESCE(MAX(id),0) FROM runs WHERE task_id=? AND role=? AND ended IS NOT NULL AND outcome<>?', (subject[1], 'worker', contract.DONE)).fetchone()[0]
            self.conn.execute('UPDATE tasks SET retry_after_run=?,replans_used=0 WHERE id=?', (marker, subject[1]))
        if target in states.CREATION_STATES and task['status'] == states.NEEDS_OWNER:
            self._status(task, states.DRAFT)
        if target:
            self._status(task, target)

    def release(self, cause) -> list[tuple[str, str]]:
        from . import ops
        _identity(cause)
        with tx(self.conn):
            subjects = list(dict.fromkeys((r['subject_kind'], r['subject_key'])
                                         for r in self.pending() if r['cause'] == cause))
            eid = ops.event(self.conn, 'waiting.release', self.actor, {'cause': cause})
            self._release_cause(cause, eid)
            return subjects

    def _release_cause(self, cause, event_id):
        rows = [r for r in self.pending() if r['cause'] == cause]
        if any(r['kind'] != MANUAL and not self._satisfied(r) for r in rows):
            raise ValueError('cause is not satisfied')
        released = self._release_rows(rows, event_id,
                                      reset=any(r['kind'] not in (MANUAL, DEADLINE, REPLAN) for r in rows))
        self.conn.execute('UPDATE wait_causes SET state=? WHERE cause=? AND state NOT IN (?,?)',
                          (RESOLVED, cause, AUTHORIZED, ATTEMPTING))
        return released

    def clear(self, cause) -> list[tuple[str, str]]:
        from . import ops
        _identity(cause)
        with tx(self.conn):
            subjects = list(dict.fromkeys((r['subject_kind'], r['subject_key'])
                                         for r in self.pending() if r['cause'] == cause))
            eid = ops.event(self.conn, 'waiting.clear', self.actor, {'cause': cause})
            self._clear_cause(cause, eid)
            return subjects

    def _clear_cause(self, cause, event_id):
        from . import ops
        row = self.conn.execute('SELECT * FROM wait_causes WHERE cause=?', (cause,)).fetchone()
        if row is None or row['state'] == CLOSED:
            return []
        if row['question_id'] and row['state'] == OPEN:
            raise PermissionError('live owner question cannot be cleared')
        if row['question_id']:
            self.conn.execute("UPDATE questions SET status='answered',answered_at=COALESCE(answered_at,?) WHERE id=?",
                              (ops.stamp(), row['question_id']))
            ops.event(self.conn, 'question-superseded', self.actor, {'question': row['question_id'], 'cause': cause})
        released = self._release_rows([r for r in self.pending() if r['cause'] == cause], event_id,
                                      reset=_identity(cause)[0] == 'base')
        self.conn.execute('UPDATE wait_causes SET state=?,answer=NULL,accepted_event=NULL,consumed_event=NULL,probe_session=NULL,probe_started=NULL WHERE cause=?',
                          (CLOSED, cause))
        return released

    def _validate_event(self, kind, data):
        required = {'base-ci': {'repo', 'sha', 'status'}, 'base-observed': {'repo', 'sha'},
                    'waiting.release': {'cause'}, 'waiting.clear': {'cause'},
                    'replan-completed': {'request', 'disposition'}, 'capacity-available': {'placement'}}
        if kind in required:
            if not isinstance(data, dict) or not required[kind] <= data.keys():
                raise ValueError(f'malformed {kind}')
            if kind.startswith('base-') and (not isinstance(data['repo'], str) or not re.fullmatch('[0-9a-fA-F]{40}', str(data['sha']))):
                raise ValueError('invalid base event')
            if kind == 'base-ci' and data['status'] not in ('green', 'red'):
                raise ValueError('invalid base result')
            if kind == 'capacity-available' and data['placement'] not in ('windows', 'wsl'):
                raise ValueError('invalid placement')
            if kind.startswith('waiting.'):
                _identity(data['cause'])
            if kind == 'replan-completed' and type(data['request']) is not int:
                raise ValueError('invalid request')
        if kind == 'question-answered':
            from . import ops
            if ops._answer_context.get() != (self.conn, data.get('question')):
                raise PermissionError('answer events must be written by answer_question')

    def delay(self, task_id, role, reason_code, now, *, minutes=None) -> int:
        from . import ops
        if type(now) not in (float, int) or not math.isfinite(now):
            raise ValueError('invalid deadline time')
        cause = cause_key('deadline', task_id, role, reason_code)
        with tx(self.conn):
            active = next((r for r in self.pending(('task', task_id)) if r['cause'] == cause), None)
            if active:
                return active['id']
            if minutes is None:
                boundary = 0
                for row in self.conn.execute("SELECT id,data FROM task_events WHERE task_id=? AND kind='run-started' ORDER BY id DESC", (task_id,)):
                    if json.loads(row['data']).get('role') == role:
                        boundary = row['id']
                        break
                previous = self.conn.execute('SELECT data FROM waits WHERE cause=? AND created_event>? ORDER BY id DESC LIMIT 1', (cause, boundary)).fetchone()
                minutes = min(30, 2 * json.loads(previous['data'])['minutes']) if previous else 1
            if type(minutes) is not int or minutes < 0:
                raise ValueError('invalid delay minutes')
            return self.wait(('task', task_id), DEADLINE, cause_key(task_id, role, reason_code), cause=cause,
                             timeout_at=now + minutes * 60,
                             data={'role': role, 'reason_code': reason_code, 'minutes': minutes, 'mode': 'preserve'})

    def expire(self, now) -> list[tuple[str, str]]:
        from . import ops
        if type(now) not in (float, int) or not math.isfinite(now):
            raise ValueError('invalid expiration time')
        with tx(self.conn):
            due = [dict(r) for r in self.conn.execute(
                'SELECT * FROM waits WHERE released_event IS NULL AND timeout_at IS NOT NULL AND timeout_at<=? ORDER BY timeout_at,id', (now,))]
            released = []
            for row in due:
                row['data'] = json.loads(row['data'])
                subject = (row['subject_kind'], row['subject_key'])
                if row['kind'] != DEADLINE:
                    cause = cause_key('timeout', row['id'])
                    self.wait(subject, OWNER_ANSWER, cause_key(cause), cause=cause, data={'mode': 'needs-owner'})
                    if subject[0] == 'task':
                        target_kind, target_id = subject
                    else:
                        previous = self._cause(row['cause'])
                        if previous['question_id']:
                            q = ops.get_question(self.conn, previous['question_id'])
                            target_kind, target_id = q['target_kind'], q['target_id']
                        else:
                            target_kind = 'epic'
                            target_id = ops.create_epic(self.conn, subject[1], 'Waiting timeouts', actor=self.actor)['id']
                    self.ask(cause, target_kind, target_id, 'A wait timed out', 'Confirm how work should proceed.')
                eid = ops.event(self.conn, 'waiting.timeout', self.actor, {'wait': row['id'], 'cause': row['cause']},
                                task_id=subject[1] if subject[0] == 'task' else None)
                released += self._release_rows([row], eid)
            return list(dict.fromkeys(released))

    def resolve(self, event) -> list[tuple[str, str]]:
        kind, data = event['kind'], event['data']
        if kind == 'waiting.release':
            return self._release_cause(data['cause'], event['id'])
        if kind == 'waiting.clear':
            return self._clear_cause(data['cause'], event['id'])
        if kind == 'replan-request':
            self._request(event)
        if kind == 'comment' and data.get('urgent'):
            self._urgent(event)
        if kind in ('base-observed', 'base-ci', 'origin-synced', 'origin-sync-failed', 'landing-clean', 'landing-dirty'):
            return self._retry_event(event)
        if kind == 'question-answered':
            return self._answered(event)
        released = []
        if kind == 'capacity-available' and data['placement'] == 'wsl':
            rows = [r for r in self.pending() if r['kind'] == DEADLINE and r['data']['reason_code'] == 'wsl-down']
            return self._release_rows(rows, event['id'])
        if kind in ('status', 'run-started') and event['task_id']:
            subject = ('task', event['task_id'])
            active = self.pending(subject)
            rows = [r for r in active if r['kind'] == DEADLINE and
                    ((kind == 'status' and data.get('to') != r['resume_state']) or
                     (kind == 'run-started' and data.get('role') == r['data']['role']))]
            if rows:
                if all(r['data']['mode'] == 'preserve' for r in active):
                    from . import ops
                    self.conn.execute('UPDATE wait_subjects SET resume_state=? WHERE subject_kind=? AND subject_key=?',
                                      (ops.get_task(self.conn, subject[1])['status'], *subject))
                released += self._release_rows(rows, event['id'])
        terminal = kind == 'status' and data.get('to') in states.TERMINAL
        deleted = kind == 'task.deleted'
        if terminal or deleted:
            source = data.get('id') if deleted else event['task_id']
            rows = [r for r in self.pending() if r['kind'] == TASK_TERMINAL and r['data']['source'] == source]
            released += self._release_rows(rows, event['id'], reset=True)
            for request in self.replans():
                if request['task_id'] == source:
                    released += self._request_done(request['id'], 'cancelled')['released']
            urgent = cause_key('urgent', 'task', source)
            urgent_rows = [r for r in self.pending() if r['cause'] == urgent]
            released += self._release_rows(urgent_rows, event['id'], reset=True)
            if self.state(urgent) is not None:
                self.conn.execute('UPDATE wait_causes SET state=? WHERE cause=?', (RESOLVED, urgent))
            own = self.pending(('task', source))
            released += self._release_rows(own, event['id'])
        elif kind == 'run-finished' and event['task_id']:
            self._park_subject(('task', event['task_id']))
        return released

    def ask(self, cause, target_kind, target_id, title, body="", options=(), allow_text=True) -> int:
        from . import ops
        options = [{'label': o['label'], 'description': o.get('description', ''), 'recommended': bool(o.get('recommended'))} for o in options]
        with tx(self.conn):
            family = _identity(cause)[0]
            if self.state(cause) is None and family != 'tuner':
                raise ValueError('question needs a registered cause')
            row = self._cause(cause)
            labels = [o['label'] for o in options]
            allowed = {'origin': {'Retry origin sync'}, 'dirty': {'Retry landing'},
                       'base': {'Retry landing', 'Keep holding'}, 'urgent': {'Resume work', 'Keep holding'},
                       'borrow': {'Approve borrowing', 'Keep default seat'}}
            if family in allowed and (not labels or len(set(labels)) != len(labels) or set(labels) - allowed[family] or allow_text):
                raise ValueError('incompatible handler options')
            if family == 'script' and (options or not allow_text):
                raise ValueError('script requires text')
            if row['question_id'] is not None:
                q = ops.get_question(self.conn, row['question_id'])
                if (q['target_kind'], q['target_id'], q['options'], bool(q['allow_text'])) != (target_kind, target_id, options, bool(allow_text)):
                    raise ValueError('incompatible question for cause')
                return q['id']
            if family == 'question':
                qid = row['data']['question']
                q = ops.get_question(self.conn, qid)
                if (q['target_kind'], q['target_id']) != (target_kind, target_id):
                    raise ValueError('question target mismatch')
            else:
                qid = ops.ask_question(self.conn, target_kind, target_id, title, body, options, allow_text, self.actor)
            self.conn.execute('UPDATE wait_causes SET question_id=? WHERE cause=?', (qid, cause))
            return qid

    def answer(self, cause) -> dict | None:
        _identity(cause)
        row = self.conn.execute('SELECT answer FROM wait_causes WHERE cause=?', (cause,)).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    def _owner_answer(self, question_id, choice, text, actor) -> dict:
        from . import ops
        row = self.conn.execute('SELECT * FROM wait_causes WHERE question_id=?', (question_id,)).fetchone()
        if row is None:
            return {}
        if actor != 'owner':
            raise PermissionError('only owner may answer managed questions')
        q = ops.get_question(self.conn, question_id)
        if choice is not None and choice not in [o['label'] for o in q['options']]:
            raise ValueError('invalid choice')
        if text and not q['allow_text']:
            raise ValueError('question does not accept text')
        if not choice and (not isinstance(text, str) or not text.strip()):
            raise ValueError('empty answer')
        family = _identity(row['cause'])[0]
        if family == 'script':
            from .. import repos
            # Validate the transient answer with the same repo contract used on reload.
            repos.normalize({'name': 'answer', 'repo': '.', 'scripts': {'test': text}})
        return {'question': question_id, 'actor': 'owner', 'choice': choice, 'text': text}

    def _reopen(self, cause):
        row = self._cause(cause)
        if row['question_id'] is not None:
            self.conn.execute("UPDATE questions SET status='open',answer_choice=NULL,answer_text=NULL,answered_at=NULL WHERE id=?", (row['question_id'],))
        self.conn.execute('UPDATE wait_causes SET state=?,probe_session=NULL,probe_started=NULL WHERE cause=?', (OPEN, cause))

    def _answered(self, event):
        from . import ops
        data = event['data']
        if type(data.get('question')) is not int:
            raise ValueError('invalid question event')
        q = ops.get_question(self.conn, data['question'])
        row = self.conn.execute('SELECT * FROM wait_causes WHERE question_id=?', (q['id'],)).fetchone()
        if row is None:
            return []
        answer = self._owner_answer(q['id'], q['answer_choice'], q['answer_text'], event['actor'])
        if q['status'] != 'answered' or data.get('choice') != q['answer_choice']:
            raise ValueError('answer event has no recorded answer')
        answer['event'] = event['id']
        self.conn.execute('UPDATE wait_causes SET answer=?,accepted_event=? WHERE cause=?', (json.dumps(answer), event['id'], row['cause']))
        if answer['choice'] == 'Keep holding':
            self._reopen(row['cause'])
            return []
        phase = AUTHORIZED if row['handler'] == 'retry' else RESOLVED
        self.conn.execute('UPDATE wait_causes SET state=?,probe_session=NULL,probe_started=NULL WHERE cause=?', (phase, row['cause']))
        return self._release_rows([r for r in self.pending() if r['cause'] == row['cause']], event['id'], reset=True)

    def consume_retry(self, cause) -> bool:
        _identity(cause)
        with tx(self.conn):
            row = self.conn.execute('SELECT * FROM wait_causes WHERE cause=?', (cause,)).fetchone()
            if row is None or row['handler'] != 'retry':
                return False
            if row['state'] == ATTEMPTING:
                if row['probe_session'] != PROCESS_SESSION:
                    self._reopen(cause)
                return False
            if row['state'] != AUTHORIZED or row['accepted_event'] is None or row['consumed_event'] == row['accepted_event']:
                return False
            self.conn.execute('UPDATE wait_causes SET state=?,consumed_event=accepted_event,probe_session=?,probe_started=? WHERE cause=?',
                              (ATTEMPTING, PROCESS_SESSION, time.time(), cause))
            parts = _identity(cause)
            if parts[0] == 'base':
                self.conn.execute('DELETE FROM base_gate WHERE repo=? AND sha=?', tuple(parts[1:]))
            return True

    def _retry_event(self, event):
        kind, data = event['kind'], event['data']
        repo = data.get('repo')
        if not isinstance(repo, str) or not repo:
            raise ValueError('retry event requires repo')
        released = []
        if kind in ('base-observed', 'base-ci'):
            if kind == 'base-ci':
                from . import ops
                self.conn.execute('INSERT INTO base_gate(repo,sha,status,at) VALUES(?,?,?,?) ON CONFLICT(repo,sha) DO UPDATE SET status=excluded.status,at=excluded.at',
                                  (repo, data['sha'], data['status'], ops.stamp()))
            for row in self.conn.execute('SELECT cause FROM wait_causes WHERE handler=?', ('retry',)).fetchall():
                parts = _identity(row['cause'])
                if parts[0] != 'base' or parts[1] != repo:
                    continue
                if kind == 'base-ci' and parts[2] == data['sha'] and data['status'] == 'red':
                    if self.state(row['cause']) != CLOSED:
                        self._reopen(row['cause'])
                if ((kind == 'base-observed' and parts[2] != data['sha']) or
                        (kind == 'base-ci' and parts[2] == data['sha'] and data['status'] == 'green')):
                    self.conn.execute('UPDATE wait_causes SET state=? WHERE cause=?', (RESOLVED, row['cause']))
                    released += self.clear(row['cause'])
            return released
        family = 'origin' if kind.startswith('origin-') else 'dirty'
        cause = cause_key(family, repo)
        if self.state(cause) is None:
            return []
        if kind in ('origin-sync-failed', 'landing-dirty'):
            self._reopen(cause)
            return []
        self.conn.execute('UPDATE wait_causes SET state=?,probe_session=NULL,probe_started=NULL WHERE cause=?', (RESOLVED, cause))
        return self._release_rows([r for r in self.pending() if r['cause'] == cause], event['id'], reset=True)

    def request_replan(self, task_id, trigger, data=None) -> dict:
        from . import ops
        if not isinstance(trigger, str) or not trigger:
            raise ValueError('replan trigger required')
        with tx(self.conn):
            if ops.get_task(self.conn, task_id)['status'] in states.TERMINAL:
                raise states.IllegalTransition('terminal task cannot request a replan')
            ops.event(self.conn, 'replan-request', self.actor, {**(data or {}), 'trigger': trigger}, task_id=task_id)
            return next(r for r in self.replans() if r['task_id'] == task_id)

    def replans(self) -> list[dict]:
        rows = [dict(r) for r in self.conn.execute('SELECT * FROM replan_requests WHERE state<>? ORDER BY id', (DONE,))]
        from . import ops
        for row in rows:
            row['snapshot'] = json.loads(row['snapshot'])
            row['result'] = json.loads(row['result']) if row['result'] else None
            row['task'] = ops.get_task(self.conn, row['task_id'])
            row['active_run'] = self.conn.execute('SELECT id FROM runs WHERE task_id=? AND ended IS NULL ORDER BY id LIMIT 1', (row['task_id'],)).fetchone()
            row['active_run'] = row['active_run'][0] if row['active_run'] else None
            row['pending'] = self.pending(('task', row['task_id']))
        return rows

    def _followers(self, source, *, overlap=False):
        from . import ops, scope
        followers = set(ops.dependents_of(self.conn, source['id'], transitive=True))
        if overlap and source['epic_id']:
            for task in ops.list_tasks(self.conn, epic_id=source['epic_id'], include_archived=True):
                if task['id'] != source['id'] and scope.any_overlap(source['scope_files'], task['scope_files']):
                    followers.add(task['id'])
        return [t for t in ops.list_tasks(self.conn, include_archived=True) if t['id'] in followers
                and t['status'] not in (*states.ACTIVE, *states.TERMINAL)]

    def _request(self, event) -> list[dict]:
        from . import ops
        if event['task_id']:
            tasks = [ops.get_task(self.conn, event['task_id'])]
        elif event['epic_id']:
            ops.get_epic(self.conn, event['epic_id'])
            tasks = ops.list_tasks(self.conn, epic_id=event['epic_id'], include_archived=True)
        else:
            raise ValueError('request requires a task or epic')
        result = []
        for task in tasks:
            if task['status'] in states.TERMINAL:
                continue
            existing = self.conn.execute('SELECT id FROM replan_requests WHERE task_id=? AND state<>?', (task['id'], DONE)).fetchone()
            if existing:
                result.append(dict(self.conn.execute('SELECT * FROM replan_requests WHERE id=?', (existing[0],)).fetchone()))
                continue
            cur = self.conn.execute('INSERT INTO replan_requests(task_id,requesting_event) VALUES(?,?)', (task['id'], event['id']))
            request_id = cur.lastrowid
            cause = cause_key('replan', request_id)
            for target in [task, *self._followers(task, overlap=True)]:
                self.wait(('task', target['id']), REPLAN, cause_key(request_id), cause=cause,
                          data={'request': request_id, 'source': task['id'], 'mode': 'blocked'})
            result.append(dict(self.conn.execute('SELECT * FROM replan_requests WHERE id=?', (request_id,)).fetchone()))
        return result

    def _request_done(self, request_id, disposition, result=None) -> dict:
        from . import ops
        if disposition not in ('owner-resolved', 'cancelled', 'revised', 'split'):
            raise ValueError('invalid replan disposition')
        with tx(self.conn):
            request = self.conn.execute('SELECT * FROM replan_requests WHERE id=?', (request_id,)).fetchone()
            if request is None:
                raise ops.NotFound(f'replan {request_id}')
            if request['state'] == DONE:
                return json.loads(request['result'])
            cause = cause_key('replan', request_id)
            eid = ops.event(self.conn, 'replan-completed', self.actor, {'request': request_id, 'disposition': disposition}, task_id=request['task_id'])
            self.conn.execute('UPDATE replan_requests SET state=?,disposition=?,finished_event=? WHERE id=?', (DONE, disposition, eid, request_id))
            released = self._release_rows([r for r in self.pending() if r['cause'] in (cause, cause_key('replan-owner', request_id))], eid, reset=disposition == 'owner-resolved')
            self.conn.execute('UPDATE wait_causes SET state=? WHERE cause=?', (RESOLVED, cause))
            completed = {'disposition': disposition, 'released': released, 'children': [], **(result or {})}
            self.conn.execute('UPDATE replan_requests SET result=? WHERE id=?', (json.dumps(completed), request_id))
            return completed

    def _urgent(self, event):
        from . import ops
        target_kind = 'task' if event['task_id'] else 'epic'
        target_id = event['task_id'] or event['epic_id']
        if not target_id:
            raise ValueError('urgent comment requires target')
        cause = cause_key('urgent', target_kind, target_id)
        if target_kind == 'task':
            source = ops.get_task(self.conn, target_id)
            if source['status'] in states.TERMINAL:
                return
            targets = self._followers(source)
        else:
            targets = [t for t in ops.list_tasks(self.conn, epic_id=target_id, include_archived=True)
                       if t['status'] not in (*states.ACTIVE, *states.TERMINAL)]
        if targets and self.state(cause) in (RESOLVED, CLOSED):
            self._reopen(cause)
        for task in targets:
            self.wait(('task', task['id']), OWNER_ANSWER, cause_key(cause), cause=cause, data={'mode': 'blocked'})
        if targets:
            self.ask(cause, target_kind, target_id, 'Resume work?', options=[{'label': 'Resume work'}, {'label': 'Keep holding'}], allow_text=False)

    def questions(self, *, include_archived=False) -> list[dict]:
        """Read-only canonical questions; lifecycle visibility is extended at T30."""
        from . import ops
        return ops.open_questions(self.conn)
