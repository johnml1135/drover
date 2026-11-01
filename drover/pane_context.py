"""How full an agent's context is, or None. Unknown is an answer: nothing is requested or rotated on it.

Claude says so in its transcript under the seat's config folder: the last real assistant message's usage, or the size a
compaction left when one came after it. Codex says so only in its footer, as a percent of the window it was pinned to.

A WSL seat's transcript sits on the Linux side. Nothing here wakes WSL or starts a process in it: a held or stopped WSL
is unknown, and so is a home in `~` form, which only WSL can expand.
"""
import json

from pathlib import Path, PurePosixPath

from . import compaction_hold, config, pane_screen, pane_send, placement, runner
from .pane_files import NATIVE_COMPACT
from .pane_send import PaneRefused
from .resources import wsl_running

BOUNDARY = b'"subtype":"compact_boundary"'
ASSISTANT = b'"type":"assistant"'
TAIL_START = 1 << 21
USAGE_KEYS = ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')


def _parse(line):
    try:
        entry = json.loads(line)
    except ValueError:
        return None
    return entry if isinstance(entry, dict) else None


def _real_usage(entry):
    """The usage of a message of this session's own model, else None. One that says nothing is `{}`, which reads unknown."""
    message = entry.get('message')
    if entry.get('type') != 'assistant' or entry.get('isSidechain') or not isinstance(message, dict):
        return None
    return None if message.get('model') == '<synthetic>' else message.get('usage') or {}


def _scan(lines):
    """(last usage after the last boundary, what that boundary left). Usage wins when both are there."""
    usage = post = None
    for line in lines:
        if ASSISTANT not in line and BOUNDARY not in line:
            continue
        entry = _parse(line)
        if not entry:
            continue
        if entry.get('subtype') == 'compact_boundary':
            metadata = entry.get('compactMetadata')
            usage, post = None, metadata.get('postTokens') if isinstance(metadata, dict) else None
        elif (found := _real_usage(entry)) is not None:
            usage = found
    return usage, post


def _tail(path):
    """Only as much of the end of the file as it takes, so a long session costs a tick no more than a short one."""
    size = path.stat().st_size
    window = min(size, TAIL_START)
    while True:
        with open(path, 'rb') as stream:
            stream.seek(size - window)
            usage, post = _scan(stream.read().split(b'\n'))     # a window's cut first line does not parse, so it is skipped
        if usage is not None or post is not None or window >= size:
            return usage, post
        window = min(size, window * 4)


def _tokens(path):
    usage, post = _tail(path)
    if usage is None:
        return post if type(post) is int and post >= 0 else None
    if not isinstance(usage, dict):
        return None
    values = [usage.get(key) for key in USAGE_KEYS]     # a number not there is unknown, never 0: 0 would read as a compaction
    return sum(values) if all(type(value) is int and value >= 0 for value in values) else None


def _transcript(folder, session):
    return next(iter((folder / 'projects').glob(f'*/{session}.jsonl')), None)


def _folder(row, cfg):
    """The seat's config folder as this host can open it, or None. Reaching a Linux path needs a running WSL."""
    seat = config.seat_by_name(row['seat'], cfg)
    home = config.seat_env(seat).get('CLAUDE_CONFIG_DIR') if seat else None
    if not home or home.startswith('~'):
        return None
    if placement.is_linux_native(home):
        distro = placement.distro(cfg['paths']['wsl_distro'])
        if not placement.IS_WINDOWS:
            return Path(home)
        return Path('//wsl.localhost', distro, *PurePosixPath(home).parts[1:]) if wsl_running(distro) else None
    return Path(placement.native(home))


def _claude(row, cfg):
    if not runner.valid_worker_session(row.get('session_id')):
        return None
    try:
        folder = _folder(row, cfg)
        path = folder and _transcript(folder, row['session_id'])
        return _tokens(path) if path else None
    except (OSError, ValueError):
        return None


def _codex(row, herdr):
    try:
        left = pane_screen.context_left(pane_send.observe(herdr, row)['screen'])
    except PaneRefused:
        return None
    return None if left is None else int(NATIVE_COMPACT * (100 - left) / 100)


def read(row, herdr, cfg) -> int | None:
    """Tokens in the agent's context now. Anything unreadable is None."""
    if row['placement'] == 'wsl' and compaction_hold.active(config.state_dir(cfg)):
        return None
    return _codex(row, herdr) if row['kind'] == 'codex' else _claude(row, cfg)
