"""Cleanup notice persistence and delivery."""
import hashlib
import json
import uuid

from . import config, disk_state, placement


def _report_notices(state_dir, result):
    if not any(i.get('reason_code') == 'merged_live_task' for i in result['items']):
        return
    def prepare(state):
        outbox = state.setdefault('outbox', {})
        for item in result['items']:
            if item.get('reason_code') != 'merged_live_task':
                continue
            ident = 'merged_live_task:' + hashlib.sha256(json.dumps(
                [item.get('repo'), item.get('task_id'), item['path']]).encode()).hexdigest()
            notice = dict(id=ident, kind='merged_live_task', title='Merged work has an open task',
                          body=f"{item.get('task_id')}: {item['path']} retained for its open task.", items=[item])
            if ident not in outbox:
                outbox[ident] = dict(notice=notice, toasted=False, acknowledged=False)
    disk_state.update(state_dir, prepare)


def _notices(cfg, result, runtime, machines):
    state_dir = config.state_dir(cfg)
    def prepare(state):
        episodes = state.setdefault('notice_episodes', {})
        pending = []
        for drive in result['drives']:
            free = drive.get('post_free_gb', drive['free_gb'])
            name = drive['name']
            previous = episodes.get(name)
            if free is not None and free >= drive['warn_gb']:
                episodes.pop(name, None)
            elif result['mode'] != 'daily' and free is not None and free < drive['warn_gb'] and \
                    any(s['drive'] == name and s['stage'] == 6 for s in result['stages']):
                if previous is None or free <= previous['free'] - 1:
                    previous = dict(id='exhaustion:' + uuid.uuid4().hex, free=free)
                    episodes[name] = previous
                    pending.append(dict(id=previous['id'], kind='exhaustion', drive=name,
                                        title=f"Disk cleanup exhausted: {name}",
                                        body=f"{free:.1f} GiB free after all cleanup stages; review retained items."))
        actions = list(dict.fromkeys(p['owner_action'] for row in result['items']
                                     if row.get('reason_code') == 'process_visibility_unavailable'
                                     for p in row.get('processes', []) if p.get('owner_action')))
        if actions:
            for notice in pending:
                drive = next(d for d in result['drives'] if d['name'] == notice['drive'])
                if drive['level'] == 'critical':
                    notice['body'] += '\n' + '\n'.join(actions)
        budgets = state.setdefault('budget_episodes', {})
        for machine in machines:
            if not machine.get('budget_gb'):
                continue
            # Stage workers return distinct owning-side directories, never symlink targets.
            usage = {i['identity']: i['bytes'] for i in result['items']
                     if i.get('reason_code') == 'budget_usage' and i.get('repo') == machine['name']}
            if not usage:
                continue
            name = machine['name']
            if sum(usage.values()) > machine['budget_gb'] * 1024 ** 3:
                if name not in budgets:
                    budgets[name] = 'budget:' + uuid.uuid4().hex
                    pending.append(dict(id=budgets[name], kind='budget', repo=name,
                                        title=f"Repository disk budget exceeded: {name}",
                                        body=f"{sum(usage.values()) / 1024 ** 3:.3f} GiB in repository directories."))
            else:
                budgets.pop(name, None)
        outbox = state.setdefault('outbox', {})
        for notice in pending:
            outbox[notice['id']] = dict(notice=notice, toasted=False, acknowledged=False)
        return pending
    pending = disk_state.update(state_dir, prepare)
    for notice in pending:
        try:
            runtime.toast(notice)
        except (OSError, RuntimeError):
            continue
        try:
            disk_state.update(state_dir, lambda state: state['outbox'][notice['id']].update(toasted=True))
        except BlockingIOError:
            pass  # Transport status never gates durable Inbox delivery.


def retry_notices(cfg):
    """Drain completion facts deferred by state contention, without rescanning repos."""
    for path in (config.state_dir(cfg) / 'disk').glob('pending-notices-*.json'):
        try:
            pending = json.loads(path.read_text(encoding='utf-8'))
            _report_notices(config.state_dir(cfg), pending['result'])
            if pending['result']['applied']:
                _notices(cfg, pending['result'], placement.cleanup_runtime(cfg), pending['machines'])
        except BlockingIOError:
            return
        path.unlink(missing_ok=True)
