"""What pane upkeep does with an exception: an expected one stays that step's trouble, and any other is a bug the owner hears of.

Herdr refusing or closed, a timeout, an OS error and a busy ledger are expected, and are logged by class alone. Anything else
is a mistake in Drover, so it never passes for an outage: its traceback goes to the log and one notice to the Inbox, once per
place. A tick carries on either way. No exception's text is ever written, because it may quote a path or a credential: a
traceback's frames show only where it happened.
"""
import sqlite3
import traceback
from pathlib import Path

from .herdr_client import HerdrRefused, HerdrUnavailable
from .launch import AdmissionHeld
from .ledger import disk_notices, ops_panes
from .pane_read import PaneRefused

EXPECTED = (HerdrRefused, HerdrUnavailable, PaneRefused, AdmissionHeld, OSError)
LEDGER_BUSY = (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


def _publish(port, notice_id, title, body):
    disk_notices.publish(port.conn, dict(id=notice_id, kind='pane', title=title, body=body), kind='pane.notice',
                         epic_title='Directors', actor='panes')


def expected(error) -> bool:
    """Only a ledger that is busy: any other OperationalError is a query that cannot run."""
    if isinstance(error, sqlite3.OperationalError):
        return (vars(error).get('sqlite_errorcode', 0) & 0xff) in LEDGER_BUSY
    return isinstance(error, EXPECTED)


def _place(error) -> str:
    frames = traceback.extract_tb(error.__traceback__)
    return f'{Path(frames[-1].filename).name}:{frames[-1].lineno}' if frames else 'unknown'


def _tell(port, log, step, error):
    name, place = type(error).__name__, _place(error)
    try:
        _publish(port, f'pane-fault:{name}:{place}', 'A bug stopped a pane step',
                 f'{step} failed with {name} at {place}. The tick went on, but that step does nothing until this is fixed; '
                 'the traceback is in the hub log.')
    except Exception as failed:     # the log has it, and no notice may cost the tick
        log.error('pane fault notice failed: %s', type(failed).__name__)


def note(port, log, step, error) -> None:
    """Log one caught exception, and tell the owner if it is a bug. Call it from the `except`."""
    if expected(error):
        log.warning('%s failed: %s', step, type(error).__name__)
        return
    frames = ''.join(traceback.format_tb(error.__traceback__))
    log.error('%s hit a bug: %s\n%s', step, type(error).__name__, frames.rstrip())
    _tell(port, log, step, error)


def left_running(port) -> None:
    """The hub came up with panes off and a director still in one. No tick reads that row now, so the owner hears of it."""
    for row in ops_panes.running_agents(port.conn):
        _publish(port, f"pane-left-running:{row['id']}", 'A director is still in a pane',
                 f"Seat {row['seat']} ({row['placement']}), pane {row['pane_id']}: panes are off, so Drover cannot see or stop this "
                 'agent, and its run keeps its slot. Turn panes back on and follow the rollback in docs/operations/directors-rollout.md.')
