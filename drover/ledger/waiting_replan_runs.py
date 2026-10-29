"""Waiting methods grouped by responsibility."""
import json
from graphlib import TopologicalSorter
import math
import re
import time

from .. import contract
from . import states
from .db import tx
from .waiting_common import REPLAN, QUEUED, RUNNING, DONE


class WaitingReplanRuns:
    def _replan_order(self):
        from . import ops
        sources = [r[0] for r in self.conn.execute(
            'SELECT task_id FROM replan_requests WHERE state<>? ORDER BY task_id', (DONE,))]
        followers = {source: set(ops.dependents_of(self.conn, source, transitive=True)) for source in sources}
        order = {source: index for index, source in enumerate(TopologicalSorter({
            source: [other for other in sources if source in followers[other]]
            for source in sources}).static_order())}
        return followers, order

    def replan_busy(self, task_id, runs=None):
        """An open or uncertain physical run protects every member, including resolved requests.

        `runs` is a replan_runs() snapshot a caller reuses across many tasks."""
        runs = self.replan_runs() if runs is None else runs
        return any(any(m['task_id'] == task_id for m in batch['members']) for batch in runs)

    def _replan_eligible(self, request, enrolled, followers, order):
        from . import ops
        task = ops.get_task(self.conn, request['task_id'])
        if request['state'] != QUEUED or task['archived'] or task['status'] in (*states.ACTIVE, *states.TERMINAL):
            return False
        if self.replan_busy(task['id']) or self.conn.execute(
                'SELECT 1 FROM runs WHERE task_id=? AND ended IS NULL', (task['id'],)).fetchone():
            return False
        for row in self.pending(('task', task['id'])):
            if row['kind'] != REPLAN:
                return False
            other_id = row['data']['request']
            if other_id in enrolled:
                continue
            source = row['data']['source']
            other = self.conn.execute('SELECT state FROM replan_requests WHERE id=?', (other_id,)).fetchone()
            if (other and other['state'] == QUEUED and order.get(source, -1) > order[task['id']]
                    and task['id'] not in followers[source]):
                continue
            return False
        return True

    def start_replans(self, members, route, *, now=None):
        from . import ops
        if not members or len({m['request_id'] for m in members}) != len(members):
            raise ValueError('unique replan members required')
        with tx(self.conn):
            requests = []
            for member in members:
                row = self.conn.execute('SELECT * FROM replan_requests WHERE id=?', (member['request_id'],)).fetchone()
                if row is None:
                    raise ops.NotFound(f"replan {member['request_id']}")
                requests.append(dict(row))
            followers, order = self._replan_order()
            enrolled = {r['id'] for r in requests}
            if not all(self._replan_eligible(r, enrolled, followers, order) for r in requests):
                return None
            raw_limit = ops.get_setting(self.conn, 'replan_limit', '2')
            if not isinstance(raw_limit, str) or not re.fullmatch(r'[0-9]+', raw_limit):
                raise ValueError('replan_limit must be a nonnegative decimal integer')
            exhausted = [r for r in requests if ops.get_task(self.conn, r['task_id'])['replans_used'] >= int(raw_limit)]
            for request in exhausted:
                self._owner_replan(request['id'], 'The planner limit was reached. How should work proceed?')
            if exhausted:
                return None
            inputs = {m['request_id']: m['inputs'] for m in members}
            requests.sort(key=lambda r: order[r['task_id']])
            snapshots = {r['id']: self._snapshot(r['task_id'], inputs[r['id']]) for r in requests}
            if any(m.get('snapshot', snapshots[m['request_id']]) != snapshots[m['request_id']] for m in members):
                return None
            anchor = requests[0]['task_id']
            run = ops.start_run(self.conn, anchor, 'planner', route['backend'], route['model'],
                                route.get('effort'), actor=self.actor)
            for position, request in enumerate(requests):
                rid, tid = request['id'], request['task_id']
                if tid != anchor:
                    self.conn.execute('UPDATE tasks SET attempts=attempts+1,updated=? WHERE id=?', (ops.stamp(), tid))
                    ops.event(self.conn, 'run-started', self.actor, {'run': run, 'role': 'planner',
                              'backend': route['backend']}, task_id=tid)
                self.conn.execute('INSERT INTO replan_run_requests(run_id,request_id,position) VALUES(?,?,?)',
                                  (run, rid, position))
                self.conn.execute('UPDATE replan_requests SET state=?,current_run=?,snapshot=? WHERE id=?',
                                  (RUNNING, run, json.dumps(snapshots[rid]), rid))
            ops.event(self.conn, 'replan-enrolled', self.actor, {'run': run, 'assignment': route}, task_id=anchor)
            return self.replan_run(run)

    def _neutral(self, request_id, now):
        from . import ops
        request = self.conn.execute('SELECT * FROM replan_requests WHERE id=?', (request_id,)).fetchone()
        self.conn.execute('UPDATE replan_requests SET state=?,current_run=NULL WHERE id=?', (QUEUED, request_id))
        streak = 1
        for event in reversed(ops.events(self.conn, task_id=request['task_id'], kind='replan-attempt-finished')):
            data = event['data']
            if data.get('request') != request_id:
                continue
            if data.get('outcome') not in (contract.PROVIDER_ERROR, contract.LAUNCH_ERROR, contract.ORPHANED, contract.UNVERIFIED):
                break
            streak += 1
        delay = min(1800, 60 * 2 ** min(streak - 1, 5))
        self.delay(request['task_id'], 'planner', 'replan-neutral', now, minutes=math.ceil(delay / 60))
        return dict(disposition='neutral', released=[], children=[])

    def verify_replans(self, run_id, fresh, *, now=None):
        """Abort an unlaunched batch as a unit, charging backoff only to changed members."""
        from . import ops
        with tx(self.conn):
            batch = self.replan_run(run_id)
            if batch is None:
                raise ValueError('unknown planner batch')
            changed = {m['request_id'] for m in batch['members'] if fresh.get(m['request_id']) is None
                       or self._snapshot(m['task_id'], fresh[m['request_id']]) != m['snapshot']
                       or m['state'] != RUNNING or ops.get_task(self.conn, m['task_id'])['archived']}
            if not changed:
                return True
            self._complete_replans(batch, contract.UNVERIFIED, {}, {}, fresh, now, unchanged_queued=True)
            return False

    def finish_replans(self, run_id, outcome, records=None, usage=None, *, fresh=None, now=None):
        with tx(self.conn):
            batch = self.replan_run(run_id)
            if batch is None:
                raise ValueError('unknown planner batch')
            return self._complete_replans(batch, outcome, records or {}, usage or {}, fresh or {}, now)

    def _complete_replans(self, batch, outcome, records, usage, fresh, now, *, unchanged_queued=False):
        from . import ops
        allowed = (contract.DONE, contract.NO_RECORD, contract.ERROR, contract.TIMEOUT,
                   contract.PROVIDER_ERROR, contract.LAUNCH_ERROR, contract.ORPHANED, contract.UNVERIFIED)
        if outcome not in allowed:
            raise ValueError('invalid planner outcome')
        now = time.time() if now is None else now
        if type(now) not in (float, int) or not math.isfinite(now):
            raise ValueError('invalid planner time')
        members, run_id = batch['members'], batch['run_id']
        results = {}
        # Read every comparison before a split can change another member's graph.
        matches = {m['request_id']: fresh.get(m['request_id']) is not None
                   and self._snapshot(m['task_id'], fresh[m['request_id']]) == m['snapshot'] for m in members}
        singleton, physical_outcome = len(members) == 1, outcome
        for member in members:
            rid, tid = member['request_id'], member['task_id']
            if member['finished_event']:
                event = self.conn.execute('SELECT data FROM task_events WHERE id=?', (member['finished_event'],)).fetchone()
                results[rid] = json.loads(event[0])['result']
                continue
            request = self.conn.execute('SELECT * FROM replan_requests WHERE id=?', (rid,)).fetchone()
            task = ops.get_task(self.conn, tid)
            local_outcome = contract.UNVERIFIED if not singleton and outcome == contract.NO_RECORD else outcome
            if request['state'] == DONE:
                completed = json.loads(request['result'])
                local_outcome = contract.UNVERIFIED
            elif task['status'] in states.TERMINAL or task['archived']:
                completed = self._request_done(rid, 'cancelled')
                local_outcome = contract.UNVERIFIED
            elif unchanged_queued and matches[rid]:
                self.conn.execute('UPDATE replan_requests SET state=?,current_run=NULL WHERE id=?', (QUEUED, rid))
                completed = dict(disposition=QUEUED, released=[], children=[])
            else:
                if not matches[rid]:
                    local_outcome = contract.UNVERIFIED
                record = contract.validate_replan_record(records.get(tid))
                if record and record['action'] == 'revise' and any(task[f] and not record[f] for f in ('acceptance', 'scope_files')):
                    record = None
                if record and record['action'] == 'split' and any(task[f] and not child[f]
                        for child in record['tasks'] for f in ('acceptance', 'scope_files')):
                    record = None
                if local_outcome == contract.DONE and record is None:
                    local_outcome = contract.NO_RECORD if singleton else contract.UNVERIFIED
                if local_outcome in (contract.PROVIDER_ERROR, contract.LAUNCH_ERROR, contract.ORPHANED, contract.UNVERIFIED):
                    completed = self._neutral(rid, now)
                else:
                    # A real savepoint isolates a rejected member without hiding database failures.
                    self.conn.execute('SAVEPOINT replan_member')
                    try:
                        self.conn.execute('UPDATE tasks SET replans_used=replans_used+1 WHERE id=?', (tid,))
                        self.conn.execute('UPDATE replan_requests SET state=?,current_run=NULL WHERE id=?', (QUEUED, rid))
                        if local_outcome == contract.DONE and record['action'] == 'split':
                            completed = self._split(rid, record)
                        elif local_outcome == contract.DONE and record['action'] == 'revise':
                            ops.update_task(self.conn, tid, actor=self.actor,
                                            **{k: record[k] for k in ('goal', 'acceptance', 'scope_files')})
                            completed = self._request_done(rid, 'revised')
                        elif local_outcome == contract.DONE and record['action'] == 'needs-owner':
                            completed = self._owner_replan(rid, record['question'])
                        else:
                            completed = dict(disposition='retry', released=[], children=[])
                    except ValueError:
                        self.conn.execute('ROLLBACK TO replan_member')
                        if singleton:
                            local_outcome = contract.NO_RECORD
                            self.conn.execute('UPDATE tasks SET replans_used=replans_used+1 WHERE id=?', (tid,))
                            self.conn.execute('UPDATE replan_requests SET state=?,current_run=NULL WHERE id=?', (QUEUED, rid))
                            completed = dict(disposition='retry', released=[], children=[])
                        else:
                            local_outcome = contract.UNVERIFIED
                            completed = self._neutral(rid, now)
                    finally:
                        self.conn.execute('RELEASE replan_member')
            completed = json.loads(json.dumps(completed))
            eid = ops.event(self.conn, 'replan-attempt-finished', self.actor,
                            dict(request=rid, run=run_id, outcome=local_outcome, result=completed), task_id=tid)
            self.conn.execute('UPDATE replan_run_requests SET finished_event=? WHERE run_id=? AND request_id=?', (eid, run_id, rid))
            results[rid] = completed
            if singleton:
                physical_outcome = local_outcome
        if batch['run']['ended'] is None:
            ops.finish_run(self.conn, run_id, physical_outcome, actor=self.actor, **usage)
        return results
