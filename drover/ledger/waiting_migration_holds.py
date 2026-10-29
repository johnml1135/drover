"""Legacy holds, deadlines, and finalization for the waiting migration."""
import json
import math
import re
from datetime import datetime

from .. import contract
from . import ops, states
from .waiting import (Waiting, cause_key, repo_key, TASK_TERMINAL, OWNER_ANSWER,
                      REPLAN, MANUAL, DEADLINE, BASE, RESOLVED)
from .waiting_migration_common import (_event, _events, _task, _subject, _stage, _question,
                                       _diagnostic, _request, _boundary)


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
