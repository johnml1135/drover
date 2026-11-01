"""A due wake reaches a safe pane, or stays in the mailbox.

A wake is one fixed line. It carries no mail and grants nothing, and only the agent's own commit takes mail out of the
inbox. A refusal of any kind, or a pane that reads as None, leaves the mail and spends no wake: `pane_send.check` comes
before the reserve, and a `PaneRefused` after it means no input went out, so the entry is taken back. A held or coalesced
answer is taken back too, or it would stand for this mail's position after its cause had cleared. Claude panes often read
None while a past prompt is visible; that is accepted, and it means mail.

Scheduling is not delivery, and `sent` closes a write only for `pane_send`. Here a wake stays outstanding, and no other
goes, until later ticks settle it:

    pending, unverified     may not have landed: only `pane_send.confirm_landed` moves them to `observed`
    sent                    landed: working or blocked activity moves it to `observed`
    queued                  this exact text is in the agent's queue
    observed                a turn was seen; a higher checkpoint revision makes it `acknowledged`, the only delivered wake
    wake_unverified         past UNVERIFIED_S, with one notice, whichever of these it was. It stays open and refuses
                            every later send, while mail still flows, until the agent acts on the ledger or the row rotates

A higher revision with no turn seen proves the agent acted, not that the wake did it. The write waits out UNVERIFIED_S for
a turn and then ends `observed` with reason ACTED, unnoticed. ACTED and ROTATED mean the wake is over and claim no delivery.

The key is `wake:<wake_position>:<checkpoint_revision>:<row id>`, not the position alone. `reserve_wake` answers a key it
knows from its stored receipt, so a director that commits with mail still pending, or the next row of a role whose last
agent never committed, would be woken at the same position free of the hourly limit and of the window, and a refusal
would take back a wake that really went out. A repeated key now means only that a hub died before its send.
"""
from . import compaction_hold, pane_launch, pane_screen, pane_send
from .ledger import disk_notices, ops_panes, tx
from .notifications import roles
from .pane_send import PaneRefused

TEXT = 'Drover: new notices. Read your inbox and commit a checkpoint.'
UNVERIFIED_S = 900
WEDGED_S = 600
ACTIVE = ('working', 'blocked')
TURN_SEEN = 'turn-seen'             # as `pane_send.confirm_landed` records it
ACTED, ROTATED = 'ledger-acted', 'row-rotated'
MAY_NOT_HAVE_LANDED = ('pending', 'unverified')


def _revision_key(row):
    return f"pane_wake_revision:{row['id']}"


def _queue_key(write):
    return f"pane_wake_queued:{write['id']}"


def _unverified_id(write):
    return f"pane-wake-unverified:{write['id']}"


def _wedged_id(write):
    return f"pane-wake-wedged:{write['id']}"


def _outstanding(write):
    return write is not None and write['state'] not in ('unsent', 'acknowledged') and write['reason'] not in (ACTED, ROTATED)


def _settle(service, write, state, reason):
    ops_panes.finish_write(service.conn, write['id'], state, reason)
    for notice_id in (_unverified_id(write), _wedged_id(write)):
        disk_notices.retract(service.conn, notice_id, kind='pane.notice', actor='panes')


def _close_rotated(service, row):
    """A retired row gets no ticks of its own, so its role's next live row ends what it left open."""
    for old in ops_panes.open_agents(service.conn):
        if old['role_id'] == row['role_id'] and old['id'] != row['id'] and old['state'] not in pane_launch.RUNNING:
            for write in ops_panes.open_writes(service.conn, old['id']):
                if write['kind'] == 'wake':
                    _settle(service, write, 'observed', ROTATED)


def _take_back(service, role_id, key):
    """Nothing went out, so nothing was spent: the entry would count against the hour and start the window."""
    with tx(service.conn):
        service.conn.execute('DELETE FROM director_budget_entries WHERE role_id=? AND request_key=?', (role_id, key))


def _send(service, row, role_id, state):
    try:
        pane_send.check(service.herdr, row)
    except PaneRefused:
        return
    revision = state['checkpoint_revision']
    key = f"wake:{state['wake_position']}:{revision}:{row['id']}"
    if not service.notifications.reserve_wake(role_id, key).get('accepted'):
        _take_back(service, role_id, key)
        return
    service.port.set_setting(_revision_key(row), str(revision))
    try:
        pane_send.send(service.port, service.herdr, row, TEXT, 'wake')
    except PaneRefused:
        _take_back(service, role_id, key)


def _publish(service, row, notice_id, title, what):
    pane_launch.publish(service.port, notice_id, title, f"Seat {row['seat']} ({row['placement']}), pane {row['pane_id']}: {what}")


def _watch_queue(service, row, write, idle):
    """Ten idle minutes with the wake still queued is one notice. No key, ever: the queue is the director's or the owner's."""
    key = _queue_key(write)
    since = service.port.get_setting(key)
    if not idle:
        if since:
            service.port.set_setting(key, '')
    elif not since:
        service.port.set_setting(key, str(service.now()))
    elif service.now() - float(since) >= WEDGED_S:
        _publish(service, row, _wedged_id(write), 'A wake is stuck in a queue',
                 'the wake has sat in the agent\'s queue with no turn running. Drover presses no key; its mail waits in the inbox.')


def _active(seen, row):
    return seen['agent'].get('agent_status') in ACTIVE or pane_screen.status(seen['screen'], row['kind']) in ACTIVE


def _landed(write):
    """Sent, or seen in the queue. `wake_unverified` keeps the reason it came with, and None is what these two had."""
    return write['state'] in ('sent', 'queued') or (write['state'] == 'wake_unverified' and write['reason'] is None)


def _confirmed(service, row, write):
    try:
        return pane_send.confirm_landed(service.port, service.herdr, row)
    except PaneRefused:
        return write


def _progress(service, row, write, seen):
    """What this read proves. A running turn with this wake still queued is some other turn."""
    queued = pane_screen.queued_prompt(seen['screen'], TEXT)
    _watch_queue(service, row, write, queued and pane_screen.queued_idle(seen['screen']))
    if queued:
        if write['state'] == 'sent' or write['state'] in MAY_NOT_HAVE_LANDED:     # the queue proves it landed
            return ops_panes.finish_write(service.conn, write['id'], 'queued')
        return write
    if not _active(seen, row):
        return write
    if write['state'] in MAY_NOT_HAVE_LANDED:
        return _confirmed(service, row, write)
    if _landed(write):
        return ops_panes.finish_write(service.conn, write['id'], 'observed', TURN_SEEN)
    return write


def _verify(service, row, write, revision):
    try:
        write = _progress(service, row, write, pane_send.observe(service.herdr, row))
    except PaneRefused:
        pass                        # an unread pane proves nothing, and the ledger below still counts
    base = service.port.get_setting(_revision_key(row))
    acted = base is not None and revision > int(base)
    late = pane_launch.age(write['at'], service.now()) >= UNVERIFIED_S
    if acted and write['reason'] == TURN_SEEN:
        _settle(service, write, 'acknowledged', TURN_SEEN)
    elif acted and late:
        _settle(service, write, 'observed', ACTED)
    elif late and write['state'] != 'wake_unverified':
        ops_panes.finish_write(service.conn, write['id'], 'wake_unverified', write['reason'])
        _publish(service, row, _unverified_id(write), 'A wake was not acted on',
                 'a wake went out and the agent has not acted on it. Nothing is resent, and no more input goes to this pane until '
                 'it commits or the role moves to a new agent. Its mail waits in the inbox.')


def deliver(service, run) -> None:
    """Called for a live run of the current generation whose pane shows the recorded agent."""
    row = ops_panes.by_run(service.conn, run['run_id'])
    if row is None:
        return
    _close_rotated(service, row)
    if compaction_hold.active():
        return
    role_id = run['role_id']
    state = service.notifications.role_state(role_id)
    write = ops_panes.last_write(service.conn, row['id'], 'wake')
    if _outstanding(write):
        _verify(service, row, write, state['checkpoint_revision'])
    elif state['pending_wake'] and not ops_panes.open_writes(service.conn, row['id']) \
            and roles.wake_due(service.notifications, role_id):
        _send(service, row, role_id, state)
