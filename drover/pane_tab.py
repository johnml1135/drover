"""What a launch reads from its pane, and the new tab it opens. Nothing here types, and nothing here touches the ledger."""
from . import pane_guard, pane_send
from .herdr_client import HerdrRefused, HerdrUnavailable
from .launch import AdmissionHeld
from .pane_send import PaneRefused


def open_pane(herdr, cfg, cwd) -> str:
    """A new tab that takes no focus. Herdr's own errors are the caller's to hold on."""
    workspace = cfg['panes']['workspace']
    if not workspace:
        listed = herdr.call('workspace', 'list').get('workspaces') or []
        if not listed:
            raise AdmissionHeld('herdr-no-workspace')
        workspace = listed[0]['workspace_id']
    return herdr.call('tab', 'create', '--workspace', workspace, '--cwd', cwd, '--no-focus')['root_pane']['pane_id']


def _terminal(herdr, pane_id):
    try:
        pane = herdr.call('pane', 'get', pane_id).get('pane')
    except (HerdrUnavailable, HerdrRefused):
        return None
    return pane.get('terminal_id') or None if isinstance(pane, dict) else None


def _read_alone(herdr, row):
    """What the failure record says: True, False, or None when the pane cannot be read."""
    try:
        return pane_send.shell_alone(herdr, row)
    except PaneRefused:
        return None


def _no_agent(herdr, row):
    try:
        return pane_send.shell_alone(herdr, row)
    except PaneRefused as refused:
        return refused.reason == 'herdr-refused:pane_not_found'


def _dialog(screen):
    try:
        pane_guard.refuse_dialog(screen)
        return pane_guard.suspected_dialog(screen)
    except ValueError:
        return True


def session_of(agent):
    session = agent.get('agent_session')
    return session.get('value') if isinstance(session, dict) else None


def _look(herdr, row):
    alone = pane_send.shell_alone(herdr, row)
    try:
        screen = herdr.text('pane', 'read', row['pane_id'], '--source', 'visible')
        try:
            agent = herdr.call('agent', 'get', row['pane_id']).get('agent')
        except HerdrRefused:        # no agent has reported here yet
            agent = None
    except (HerdrUnavailable, HerdrRefused):
        raise PaneRefused('herdr-unavailable') from None
    return alone, screen, agent if isinstance(agent, dict) else {}


def _rename(herdr, row):
    if row['placement'] == 'wsl':
        try:
            herdr.call('agent', 'rename', row['pane_id'], row['name'])
        except (HerdrUnavailable, HerdrRefused):
            pass                # a name is a label: Task 10 restores lost ones


def _stall_reason(herdr, row):
    """Herdr gives up on an agent held at a dialog too, and the owner needs to hear which."""
    try:
        return 'dialog' if _dialog(herdr.text('pane', 'read', row['pane_id'], '--source', 'visible')) else 'launch-stalled'
    except (HerdrUnavailable, HerdrRefused):
        return 'launch-stalled'


def _workspace(herdr, cfg):
    """`open_pane` would have somewhere to open: the configured workspace, or any one if none is set."""
    listed = [entry['workspace_id'] for entry in herdr.call('workspace', 'list').get('workspaces') or []]
    wanted = cfg['panes']['workspace']
    if not listed or (wanted and wanted not in listed):
        raise AdmissionHeld('herdr-no-workspace')
