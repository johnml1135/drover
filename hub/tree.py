"""Batched ledger data for the owner task tree."""
from collections import Counter
from datetime import datetime, timezone
import json


def tree_data(conn):
    epics = [dict(r, tasks=[]) for r in conn.execute('SELECT id, title, status FROM epics ORDER BY rank, id')]
    rows = conn.execute('''
        SELECT t.*,
          (SELECT at FROM task_events WHERE task_id=t.id AND kind='status' ORDER BY id DESC LIMIT 1) status_at,
          (SELECT json_object('kind',kind,'actor',actor,'at',at,'data',data) FROM task_events
             WHERE task_id=t.id ORDER BY id DESC LIMIT 1) last_event,
          (SELECT json_object('id',id,'role',role,'backend',backend,'model',model,'started',started)
             FROM runs WHERE task_id=t.id AND ended IS NULL ORDER BY id DESC LIMIT 1) current_run,
          (SELECT json_group_array(json_object('id',d.depends_on,'kind',d.kind,'status',p.status))
             FROM task_deps d JOIN tasks p ON p.id=d.depends_on WHERE d.task_id=t.id) deps,
          (SELECT json_group_array(json_object('id',d.task_id,'kind',d.kind))
             FROM task_deps d WHERE d.depends_on=t.id) blocks
        FROM tasks t ORDER BY t.epic_id, t.rank, t.id
    ''').fetchall()
    groups = {e['id']: e for e in epics}
    no_epic = {'id': '', 'title': 'No epic', 'status': '', 'tasks': []}
    tasks = {}
    for group in [*epics, no_epic]:
        group['counts'] = Counter()
        group['open'] = False
    now = datetime.now(timezone.utc)
    for row in rows:
        t = dict(row)
        for field in ('last_event', 'current_run', 'deps', 'blocks'):
            t[field] = json.loads(t[field]) if t[field] else None
        t['description'] = '\n'.join((t['goal'] or '').splitlines()[:6])
        t['waiting'] = [d for d in t['deps'] if d['status'] != 'landed']
        t['children'] = []
        try:
            at = datetime.fromisoformat((t['status_at'] or t['created']).replace('Z', '+00:00'))
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            seconds = max(0, int((now - at).total_seconds()))
            t['age'] = (f'{seconds // 86400}d' if seconds >= 86400 else f'{seconds // 3600}h' if seconds >= 3600
                        else f'{seconds // 60}m' if seconds >= 60 else 'now')
        except (ValueError, TypeError):
            t['age'] = 'unknown'
        tasks[t['id']] = t
        group = groups.get(t['epic_id'], no_epic)
        group['counts'][t['status']] += 1
        group['open'] |= t['status'] not in ('landed', 'closed', 'cancelled', 'abandoned')
    for t in tasks.values():
        group = groups.get(t['epic_id'], no_epic)
        parent = tasks.get(t['parent_id'])
        seen = {t['id']}
        ancestor = parent
        while ancestor and ancestor['id'] not in seen:
            seen.add(ancestor['id'])
            ancestor = tasks.get(ancestor['parent_id'])
        if parent and ancestor is None and groups.get(parent['epic_id'], no_epic) is group:
            parent['children'].append(t)
        else:
            group['tasks'].append(t)
    if no_epic['tasks']:
        epics.append(no_epic)
    return epics
