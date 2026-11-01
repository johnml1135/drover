"""What a pane write must first prove, read from Herdr. Nothing here writes: only `pane_send` does.

Every guard refuses with a PaneRefused, whose reason is a fixed word. `check` is the whole test for a write to an idle
pane; `shell_alone` and `observe` read a pane and grant no write.
"""
from . import pane_guard, pane_screen
from .herdr_client import HerdrRefused, HerdrUnavailable

LIVE = ('live',)
HERDR_READY = ('idle', 'done')
AGENT_PROCESSES = dict(windows=('claude', 'node'), wsl=('wsl',))    # lower case, no .exe; Task 15 samples the real names


class PaneRefused(Exception):
    """No input went to the pane. `reason` is a fixed word, never screen or prompt text."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _owned(row, states):
    if row is None:
        raise PaneRefused('no-row')
    pane = row.get('pane_id')
    if row.get('started_by') != 'drover' or not isinstance(pane, str) or not pane or pane.startswith('-') or '\0' in pane:
        raise PaneRefused('not-drovers')
    if row.get('state') not in states or row.get('placement') not in ('windows', 'wsl'):
        raise PaneRefused('not-live')


def _ask(ask, *argv):
    try:
        return ask(*argv)
    except HerdrUnavailable:
        raise PaneRefused('herdr-unavailable') from None
    except HerdrRefused as refused:
        raise PaneRefused(f'herdr-refused:{refused.code}') from None


def _processes(herdr, row):
    """(shell pid, [(pid, name)]), or None when Herdr's answer does not say."""
    info = _ask(herdr.call, 'pane', 'process-info', '--pane', row['pane_id']).get('process_info')
    listed = info.get('foreground_processes') if isinstance(info, dict) else None
    if not isinstance(listed, list) or type(info.get('shell_pid')) is not int or \
            not all(isinstance(entry, dict) and type(entry.get('pid')) is int for entry in listed):
        return None
    return info['shell_pid'], [(entry['pid'], str(entry.get('name') or '').lower().removesuffix('.exe')) for entry in listed]


def _agent_in_foreground(herdr, row):
    found = _processes(herdr, row)
    if found is None or not any(pid != found[0] and name in AGENT_PROCESSES[row['placement']] for pid, name in found[1]):
        raise PaneRefused('agent-not-foreground')


def _lone_shell(herdr, row):
    found = _processes(herdr, row)
    if found is None or not found[1] or any(pid != found[0] for pid, _ in found[1]):
        raise PaneRefused('shell-not-alone')


def _read(herdr, row):
    """Agent last, so focus is the freshest value here."""
    pane_id = row['pane_id']
    screen = _ask(herdr.text, 'pane', 'read', pane_id, '--source', 'visible')
    pane = _ask(herdr.call, 'pane', 'get', pane_id)
    agent = _ask(herdr.call, 'agent', 'get', pane_id).get('agent')
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


def _readable(screen):
    try:
        pane_guard.decode_screen(screen)
    except ValueError:
        raise PaneRefused('screen-unreadable') from None


def _occupied(herdr, row):
    seen = _read(herdr, row)
    _readable(seen['screen'])
    _same_occupant(row, seen['agent'])
    return seen


def _agent_marked(screen):
    """A `❯` row is a shell's prompt too, so one must sit against a fence or a footer no shell draws."""
    rows = [line for line in screen.splitlines() if line.strip()]
    marks = [i for i, line in enumerate(rows) if line.lstrip().startswith('❯')]
    if not marks:
        return True
    near = rows[max(0, marks[-1] - 1):marks[-1]] + rows[marks[-1] + 1:]
    return any(pane_screen.SEPARATOR.fullmatch(line) or '⏵⏵' in line for line in near)


def _safe(herdr, row, states, *, focus=True, idle=True):
    """Every guard that does not depend on the composer. `focus` and `idle` are dropped only for a compaction stop."""
    _owned(row, states)
    _agent_in_foreground(herdr, row)
    seen = _occupied(herdr, row)
    _no_dialog(seen['screen'])
    if row['placement'] == 'wsl' and not _agent_marked(seen['screen']):
        raise PaneRefused('agent-not-marked')
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


def shell_alone(herdr, row) -> bool:
    """True only when Herdr shows the shell alone in the foreground. A read that fails is a PaneRefused."""
    try:
        _lone_shell(herdr, row)
    except PaneRefused as refused:
        if refused.reason != 'shell-not-alone':
            raise
        return False
    return True


def observe(herdr, row) -> dict:
    """The pane as read now, with its occupant proved unchanged. Grants no write."""
    return _occupied(herdr, row)
