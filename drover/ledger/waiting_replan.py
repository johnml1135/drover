"""Waiting methods grouped by responsibility."""
import json

from .. import contract
from . import states
from .db import tx
from .waiting_common import (TASK_TERMINAL, OWNER_ANSWER, REPLAN, RESOLVED, OWNER, DONE,
                             cause_key, InvalidSplit)


class WaitingReplan:
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
        followers, order = self._replan_order()
        for row in rows:
            row['order'] = order[row['task_id']]
            row['prerequisites'] = [source for source in followers if row['task_id'] in followers[source]]
            row['snapshot'] = json.loads(row['snapshot'])
            row['result'] = json.loads(row['result']) if row['result'] else None
            row['inspection'] = self._snapshot(row['task_id'], {})
            row['task'] = {**ops.get_task(self.conn, row['task_id']), **row['inspection']['task']}
            row['active_run'] = self.conn.execute('SELECT id FROM runs WHERE task_id=? AND ended IS NULL ORDER BY id LIMIT 1', (row['task_id'],)).fetchone()
            row['active_run'] = row['active_run'][0] if row['active_run'] else None
            row['pending'] = self.pending(('task', row['task_id']))
        return rows

    def replan_run(self, run_id) -> dict | None:
        from . import ops
        rows = [dict(r) for r in self.conn.execute(
            'SELECT m.*,r.task_id,r.state,r.snapshot,r.result,r.current_run FROM replan_run_requests m '
            'JOIN replan_requests r ON r.id=m.request_id WHERE m.run_id=? ORDER BY m.position', (run_id,))]
        if not rows:
            return None
        for row in rows:
            row['snapshot'] = json.loads(row['snapshot'])
            row['result'] = json.loads(row['result']) if row['result'] else None
        anchor = self.conn.execute('SELECT task_id FROM runs WHERE id=?', (run_id,)).fetchone()[0]
        run = next(r for r in ops.list_runs(self.conn, anchor) if r['id'] == run_id)
        event = self.conn.execute("SELECT data FROM task_events WHERE kind='replan-enrolled' "
                                  "AND json_extract(data,'$.run')=? ORDER BY id LIMIT 1", (run_id,)).fetchone()
        assignment = json.loads(event[0])['assignment'] if event else {k: run[k] for k in ('backend', 'model', 'effort')}
        return dict(run_id=run_id, run=run, members=rows, assignment=assignment)

    def replan_runs(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT DISTINCT m.run_id FROM replan_run_requests m JOIN runs r ON r.id=m.run_id "
            "WHERE r.ended IS NULL OR (EXISTS (SELECT 1 FROM task_events e WHERE e.kind='launch-cancel' "
            "AND json_extract(e.data,'$.run')=r.id) AND NOT EXISTS (SELECT 1 FROM task_events e "
            "WHERE e.kind='launch-cancel-cleared' AND json_extract(e.data,'$.run')=r.id)) ORDER BY m.run_id")
        return [self.replan_run(r[0]) for r in rows]

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
        if self.conn.execute(
                "SELECT 1 FROM task_events WHERE kind='replan-request-processed' "
                "AND json_extract(data,'$.requesting_event')=? LIMIT 1", (event['id'],)).fetchone():
            return []
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
            existing = self.conn.execute(
                'SELECT id FROM replan_requests WHERE task_id=? AND (requesting_event=? OR state<>?) ORDER BY id LIMIT 1',
                (task['id'], event['id'], DONE)).fetchone()
            if existing:
                result.append(dict(self.conn.execute('SELECT * FROM replan_requests WHERE id=?', (existing[0],)).fetchone()))
                continue
            cur = self.conn.execute('INSERT INTO replan_requests(task_id,requesting_event) VALUES(?,?)', (task['id'], event['id']))
            request_id = cur.lastrowid
            cause = cause_key('replan', request_id)
            for target in [task, *self._followers(task, overlap=True)]:
                self.wait(('task', target['id']), REPLAN, cause_key(request_id), cause=cause,
                          data={'request': request_id, 'source': task['id'], 'mode': 'blocked'})
            self._supersede_stage(request_id)
            result.append(dict(self.conn.execute('SELECT * FROM replan_requests WHERE id=?', (request_id,)).fetchone()))
        # Coalesced events need durable identities too: the creating event alone
        # cannot identify them after the shared request has completed.
        ops.event(self.conn, 'replan-request-processed', self.actor,
                  {'requesting_event': event['id']}, task_id=event['task_id'], epic_id=event['epic_id'])
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
            if disposition in ('revised', 'split'):
                marker = self.conn.execute('SELECT COALESCE(MAX(id),0) FROM runs WHERE task_id=? AND role=? AND ended IS NOT NULL AND outcome<>?',
                                           (request['task_id'], 'worker', contract.DONE)).fetchone()[0]
                self.conn.execute('UPDATE tasks SET retry_after_run=? WHERE id=?', (marker, request['task_id']))
            cause = cause_key('replan', request_id)
            eid = ops.event(self.conn, 'replan-completed', self.actor, {'request': request_id, 'disposition': disposition}, task_id=request['task_id'])
            self.conn.execute('UPDATE replan_requests SET state=?,disposition=?,finished_event=? WHERE id=?', (DONE, disposition, eid, request_id))
            released = self._release_rows([r for r in self.pending() if r['cause'] in (cause, cause_key('replan-owner', request_id)) and r['kind'] in (REPLAN, OWNER_ANSWER)], eid, reset=disposition == 'owner-resolved')
            self.conn.execute('UPDATE wait_causes SET state=? WHERE cause=?', (RESOLVED, cause))
            if disposition == 'cancelled':
                owner_cause = cause_key('replan-owner', request_id)
                self.conn.execute('UPDATE wait_causes SET state=? WHERE cause=?', (RESOLVED, owner_cause))
                self._clear_cause(owner_cause, eid)
                self._clear_cause(cause, eid)
            completed = {'disposition': disposition, 'released': released, 'children': [], **(result or {})}
            self.conn.execute('UPDATE replan_requests SET result=? WHERE id=?', (json.dumps(completed), request_id))
            return completed

    def _split(self, request_id, record) -> dict:
        from . import ops
        request = self.conn.execute('SELECT * FROM replan_requests WHERE id=?', (request_id,)).fetchone()
        source = ops.get_task(self.conn, request['task_id'])
        # Validate the final graph using virtual child ids before allocating any rows.
        children = {t['key']: ('child', t['key']) for t in record['tasks']}
        edges = {}
        def edge(task, prerequisite, kind):
            pair = (task, prerequisite)
            edges[pair] = 'blocks' if kind == 'blocks' or edges.get(pair) == 'blocks' else 'waits-for'
        incoming = [dict(r) for r in self.conn.execute('SELECT task_id,kind FROM task_deps WHERE depends_on=?', (source['id'],))]
        for row in self.conn.execute('SELECT * FROM task_deps'):
            if row['depends_on'] != source['id']:
                edge(row['task_id'], row['depends_on'], row['kind'])
        prerequisites = ops.deps_of(self.conn, source['id'])
        for child in record['tasks']:
            for dep in prerequisites:
                edge(children[child['key']], dep['depends_on'], dep['kind'])
            for dep in child['deps']:
                edge(children[child['key']], children[dep], 'blocks')
            for dep in incoming:
                edge(dep['task_id'], children[child['key']], dep['kind'])
        nodes = {t['id'] for t in ops.list_tasks(self.conn, include_archived=True)} | set(children.values())
        graph = {node: [] for node in nodes}
        for task, prerequisite in edges:
            if task not in nodes or prerequisite not in nodes or task == prerequisite:
                raise InvalidSplit('invalid split dependency')
            graph[task].append(prerequisite)
        visiting, visited = set(), set()
        def visit(node):
            if node in visiting:
                raise InvalidSplit('split dependency cycle')
            if node in visited:
                return
            visiting.add(node)
            for dep in graph[node]:
                visit(dep)
            visiting.remove(node)
            visited.add(node)
        for node in graph:
            visit(node)
        ids = {}
        inherit = {k: source[k] for k in ('repo', 'repo_path', 'epic_id', 'kind', 'write', 'read_access', 'priority', 'size', 'risk')}
        for child in record['tasks']:
            ids[children[child['key']]] = ops.create_task(self.conn, actor=self.actor, **inherit,
                **{k: child[k] for k in ('title', 'goal', 'acceptance', 'scope_files')})['id']
        self.conn.execute('DELETE FROM task_deps WHERE depends_on=?', (source['id'],))
        for (task, prerequisite), kind in edges.items():
            task, prerequisite = ids.get(task, task), ids.get(prerequisite, prerequisite)
            existing = self.conn.execute('SELECT kind FROM task_deps WHERE task_id=? AND depends_on=?', (task, prerequisite)).fetchone()
            if existing:
                if kind == 'blocks' and existing['kind'] != 'blocks':
                    self.conn.execute('UPDATE task_deps SET kind=? WHERE task_id=? AND depends_on=?', (kind, task, prerequisite))
            else:
                ops.add_dep(self.conn, task, prerequisite, kind=kind, actor=self.actor)
        cause = cause_key('replan', request_id)
        for row in [r for r in self.pending() if r['cause'] == cause and r['subject_key'] != source['id']]:
            for child in ids.values():
                self.wait((row['subject_kind'], row['subject_key']), TASK_TERMINAL, cause_key(child), cause=cause,
                          data={'source': child, 'mode': 'blocked'})
        completed = self._request_done(request_id, 'split', {'children': list(ids.values())})
        ops.set_status(self.conn, source['id'], states.CANCELLED, actor=self.actor, reason='split into proposed tasks')
        return completed

    def _owner_replan(self, request_id, question) -> dict:
        from . import ops
        request = self.conn.execute('SELECT * FROM replan_requests WHERE id=?', (request_id,)).fetchone()
        cause = cause_key('replan-owner', request_id)
        rows = [r for r in self.pending() if r['cause'] == cause_key('replan', request_id)]
        for row in rows:
            self.wait((row['subject_kind'], row['subject_key']), OWNER_ANSWER, cause_key(cause), cause=cause,
                      data={'mode': 'needs-owner' if row['subject_key'] == request['task_id'] else 'blocked'})
        self.ask(cause, 'task', request['task_id'], question)
        self.conn.execute('UPDATE replan_requests SET state=?,current_run=NULL WHERE id=?', (OWNER, request_id))
        eid = ops.event(self.conn, 'replan-owner', self.actor, {'request': request_id}, task_id=request['task_id'])
        self._release_rows(rows, eid)
        return dict(disposition=OWNER, released=[], children=[])

    def _supersede_stage(self, request_id) -> None:
        from . import ops
        request = self.conn.execute('SELECT * FROM replan_requests WHERE id=?', (request_id,)).fetchone()
        if request is None or request['state'] == DONE:
            return
        runs = ops.list_runs(self.conn, request['task_id'])
        if any(r['ended'] is None for r in runs):
            return
        worker = next((r for r in reversed(runs) if r['role'] == 'worker'), None)
        if worker and worker['outcome'] in (contract.DONE, contract.GATED):
            stage = 'gate' if worker['outcome'] == contract.DONE else 'review'
            ops.amend_run(self.conn, worker['id'], outcome=contract.UNVERIFIED)
            ops.event(self.conn, 'replan.superseded-stage', self.actor,
                      dict(request=request_id, worker=worker['id'], stage=stage), task_id=request['task_id'])

    def _snapshot(self, task_id, inputs) -> dict:
        from . import ops
        task = ops.get_task(self.conn, task_id)
        fields = ('id', 'repo', 'repo_path', 'epic_id', 'title', 'goal', 'acceptance', 'scope_files',
                  'kind', 'write', 'read_access', 'branch', 'worktree', 'approved_at', 'model_pin')
        runs = [r for r in ops.list_runs(self.conn, task_id) if r['role'] != 'planner']
        latest = runs[-1] if runs else None
        return dict(task={k: task.get(k) for k in fields},
                    deps=sorted(ops.deps_of(self.conn, task_id), key=lambda r: (r['depends_on'], r['kind'])),
                    incoming=[dict(r) for r in self.conn.execute(
                        'SELECT task_id,kind FROM task_deps WHERE depends_on=? ORDER BY task_id,kind', (task_id,))],
                    latest_nonplanner_run={k: latest[k] for k in ('id', 'outcome', 'ended', 'gate_receipt', 'review_verdict')} if latest else None,
                    inputs=json.loads(json.dumps(inputs)))
