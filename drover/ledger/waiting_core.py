"""Waiting methods grouped by responsibility."""
import json
import math
import re

from .. import contract
from . import states
from .db import tx
from .waiting_common import (TASK_TERMINAL, OWNER_ANSWER, BASE, REPLAN, DEADLINE, MANUAL,
                             OPEN, AUTHORIZED, ATTEMPTING, RESOLVED, CLOSED, _identity)


class WaitingCore:
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
        parts = _identity(cause)
        row = self.conn.execute('SELECT state FROM wait_causes WHERE cause=?', (cause,)).fetchone()
        if row:
            return row[0]
        if len(parts) > 1 and parts[0] == 'tuner' and parts[1] != 'legacy':
            for legacy in self.conn.execute('SELECT state,data FROM wait_causes WHERE handler=? AND state=?', ('proposal', OPEN)):
                subset = json.loads(legacy['data']).get('legacy_subset')
                if subset and len(subset) == len(parts) - 1 and all(
                        expected is None or expected == actual for expected, actual in zip(subset, parts[1:])):
                    return legacy['state']
        return None

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
            eid = ops.event(self.conn, 'waiting.clear', self.actor, {'cause': cause}) if subjects else None
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
