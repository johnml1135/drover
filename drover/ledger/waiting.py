"""Durable waits and their event-driven release, in the ledger transaction."""
import json
from graphlib import TopologicalSorter
import math
import re
import time
import uuid

from .. import contract, placement
from . import states
from .db import tx
from .waiting_common import (TASK_TERMINAL, OWNER_ANSWER, BASE, REPLAN, DEADLINE, MANUAL,
                             OPEN, AUTHORIZED, ATTEMPTING, RESOLVED, CLOSED,
                             QUEUED, RUNNING, OWNER, DONE, cause_key, _identity, repo_key, InvalidSplit)
from .waiting_core import WaitingCore
from .waiting_events import PROCESS_SESSION, WaitingEvents
from .waiting_replan import WaitingReplan
from .waiting_replan_runs import WaitingReplanRuns


class Waiting(WaitingCore, WaitingEvents, WaitingReplan, WaitingReplanRuns):
    def __init__(self, conn, actor="system"):
        self.conn, self.actor = conn, actor

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

    def forget_task(self, task_id) -> None:
        """Detach a task and preserve shared decisions inside the deletion transaction."""
        from . import ops
        with tx(self.conn):
            task = ops.get_task(self.conn, task_id)
            managed = self.conn.execute(
                "SELECT c.* FROM wait_causes c JOIN questions q ON q.id=c.question_id "
                "WHERE q.target_kind='task' AND q.target_id=?", (task_id,)).fetchall()
            for cause in managed:
                family = _identity(cause['cause'])[0]
                if family == 'script':
                    remaining = [r for r in self.conn.execute(
                        'SELECT id,repo FROM tasks WHERE id<>? ORDER BY archived,rank,id', (task_id,))
                        if r['repo'] and repo_key(r['repo']) == json.loads(cause['data'])['repo']]
                else:
                    remaining = self.conn.execute(
                        "SELECT DISTINCT t.id,t.archived,t.rank FROM waits w JOIN tasks t ON t.id=w.subject_key "
                        "WHERE w.subject_kind='task' AND w.cause=? AND w.released_event IS NULL AND t.id<>? "
                        "ORDER BY t.archived,t.rank,t.id", (cause['cause'], task_id)).fetchall()
                if remaining:
                    self.conn.execute('UPDATE questions SET target_id=? WHERE id=?',
                                      (remaining[0]['id'], cause['question_id']))
                elif self.conn.execute(
                        "SELECT 1 FROM waits WHERE cause=? AND subject_kind='merge_queue' AND released_event IS NULL",
                        (cause['cause'],)).fetchone():
                    epic = ops.create_epic(self.conn, task['repo'], 'Waiting decisions', actor=self.actor)
                    self.conn.execute("UPDATE questions SET target_kind='epic',target_id=? WHERE id=?",
                                      (epic['id'], cause['question_id']))
                else:
                    # Teardown is allowed to supersede the last consumer's guarded question.
                    self.conn.execute('UPDATE wait_causes SET state=? WHERE cause=?', (RESOLVED, cause['cause']))
                    self.clear(cause['cause'])
            # Resolve source followers and requests before their task/run references disappear.
            ops.event(self.conn, 'task.deleted', self.actor, {'id': task_id})
            self.conn.execute("DELETE FROM wait_subjects WHERE subject_kind='task' AND subject_key=?", (task_id,))

    def questions(self, *, include_archived=False) -> list[dict]:
        """Open decisions and their surviving consumers, including unmanaged questions."""
        from . import ops
        result = []
        for question in ops.open_questions(self.conn):
            cause = self.conn.execute('SELECT cause,handler FROM wait_causes WHERE question_id=?',
                                      (question['id'],)).fetchone()
            subjects = []
            if cause:
                rows = [row for row in self.pending() if row['cause'] == cause['cause']]
                subjects = list(dict.fromkeys((r['subject_kind'], r['subject_key']) for r in rows))
            else:
                subjects = [(question['target_kind'], question['target_id'])]
            visible = include_archived or any(
                kind != 'task' or self.conn.execute('SELECT 1 FROM tasks WHERE id=? AND archived=0', (key,)).fetchone()
                for kind, key in subjects)
            result.append({**question, 'cause': cause['cause'] if cause else None,
                           'subjects': subjects, 'visible': bool(visible),
                           'proposal': bool(cause and cause['handler'] == 'proposal')})
        return result
