"""A tab Drover opened that never showed a prompt, closed if nothing has touched it.

This is the one close `pane_close_enabled` does not rule, by the owner's ruling: the tab is Drover's own and never held
anything, and nothing else may close it. Its guards below decide alone, and it goes through `pane_send.close_pane`.
"""
from . import pane_send
from .herdr_client import HerdrRefused, HerdrUnavailable
from .ledger import ops_panes
from .pane_send import PaneRefused


def close_blank(port, herdr, row):
    """Close a tab Drover opened that never showed a prompt. True only if it did: anything on it, and it stays."""
    last = ops_panes.last_write(port.conn, row['id'])
    if ops_panes.reused_pane(port.conn, row) or (last and last['state'] != 'unsent'):
        return False
    try:
        pane = herdr.call('pane', 'get', row['pane_id']).get('pane')
        if not isinstance(pane, dict) or not pane_send.shell_alone(herdr, row) or \
                herdr.text('pane', 'read', row['pane_id'], '--source', 'visible').strip() or \
                pane.get('focused') is not False or pane.get('terminal_id') != row['terminal_id']:
            return False
        pane_send.close_pane(port, herdr, row)
    except (PaneRefused, HerdrUnavailable, HerdrRefused):
        return False
    return True
