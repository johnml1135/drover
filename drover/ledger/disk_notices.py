"""Hub-only delivery of durable disk notices into the existing Inbox."""
from . import ops, tx


def publish(conn, notice: dict) -> int:
    with tx(conn):
        for event in ops.events(conn, kind='disk.notice'):
            if event['data']['notice']['id'] == notice['id']:
                return event['data']['question']
        prior = ops.events(conn, kind='disk.notice')
        epic = prior[0]['epic_id'] if prior else ops.create_epic(conn, '', 'Disk space', actor='disk')['id']
        title = notice.get('title') or ('Repository disk budget exceeded' if notice['kind'] == 'budget' else 'Disk cleanup exhausted')
        question = ops.ask_question(conn, 'epic', epic, title, body=notice.get('body', ''),
                                    options=[dict(label='Acknowledge')], allow_text=False, actor='disk')
        ops.event(conn, 'disk.notice', 'disk', dict(notice=notice, question=question), epic_id=epic)
        return question


def replay(conn, cfg, now):
    """Publish hub facts and acknowledge durable notices only after ledger commit."""
    from .. import config, disk_state, ownership
    from . import states
    tasks = []
    for task in ops.list_tasks(conn, include_archived=True):
        live = task['status'] in states.ACTIVE
        tasks.append(dict(id=task['id'], state=task['status'], terminal=task['status'] in states.TERMINAL,
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
    delivered = []
    for ident, entry in state.get('outbox', {}).items():
        if not entry['acknowledged']:
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
    return delivered
