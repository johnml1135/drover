"""Waiting methods grouped by responsibility."""
import json
import math
import re
import time
import uuid

from . import states
from .db import tx
from .waiting_common import (TASK_TERMINAL, OWNER_ANSWER, DEADLINE, OPEN, AUTHORIZED,
                             ATTEMPTING, RESOLVED, CLOSED, cause_key, _identity)

PROCESS_SESSION = uuid.uuid4().hex


class WaitingEvents:
    def _validate_event(self, kind, data):
        required = {'base-ci': {'repo', 'sha', 'status'}, 'base-observed': {'repo', 'sha'},
                    'waiting.release': {'cause'}, 'waiting.clear': {'cause'},
                    'replan-completed': {'request', 'disposition'}, 'capacity-available': {'placement'},
                    'replan.superseded-stage': {'request', 'worker', 'stage'},
                    'replan.stage-finished': {'request', 'run', 'worker', 'stage', 'receipt'}}
        if kind in required:
            if not isinstance(data, dict) or not required[kind] <= data.keys():
                raise ValueError(f'malformed {kind}')
            if kind in ('replan.superseded-stage', 'replan.stage-finished'):
                if any(type(data[k]) is not int or data[k] < 1 for k in ('request', 'worker')) or data['stage'] not in ('gate', 'review'):
                    raise ValueError('invalid replan stage audit')
                if kind == 'replan.stage-finished' and (type(data['run']) is not int or data['run'] < 1):
                    raise ValueError('invalid predecessor run')
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
        with tx(self.conn):
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
                    self._clear_cause(urgent, event['id'])
                own = self.pending(('task', source))
                released += self._release_rows(own, event['id'])
            elif kind == 'run-finished' and event['task_id']:
                for request in self.replans():
                    if request['task_id'] == event['task_id']:
                        self._supersede_stage(request['id'])
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
        if row is None or row['state'] == CLOSED or (
                row['accepted_event'] is not None and event['id'] <= row['accepted_event']):
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
        if _identity(row['cause'])[0] == 'replan-owner':
            return self._request_done(_identity(row['cause'])[1], 'owner-resolved')['released']
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

    def finish_retry(self, cause, *, completed=False, deferred=False):
        """Finish a consumed probe even when CI cannot produce a code verdict."""
        with tx(self.conn):
            if self.state(cause) != ATTEMPTING:
                return
            if deferred:
                self.conn.execute('UPDATE wait_causes SET state=?,consumed_event=NULL,probe_session=NULL,probe_started=NULL WHERE cause=?',
                                  (AUTHORIZED, cause))
            elif completed:
                self.conn.execute('UPDATE wait_causes SET state=?,probe_session=NULL,probe_started=NULL WHERE cause=?',
                                  (RESOLVED, cause))
                self.clear(cause)
            else:
                self._reopen(cause)

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
            for row in self.conn.execute('SELECT cause FROM wait_causes WHERE handler=? AND state<>?', ('retry', CLOSED)).fetchall():
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
