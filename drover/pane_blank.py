"""A tab Drover opened that never showed a prompt, closed if nothing has touched it and the owner has closes on.

Off, as it is by default, the tab stays: every close waits on the owner's switch (plan row 19, "dry first", and ruling 23), and
a row that failed here frees its role once a read shows no agent, so a tab left behind costs the owner one tab to close.
"""
from . import pane_send
from .herdr_client import HerdrRefused, HerdrUnavailable
from .ledger import ops_panes
from .pane_send import PaneRefused


def close_blank(port, herdr, row):
    """Close a tab Drover opened that never showed a prompt. True only if it did: anything on it, and it stays."""
    last = ops_panes.last_write(port.conn, row['id'])
    if not pane_send.close_enabled(port) or ops_panes.reused_pane(port.conn, row) or (last and last['state'] != 'unsent'):
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
