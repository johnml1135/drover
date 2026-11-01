"""The only code that types into a pane. Anything uncertain is a PaneRefused and leaves the mail in the mailbox.

A write is noted first. `PaneRefused` means no write verb went out, and the note ends `unsent`. Once one has gone out,
nothing is resent and the write ends `sent` or `unverified`; the dict returned says which, and the reason says why.
`unverified` with any reason but `enter-pressed` may be finished by `submit_own`, which reads the pane first.
Any write not known to be over stays open and stops the next write to that agent, however long ago it was made, until
`confirm_landed` sees its turn or the caller records what became of it with `ops_panes.finish_write`. A refusal from
Herdr ends `unsent` only for the codes in NOTHING_WENT_OUT; the rest may follow a write that landed.

Every path needs proof the agent is the pane's foreground process, because a shell prompt can look like a composer.
Herdr sees only wsl.exe for a WSL agent, so a WSL pane must also show something only the agent draws.

The new pane's writes each go once per row, claimed in the ledger. pane_launch closes a stalled one itself and ends the
row, so a launch is never resent. A Windows launch is one shell line for its environment, then `agent start`.

Text stays short and on one line, so a paste cannot collapse and the screen can prove it is Drover's. Longer text goes
in a file, and the pane gets a short line naming the path.

`port` must hold a ledger connection, as DirectPort does. Herdr's `idle` and `done` both mean ready for input: `done`
only says nobody has looked since the turn ended (references/cli.md:44 in the skill), and the hub never focuses a pane.
"""
import re
import time
import unicodedata

from . import compaction_hold, pane_guard, pane_screen
from .herdr_client import HerdrRefused, HerdrUnavailable
from .ledger import ops_panes
from .pane_read import (LIVE, PaneRefused, _agent_in_foreground, _ask, _lone_shell, _owned, _require_empty, _safe, check,
                        observe, shell_alone)

TYPED_KINDS = ('brief', 'wake', 'handoff', 'resume')
INTERRUPT_KEY = 'ctrl+c'
STARTING = ('starting',)
LAUNCHES = ('shell', 'start')
STOPPABLE = ('live', 'compaction')
MAX_TEXT = 400              # a margin, not a measurement: Task 15 samples where each CLI collapses a paste
PASTE_SETTLE_S = 0.8
PASTE_START, PASTE_END = '\x1b[200~', '\x1b[201~'
NOTHING_WENT_OUT = ('usage', 'agent_blocked', 'pane_not_found')     # Task 15 may extend this from real errors
SHELL_PROMPT = re.compile(r'(?:PS [^>]*>|[A-Za-z]:[\\/][^>]*>|[^\s$#%]*[$#%])\s*')  # Task 15 adds the owner's real prompts
PASTED = 'pasted'           # the text went out and its Enter is still to come
ENTER_PRESSED = 'enter-pressed'     # an Enter may have gone out: no second one, ever


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


def _newest_open(port, row):
    writes = ops_panes.open_writes(port.conn, row['id'])
    return writes[-1] if writes else None


def _refuse_open_write(port, row):
    """Every open write counts, so nothing written since, and no refused attempt, can release one."""
    write = _newest_open(port, row)
    if write:
        raise PaneRefused(f"last-write-{ENTER_PRESSED if write['reason'] == ENTER_PRESSED else write['state']}")


def _turn_visible(seen, row):
    """`status` says None of a full composer, so only its positive answers count here."""
    return pane_screen.turn_showing(seen['screen']) or pane_screen.status(seen['screen'], row['kind']) in ('working', 'blocked')


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
    """One Enter, or None when another caller claimed it. The claim comes before the last read, so that read is the
    last thing before the key, and a crash can only ever stop an Enter.
    """
    if ops_panes.claim_enter(port.conn, write['id'], write['state'], write['reason']) is None:
        return None
    finish = lambda state, reason=None: ops_panes.finish_write(port.conn, write['id'], state, reason)
    try:
        _own_unsent(herdr, row, write, hold_id)
    except PaneRefused as refused:
        return finish('unverified', refused.reason)
    try:
        herdr.call('pane', 'send-keys', row['pane_id'], 'enter')
    except (HerdrUnavailable, HerdrRefused):
        return finish('unverified', ENTER_PRESSED)
    return finish('sent')


def _note(port, row, kind, text, once):
    if not once:
        return ops_panes.note_write(port.conn, row['id'], kind, text)
    write = ops_panes.note_once(port.conn, row['id'], once, kind, text)
    if write is None:
        raise PaneRefused('already-started')
    return write


def _write(port, herdr, row, kind, text, read, argv, after=('sent', None), once=()):
    """Note the intent, read once more, make the one call. Never a second try and never another verb."""
    write = _note(port, row, kind, text, once)
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
        if refused.code not in NOTHING_WENT_OUT:
            return finish('unverified', reason)
        finish('unsent', reason)
        raise PaneRefused(reason) from None
    return finish(*after)


def _paste(port, herdr, row, kind, text, read, hold_id=None):
    argv = ('pane', 'send-text', row['pane_id'], PASTE_START + text + PASTE_END)
    write = _write(port, herdr, row, kind, text, read, argv, ('unverified', PASTED))
    if write['reason'] != PASTED:
        return write
    time.sleep(PASTE_SETTLE_S)
    return _enter(port, herdr, row, write, hold_id) or ops_panes.last_write(port.conn, row['id'])


def send(port, herdr, row, text, kind) -> dict:
    """Write `text` once to Drover's idle, unfocused pane, and return the finished write."""
    if kind not in TYPED_KINDS:
        raise PaneRefused('kind-refused')
    _check_text(text)
    row = _current(port, row)
    _refuse_open_write(port, row)
    first = check(herdr, row)

    def read():
        if check(herdr, row)['screen'] != first['screen']:
            raise PaneRefused('screen-changed')
    read()
    if row['placement'] == 'windows':
        return _write(port, herdr, row, kind, text, read, ('agent', 'prompt', row['pane_id'], text))
    return _paste(port, herdr, row, kind, text, read)


def _submittable(write):
    """Only text Drover typed, on a record that says it went out and that no Enter followed."""
    return write is not None and write['kind'] in (*TYPED_KINDS, 'stop') and write['head'] != INTERRUPT_KEY and \
        write['state'] == 'unverified' and write['reason'] != ENTER_PRESSED


def submit_own(port, herdr, row) -> dict:
    """One Enter for a paste that is still in the composer. `classify` may suggest this; only this read decides it."""
    row = _current(port, row)
    _owned(row, STOPPABLE)
    write = _newest_open(port, row)
    if not _submittable(write):
        raise PaneRefused('nothing-unsent')
    hold_id = None
    if write['kind'] == 'stop':
        hold_id = (compaction_hold.active() or {}).get('id')
        _live_hold(hold_id)
    done = _enter(port, herdr, row, write, hold_id)
    if done is None:
        raise PaneRefused('enter-claimed')
    if done['state'] != 'sent' and done['reason'] != ENTER_PRESSED:
        raise PaneRefused(done['reason'])
    return done


def _prompt_then_turn(screen, kind, write):
    """This write's own text as the last past prompt, and a live turn below it. Nothing else on the screen counts."""
    lines = screen.splitlines()
    marks = [i for i, line in enumerate(lines) if re.match(pane_screen.COMPOSER[kind][0], line)]
    if len(marks) < 2:
        return False
    prompt, composer = marks[-2:]
    end = next((i for i in range(prompt + 1, composer) if not lines[i].strip()), composer)
    text = ' '.join([lines[prompt].strip()[1:]] + lines[prompt + 1:end])
    return pane_screen.own_prompt(text, write['digest'], write['at'], None) and pane_guard.working('\n'.join(lines[end:]))


def confirm_landed(port, herdr, row) -> dict:
    """Close a typed write on one read showing it submitted and a turn under it, on Herdr and on screen alike.

    The write was made to an idle pane, so that turn began after it. Anything less leaves the write open and mail goes:
    an idle pane, history text, either status alone, or a Claude screen, whose past prompts have no capture yet.
    """
    row = _current(port, row)
    write = _newest_open(port, row)
    if write is None or write['kind'] not in TYPED_KINDS:
        raise PaneRefused('nothing-open')
    seen = _safe(herdr, row, STOPPABLE, idle=False)
    _require_empty(seen, row)
    if seen['agent'].get('agent_status') != 'working' or not _prompt_then_turn(seen['screen'], row['kind'], write):
        raise PaneRefused('turn-not-seen')
    return ops_panes.finish_write(port.conn, write['id'], 'observed', 'turn-seen')


def _interruptible(herdr, row):
    # ^c copies in a viewport and clears a draft, so both refuse (ruling 3). Only the compaction stop may reach focus.
    stopping = row.get('state') == 'compaction' and row.get('placement') == 'wsl' and compaction_hold.active() is not None
    _require_empty(_safe(herdr, row, STOPPABLE, focus=not stopping, idle=False), row)


def interrupt(port, herdr, row, kind='interrupt') -> dict:
    """One ctrl+c to Drover's own pane, for a turn that will not stop. Never repeated here."""
    if kind != 'interrupt':     # its record must never pass for text Drover typed
        raise PaneRefused('kind-refused')
    row = _current(port, row)
    read = lambda: _interruptible(herdr, row)
    read()
    return _write(port, herdr, row, kind, INTERRUPT_KEY, read, ('pane', 'send-keys', row['pane_id'], INTERRUPT_KEY))


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


def _starting(herdr, row, foreground):
    """A new pane before its agent reports: no session and maybe no agent, so the pane itself must answer."""
    _owned(row, STARTING)
    foreground(herdr, row)
    screen = _ask(herdr.text, 'pane', 'read', row['pane_id'], '--source', 'visible')
    pane = _ask(herdr.call, 'pane', 'get', row['pane_id'])
    viewport = pane_screen.viewport_class(pane, screen)
    if viewport:
        raise PaneRefused(viewport)
    if not row.get('terminal_id') or pane['pane'].get('terminal_id') != row['terminal_id']:
        raise PaneRefused('occupant-changed')
    if pane['pane'].get('focused') is not False:
        raise PaneRefused('focused')
    return screen


def _shell_line_empty(port, herdr, row):
    rows = [line for line in _starting(herdr, row, _lone_shell).splitlines() if line.strip()]
    if not rows or not SHELL_PROMPT.fullmatch(rows[-1]):
        raise PaneRefused('shell-line-not-empty')
    ran = ops_panes.ran_on_pane(port.conn, row['pane_id'], row.get('terminal_id'))
    for above in rows[:-1]:     # a wrapped draft: its first row is a prompt with text, its tail may pass as a prompt
        found = SHELL_PROMPT.match(above := above.rstrip())
        if found and found.end() < len(above) and (rows[-1].strip() != found.group().strip()
                                                   or pane_screen.prompt_fingerprint(above[found.end():])[0] not in ran):
            raise PaneRefused('shell-line-not-empty')   # exempt only a recorded echo whose own prompt form is the last row


def _launched(port, row):
    """The row's own launch went out, so the agent in this `starting` row is the one Drover put here."""
    return any((ops_panes.last_write(port.conn, row['id'], kind) or {}).get('state') == 'sent' for kind in LAUNCHES)


def answer_startup(port, herdr, row, *, trusted=False) -> dict:
    """The one Enter for Codex's folder trust dialog, once for a row. The caller proves `trusted`; the row proves fresh."""
    if trusted is not True:
        raise PaneRefused('not-trusted')
    row = _current(port, row)
    _refuse_open_write(port, row)
    if not _launched(port, row) or ops_panes.reused_pane(port.conn, row):
        raise PaneRefused('not-fresh')

    def read():
        keys = pane_guard.startup_keys(_starting(herdr, row, _agent_in_foreground), fresh_pane=True, kind=row['kind'])
        if keys != ('enter',):
            raise PaneRefused('not-the-trust-dialog')
    read()
    return _write(port, herdr, row, 'startup', 'enter', read, ('pane', 'send-keys', row['pane_id'], 'enter'),
                  once=('startup',))


def start_in_shell(port, herdr, row, command) -> dict:
    """`pane run`, once for a row: one short command while the shell is alone in the foreground, on an empty line."""
    _check_text(command)
    row = _current(port, row)
    _refuse_open_write(port, row)
    read = lambda: _shell_line_empty(port, herdr, row)
    read()
    return _write(port, herdr, row, 'shell', command, read, ('pane', 'run', row['pane_id'], command), once=LAUNCHES)


def start_agent(port, herdr, row, name, args=()) -> dict:
    """`agent start`, once for a row, under the same guards. `args` go after `--`, so they may be flags.

    It may follow the row's one shell line, which only sets the environment; nothing may follow it.
    """
    _check_text(name)
    if not isinstance(args, (list, tuple)):
        raise PaneRefused('text-refused')
    for arg in args:        # a flag is fine after `--`; the rest of the gate still applies
        _check_text('x' + arg if isinstance(arg, str) else None)
    row = _current(port, row)
    if row['placement'] != 'windows':
        raise PaneRefused('not-windows')
    _refuse_open_write(port, row)
    read = lambda: _shell_line_empty(port, herdr, row)
    read()
    argv = ('agent', 'start', name, '--kind', row['kind'], '--pane', row['pane_id'], '--', *args)
    return _write(port, herdr, row, 'start', name, read, argv, once=('start',))
