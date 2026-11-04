"""Hub-only delivery of durable disk notices into the existing Inbox."""
import hashlib
import json

from . import ops, tx


def publish(conn, notice: dict, *, kind='disk.notice', epic_title='Disk space', actor='disk') -> int:
    """Put a notice into the Inbox once per notice id; each kind finds or creates its own epic.

    A notice may carry its own `options` and `allow_text`; otherwise the owner can only acknowledge it."""
    with tx(conn):
        prior = ops.events(conn, kind=kind)
        for event in prior:
            if event['data']['notice']['id'] == notice['id']:
                return event['data']['question']
        epic = prior[0]['epic_id'] if prior else ops.create_epic(conn, '', epic_title, actor=actor)['id']
        title = notice.get('title') or ('Repository disk budget exceeded' if notice['kind'] == 'budget' else 'Disk cleanup exhausted')
        question = ops.ask_question(conn, 'epic', epic, title, body=notice.get('body', ''),
                                    options=notice.get('options') or [dict(label='Acknowledge')],
                                    allow_text=notice.get('allow_text', False), actor=actor)
        ops.event(conn, kind, actor, dict(notice=notice, question=question), epic_id=epic)
        return question


def retract(conn, notice_id: str, *, kind='disk.notice', actor='disk', choice='Acknowledge', text=None) -> None:
    """Close the Inbox question of a notice whose cause has cleared, with an answer that question accepts."""
    for event in ops.events(conn, kind=kind):
        if event['data']['notice']['id'] == notice_id:
            question = ops.get_question(conn, event['data']['question'])
            if question['status'] == 'open':
                ops.answer_question(conn, question['id'], choice=choice, text=text, actor=actor)


def replay(conn, cfg, now):
    """Publish hub facts and acknowledge durable notices only after ledger commit."""
    from .. import config, disk_state, ownership
    from . import states
    tasks = []
    running = {row[0] for row in conn.execute('SELECT DISTINCT task_id FROM runs WHERE ended IS NULL')}
    for task in ops.list_tasks(conn, include_archived=True):
        # A worktree stays live until its run has stopped, whatever the owner has done with the task since.
        closed = task['status'] in states.CLOSED and task['id'] not in running
        live = not closed
        tasks.append(dict(id=task['id'], state=task['status'], terminal=closed,
                          live=live, worktree=task.get('worktree'), owner='drover:' + task['id']))
        try:
            if live and task.get('worktree') and ownership.owned_by(task['worktree'], 'drover:' + task['id']):
                ownership.tag(task['worktree'], task['repo'], 'drover:' + task['id'], now + 86400, now=now)
        except (OSError, ValueError):
            pass
    state_dir = config.state_dir(cfg)
    disk_state.save_file(state_dir, 'tasks.json', dict(at=now, tasks=tasks,
                         active_worktrees=[t['worktree'] for t in tasks if t['live'] and t['worktree']]))
    try:
        state = disk_state.read(state_dir)
    except BlockingIOError:
        return []
    reports = []
    # The full cleanup's report, and the build-roots run's, kept apart so an hourly build-roots run never replaces it.
    # Each file is read once per version (its mtime and size, kept in disk state), so a pass that finds nothing new
    # costs two stats. A build-roots report is an event only when it removed something or has a failed row.
    seen = dict(state.get('journalled') or {})
    for name in ('last-report.json', 'last-build-roots.json'):
        path = state_dir / 'disk' / name
        try:
            info = path.stat()
            version = [info.st_mtime_ns, info.st_size]
            if seen.get(name) == version:
                continue
            report = json.loads(path.read_text(encoding='utf-8'))
            worth = name != 'last-build-roots.json' or report.get('errors') or \
                any(item.get('action') == 'removed' for item in report.get('items', []))
            if worth:
                ident = hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()
                with tx(conn):
                    if not any(e['data']['id'] == ident for e in ops.events(conn, kind='disk.report')):
                        ops.event(conn, 'disk.report', 'disk', dict(id=ident, report=report))
                        reports.append(ident)
            seen[name] = version
        except (OSError, ValueError, AttributeError):
            pass
    if seen != (state.get('journalled') or {}):
        try:
            disk_state.update(state_dir, lambda current: current.update(journalled=seen))
        except BlockingIOError:
            pass        # read again next pass; the event's id keeps it recorded once
    retracted = [ident for ident, entry in state.get('outbox', {}).items()
                 if entry.get('retracted') and entry['acknowledged']]
    for ident in retracted:
        retract(conn, ident)
    if retracted:
        def forget(state):
            for ident in retracted:
                state['outbox'].pop(ident, None)
        try:
            disk_state.update(state_dir, forget)
        except BlockingIOError:
            pass
    delivered = []
    for ident, entry in state.get('outbox', {}).items():
        if not entry['acknowledged'] and not entry.get('retracted'):
            publish(conn, entry['notice'])
            delivered.append(ident)
    if delivered:
        def acknowledge(state):
            for ident in delivered:
                state['outbox'][ident]['acknowledged'] = True
        try:
            disk_state.update(state_dir, acknowledge)
        except BlockingIOError:
            pass  # Ledger dedup protects replay after temporary contention.
    return delivered + reports
