"""The only code that types into a pane. Anything uncertain is a PaneRefused and leaves the mail in the mailbox.

A write is noted first. `PaneRefused` means no write verb went out, and the note ends `unsent`. Once one has gone out,
nothing is resent and the write ends `sent` or `unverified`; the dict returned says which, and the reason says why.
`unverified` with any reason but `enter-pressed` may be finished by `submit_own`, which reads the pane first.

Text stays short and on one line, so a paste cannot collapse and the screen can prove it is Drover's. Longer text goes
in a file, and the pane gets a short line naming the path.

`port` must hold a ledger connection, as DirectPort does. Herdr's `idle` and `done` both mean ready for input: `done`
only says nobody has looked since the turn ended (references/cli.md:44 in the skill), and the hub never focuses a pane.
"""
import time
import unicodedata

from . import compaction_hold, pane_guard, pane_screen
from .herdr_client import HerdrRefused, HerdrUnavailable
from .ledger import ops_panes

KINDS = ('brief', 'wake', 'handoff', 'resume', 'stop', 'interrupt')
LIVE = ('live',)
STOPPABLE = ('live', 'compaction')
HERDR_READY = ('idle', 'done')
MAX_TEXT = 400              # a margin, not a measurement: Task 15 samples where each CLI collapses a paste
PASTE_SETTLE_S = 0.8
PASTE_START, PASTE_END = '\x1b[200~', '\x1b[201~'
PASTED = 'pasted'           # the text went out and its Enter is still to come
ENTER_PRESSED = 'enter-pressed'     # an Enter may have gone out: no second one, ever
BUSY_ROWS = pane_screen.CLAUDE_BUSY_ROWS


class PaneRefused(Exception):
    """No input went to the pane. `reason` is a fixed word, never screen or prompt text."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _check_kind(kind):
    if kind not in KINDS:
        raise PaneRefused('kind-refused')


def _check_text(text):
    """A flag, a line break or an escape could act on the terminal instead of reaching the composer."""
    if not isinstance(text, str) or not text.strip() or text.lstrip().startswith('-') or '\ufffd' in text or \
            any(unicodedata.category(c) in pane_guard.UNREADABLE for c in text):
        raise PaneRefused('text-refused')
    if len(text) > MAX_TEXT:
        raise PaneRefused('text-too-long')


def _current(port, row):
    """The ledger's row now: the caller's copy may be from before a rotation or a close."""
    if row is None:
        raise PaneRefused('no-row')
    stored = ops_panes.by_pane(port.conn, row.get('pane_id'))
    if stored is None or stored['id'] != row.get('id'):
        raise PaneRefused('no-row')
    return stored


def _owned(row, states):
    if row is None:
        raise PaneRefused('no-row')
    pane = row.get('pane_id')
    if row.get('started_by') != 'drover' or not isinstance(pane, str) or not pane or pane.startswith('-') or '\0' in pane:
        raise PaneRefused('not-drovers')
    if row.get('state') not in states or row.get('placement') not in ('windows', 'wsl'):
        raise PaneRefused('not-live')


def _read(herdr, row):
    """Agent last, so focus is the freshest value here."""
    pane_id = row['pane_id']
    try:
        screen = herdr.text('pane', 'read', pane_id, '--source', 'visible')
        pane = herdr.call('pane', 'get', pane_id)
        agent = herdr.call('agent', 'get', pane_id).get('agent')
    except HerdrUnavailable:
        raise PaneRefused('herdr-unavailable') from None
    except HerdrRefused as refused:
        raise PaneRefused(f'herdr-refused:{refused.code}') from None
    return dict(agent=agent if isinstance(agent, dict) else {}, pane=pane, screen=screen)


def _same_occupant(row, agent):
    session = agent.get('agent_session')
    session = session.get('value') if isinstance(session, dict) else None
    pairs = ((row.get('pane_id'), agent.get('pane_id')), (row.get('terminal_id'), agent.get('terminal_id')),
             (row.get('session_id'), session), (row.get('kind'), agent.get('agent')))
    if any(not recorded or recorded != live for recorded, live in pairs):
        raise PaneRefused('occupant-changed')


def _no_dialog(screen):
    try:
        pane_guard.refuse_dialog(screen)
        suspected = pane_guard.suspected_dialog(screen)
    except ValueError:
        raise PaneRefused('dialog') from None
    if suspected:
        raise PaneRefused('suspected-dialog')


def _safe(herdr, row, states, *, focus=True, idle=True):
    """Every guard that does not depend on the composer. `focus` and `idle` are dropped only for a compaction stop."""
    _owned(row, states)
    seen = _read(herdr, row)
    try:
        pane_guard.decode_screen(seen['screen'])
    except ValueError:
        raise PaneRefused('screen-unreadable') from None
    _same_occupant(row, seen['agent'])
    _no_dialog(seen['screen'])
    viewport = pane_screen.viewport_class(seen['pane'], seen['screen'])
    if viewport:
        raise PaneRefused(viewport)
    if focus and (seen['agent'].get('focused') is not False or seen['pane']['pane'].get('focused') is True):
        raise PaneRefused('focused')
    if idle and (seen['agent'].get('agent_status') not in HERDR_READY or seen['agent'].get('interactive_ready') is False):
        raise PaneRefused('busy')
    return seen


def _require_empty(seen, row):
    if not pane_screen.composer_empty(seen['screen'], row['kind']):
        raise PaneRefused('draft' if pane_screen.composer_text(seen['screen'], row['kind']) else 'screen-not-idle')


def check(herdr, row) -> dict:
    """The pane as read when it is safe to write to: Drover's, unchanged, undisturbed, idle and empty."""
    seen = _safe(herdr, row, LIVE)
    status = pane_screen.status(seen['screen'], row['kind'])
    if status == 'working':
        raise PaneRefused('busy')
    _require_empty(seen, row)
    if status != 'idle':
        raise PaneRefused('screen-not-idle')
    return seen


def _turn_visible(seen, row):
    """Any reader that sees a turn or a block. `status` says None of a full composer, so None here proves nothing,
    and a Claude spinner can sit out of `working`'s reach, so the last rows are searched as `status` searches them.
    """
    screen = seen['screen']
    rows = [line for line in screen.splitlines() if line.strip()][-BUSY_ROWS:]
    try:
        return pane_guard.working(screen) or pane_screen.status(screen, row['kind']) in ('working', 'blocked') or \
            any('esc to interrupt' in line.lower() for line in rows)
    except ValueError:
        raise PaneRefused('screen-unreadable') from None


def _live_hold(hold_id):
    hold = compaction_hold.active()
    if not hold_id or not hold or hold.get('id') != hold_id:
        raise PaneRefused('no-live-hold')


def _own_unsent(herdr, row, write, hold_id):
    """Refuses unless this read proves the composer holds exactly this write, with every other guard still holding."""
    if hold_id:
        _live_hold(hold_id)
        seen = _safe(herdr, row, STOPPABLE, focus=False, idle=False)
    else:
        seen = _safe(herdr, row, LIVE)
        if _turn_visible(seen, row):
            raise PaneRefused('busy')
    if not pane_screen.own_prompt(pane_screen.composer_text(seen['screen'], row['kind']), write['digest'], write['at'], None):
        raise PaneRefused('not-own')


def _enter(port, herdr, row, write, hold_id=None):
    """One Enter. It is on record before the last read, so that read is the last thing before the key."""
    finish = lambda state, reason=None: ops_panes.finish_write(port.conn, write['id'], state, reason)
    finish('unverified', ENTER_PRESSED)
    try:
        _own_unsent(herdr, row, write, hold_id)
    except PaneRefused as refused:
        return finish('unverified', refused.reason)
    try:
        herdr.call('pane', 'send-keys', row['pane_id'], 'enter')
    except (HerdrUnavailable, HerdrRefused):
        return finish('unverified', ENTER_PRESSED)
    return finish('sent')


def _write(port, herdr, row, kind, text, read, argv, after=('sent', None)):
    """Note the intent, read once more, make the one call. Never a second try and never another verb."""
    write = ops_panes.note_write(port.conn, row['id'], kind, text)
    finish = lambda state, reason=None: ops_panes.finish_write(port.conn, write['id'], state, reason)
    try:
        read()
    except PaneRefused as refused:
        finish('unsent', refused.reason)
        raise
    try:
        herdr.call(*argv)
    except HerdrUnavailable:        # a timeout may follow a write that landed
        return finish('unverified', 'herdr-unavailable')
    except HerdrRefused as refused:
        reason = f'herdr-refused:{refused.code}'
        finish('unsent', reason)
        raise PaneRefused(reason) from None
    return finish(*after)


def _paste(port, herdr, row, kind, text, read, hold_id=None):
    argv = ('pane', 'send-text', row['pane_id'], PASTE_START + text + PASTE_END)
    write = _write(port, herdr, row, kind, text, read, argv, ('unverified', PASTED))
    if write['reason'] != PASTED:
        return write
    time.sleep(PASTE_SETTLE_S)
    return _enter(port, herdr, row, write, hold_id)


def send(port, herdr, row, text, kind) -> dict:
    """Write `text` once to Drover's idle, unfocused pane, and return the finished write."""
    _check_kind(kind)
    _check_text(text)
    row = _current(port, row)
    first = check(herdr, row)

    def read():
        if check(herdr, row)['screen'] != first['screen']:
            raise PaneRefused('screen-changed')
    read()
    if row['placement'] == 'windows':
        return _write(port, herdr, row, kind, text, read, ('agent', 'prompt', row['pane_id'], text))
    return _paste(port, herdr, row, kind, text, read)


def submit_own(port, herdr, row) -> dict:
    """One Enter for a paste that is still in the composer. `classify` may suggest this; only this read decides it."""
    row = _current(port, row)
    _owned(row, LIVE)
    write = ops_panes.last_write(port.conn, row['id'])
    if write is None or write['state'] != 'unverified' or write['reason'] == ENTER_PRESSED or write['kind'] == 'interrupt':
        raise PaneRefused('nothing-unsent')
    done = _enter(port, herdr, row, write)
    if done['state'] != 'sent' and done['reason'] != ENTER_PRESSED:
        raise PaneRefused(done['reason'])
    return done


def _interruptible(herdr, row):
    # ^c copies in a viewport and clears a draft, so both refuse (ruling 3). Only the compaction stop may reach focus.
    stopping = row.get('state') == 'compaction' and row.get('placement') == 'wsl' and compaction_hold.active() is not None
    _require_empty(_safe(herdr, row, STOPPABLE, focus=not stopping, idle=False), row)


def interrupt(port, herdr, row, kind='interrupt') -> dict:
    """One ctrl+c to Drover's own pane, for a turn that will not stop. Never repeated here."""
    _check_kind(kind)
    row = _current(port, row)
    read = lambda: _interruptible(herdr, row)
    read()
    return _write(port, herdr, row, kind, 'ctrl+c', read, ('pane', 'send-keys', row['pane_id'], 'ctrl+c'))


def send_stop(port, herdr, row, text, hold_id) -> dict:
    """The single exception: the compaction stop drops the idle and unfocused tests, and nothing else.

    A dialog or a draft still refuses, because the Enter could confirm a selected option or submit the owner's text.
    The screens are not compared, as a working pane's timer moves between reads.
    """
    _check_text(text)
    row = _current(port, row)
    if row['placement'] != 'wsl':
        raise PaneRefused('not-wsl')

    def read():
        _live_hold(hold_id)
        _require_empty(_safe(herdr, row, STOPPABLE, focus=False, idle=False), row)
    read()
    return _paste(port, herdr, row, 'stop', text, read, hold_id)
