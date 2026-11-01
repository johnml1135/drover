"""What a pane screen says about its agent. Pure text rules: this module never talks to Herdr.

`kind` is 'claude' or 'codex'. An unrecognised kind or layout reads as None, and None never permits input.
"""
from functools import wraps
import hashlib
import re

from .pane_guard import (
    blocking_dialog,
    bottom_region,
    decode_screen,
    model_prompt,
    queued_input_range,
    strong_dialog,
    suspected_dialog,
    working,
)

STALE = 900
WEDGE = 600  # Codex drains its follow-up queue when a turn ends; ten idle minutes with a queue is a wedge

WARNING_FOOTER = r'⚠\s*(?:\d+(?:\s+warnings?)?\s*·\s*f\d+(?:\s+to view)?|weekly limit: \d+% left\s*·\s*(?:resets at \d{2}:\d{2}|/status))'
# Current Codex uses friendly model labels; accept only these observed names with an effort.
FRIENDLY_MODEL = r'gpt-6\.1\s+(?:terra|sol)'
FRIENDLY_LOCATION = r'(?:~?/|[A-Z]:[\\/]|\\\\)[^\s·]*'
# Narrower than the skill: text after a model row's "·" must be a path or a context reading, so owner text is no footer.
MODEL_TAIL = (r'\s*·\s*(?:\d+% context left(?:\s*·\s*' + FRIENDLY_LOCATION + r')?|' +
              FRIENDLY_LOCATION + r'(?:\s*·\s*[^·]+)?)')
CUT_EFFORT = 'l|lo|low|m|me|med|medi|mediu|h|hi|hig|high|x|xh|xhi|xhig|xhigh'  # narrower than the skill's five letters
FOOTER = re.compile(
    r'(?:(?:← for agents\s*·\s*)?\? for shortcuts(?:\s+' + WARNING_FOOTER + r')?|\? fo(?:…|\.\.\.)|\?\s+⚠\s*\d+(?:\s*·\s*f\d+)?|'
    r'New activity\s*·\s*enter/esc latest(?:\s*·\s*\? (?:for )?shortcuts)?(?:\s+' + WARNING_FOOTER + r')?|'
    r'\d+% context left|' + FRIENDLY_MODEL + r'\s+(?:low|medium|high|xhigh)\s*·\s*' +
    FRIENDLY_LOCATION + r'(?:\s*·\s*[^·]+)?|'  # optional thread title / "Read briefs"
    r'gpt-[\w.\-]+…|gpt-[\w.\-]+\s+(?:' + CUT_EFFORT + r')…|gpt-[\w.\-]+\s+(?:low|medium|high|xhigh)' + MODEL_TAIL + r'|'
    + WARNING_FOOTER + r')', re.I)
SEPARATOR = re.compile(r'\s*[─━]{3,}\s*')  # narrower than the skill: `-` and `---` are text an owner can type
EMPTY_COMPOSER = ('', 'Ask Codex to do anything')
PLACEHOLDER_MIN = 7  # a narrow pane cuts the placeholder to "Ask Cod"; shorter text may be an owner's draft
VIEWPORT_CONTROL = re.compile(
    r'^(?:New activity\s*·\s*)?(?:\^c copy\s*·\s*enter copy & follow\s*·\s*esc clear|'
    r'enter/esc latest\b.*|(?:↓\s*)?Back to bottom\s*·\s*esc)$', re.I)
FOOTER_MODEL = re.compile(r'\b(' + FRIENDLY_MODEL + r'|gpt-[\w.\-]*\w)\s+(low|medium|high|xhigh)\b', re.I)
CONTEXT_LEFT = re.compile(r'(?<![\d.])(\d{1,3})% context left', re.I)
STUCK_CLASSES = ('rate_limit', 'model_prompt', 'approval', 'question', 'trust', 'usage_limit', 'unsent_prompt', 'stale_working',
                 'error_turn', 'exited', 'wake_unverified', 'scope_stale', 'queued_wedged', 'viewport_held', 'viewport_unknown',
                 'screen_unknown')
BUSY_ROWS = 12
LIVE_TURN = re.compile(r'esc to interrupt|^\s*•\s*Working\b', re.I)
# Where a real Codex answer starts. Only this ends the composer walk; Claude's answer row has no capture yet, so has none.
ANSWER_ROW = dict(codex=re.compile(r'•\s'))


def _unknown(result):
    """An unreadable capture answers `result`, the reading that permits nothing."""
    def wrap(read):
        @wraps(read)
        def guarded(screen, *args):
            try:
                return read(decode_screen(screen), *args)
            except ValueError:
                return result
        return guarded
    return wrap


def empty_composer(text):
    if text in EMPTY_COMPOSER:
        return True
    text = text.rstrip('…').rstrip()
    return len(text) >= PLACEHOLDER_MIN and EMPTY_COMPOSER[1].startswith(text)


def is_footer(line):
    # Recognize full known footer forms, including truncation, without accepting arbitrary draft prefixes.
    return not line.strip() or bool(SEPARATOR.fullmatch(line)) or bool(FOOTER.fullmatch(line.strip()))


def _claude_footer(line):
    # Narrower than send-prompt.py:33: `---` and text after "auto mode on" may be the owner's.
    return bool(not line.strip() or SEPARATOR.fullmatch(line) or re.fullmatch(r'\s*⏵⏵ auto mode on\s*', line))


def _codex_idle(lines):
    prompts = [i for i, line in enumerate(lines) if re.match(r'^\s*›', line)]
    if not prompts:
        return False
    index = prompts[-1]
    # A multiline owner draft can have an empty first line. Only the known footer,
    # blank rows and separators may follow the prompt; unknown text retains input.
    text = lines[index].strip()[1:].strip()
    # A cut placeholder counts only where a cut footer proves the pane is narrow: elsewhere those words are a draft.
    narrow = any('…' in line for line in lines[index + 1:])
    return (text in EMPTY_COMPOSER or narrow and empty_composer(text)) and all(is_footer(line) for line in lines[index + 1:])


def _claude_idle(lines):
    prompts = [i for i, line in enumerate(lines) if re.match(r'^\s*❯', line)]
    if not prompts:
        return False
    index = prompts[-1]
    return lines[index].strip() == '❯' and all(_claude_footer(line) for line in lines[index + 1:])


IDLE = dict(claude=_claude_idle, codex=_codex_idle)
COMPOSER = dict(claude=(r'^\s*❯', _claude_footer), codex=(r'^\s*›', is_footer))


def _composer_top(lines, index, kind):
    """The highest mark above `index` that nothing explains: any such mark means the last mark sits inside a draft.

    A blank row or a separator proves nothing, because a draft can hold either. Only an answer the agent printed shows
    that what lies above it is a finished turn: in the first column, under a blank row. Indented, or tight under other
    text, that row can be pasted text. A past prompt with no such row under it costs a read of None, so mail.
    """
    answer, top = ANSWER_ROW.get(kind), index
    for row in range(index - 1, -1, -1):
        if answer and answer.match(lines[row]) and (row == 0 or not lines[row - 1].strip()):
            break
        if re.match(COMPOSER[kind][0], lines[row]):
            top = row
    return top


def _lone_composer(lines, kind):
    """True only for a mark in the first column with no mark in the rows it continues: an indented mark is a draft row."""
    prompts = [i for i, line in enumerate(lines) if re.match(COMPOSER[kind][0], line)]
    if not prompts:
        return False
    return not lines[prompts[-1]][0].isspace() and _composer_top(lines, prompts[-1], kind) == prompts[-1]


@_unknown(True)
def viewport_held(screen):
    """A scroll/copy surface is owner UI, never evidence of the live turn/composer."""
    lines = screen.splitlines()
    prompts = [i for i, line in enumerate(lines) if re.match(r'^\s*[›❯]', line)]
    # Controls live below the composer or immediately above it. A quoted control
    # in transcript text is not an active viewport footer.
    region = lines[max(0, prompts[-1] - 3):] if prompts else lines[-3:]
    return any(VIEWPORT_CONTROL.fullmatch(line.strip()) for line in region)


def viewport_class(pane, screen):
    """Require the native host viewport at bottom as well as unheld application UI.

    `pane` is the parsed result of `pane get`: {'pane': {'scroll': {'offset_from_bottom': N}}}.
    """
    try:
        decode_screen(screen)
    except ValueError:
        return 'viewport_unknown'
    if viewport_held(screen):
        return 'viewport_held'
    try:
        offset = pane['pane']['scroll']['offset_from_bottom']
    except (KeyError, TypeError):
        return 'viewport_unknown'
    if type(offset) is not int or offset < 0:
        return 'viewport_unknown'
    return 'viewport_held' if offset else None


@_unknown(True)
def turn_showing(screen):
    """A live turn's row near the bottom, even above the separator `working` stops at. Never idle, never unsent."""
    return any(LIVE_TURN.search(line) for line in [line for line in screen.splitlines() if line.strip()][-BUSY_ROWS:])


@_unknown(None)
def status(screen, kind):
    """'idle', 'working' or 'blocked' only from a recognizable screen, else None."""
    if kind not in IDLE or viewport_held(screen):
        return None
    if strong_dialog(screen):
        return 'blocked'
    if working(screen):
        return 'working'
    if blocking_dialog(screen):
        return 'blocked'
    if turn_showing(screen) or suspected_dialog(screen):
        return None
    return 'idle' if composer_empty(screen, kind) else None


@_unknown(None)
def composer_text(screen, kind):
    """All text held in the live composer, or None when no live composer is visible.

    Only the unbroken run of footers at the bottom is dropped. A footer-shaped row with text below it stays in, so the
    digest cannot match, and so does anything from a mark higher up that the last mark continues.
    """
    if kind not in COMPOSER or viewport_held(screen):
        return None
    mark, footer = COMPOSER[kind]
    lines = screen.splitlines()
    prompts = [i for i, line in enumerate(lines) if re.match(mark, line)]
    if not prompts:
        return None
    first_column = [i for i in prompts if not lines[i][0].isspace()]
    index = first_column[-1] if first_column else prompts[-1]
    rows = lines[_composer_top(lines, index, kind):]
    while len(rows) > 1 and footer(rows[-1]):
        rows.pop()
    parts = [rows[0].strip()[1:].strip()] + [line.strip() for line in rows[1:]]
    return ' '.join(part for part in parts if part)


@_unknown(False)
def composer_empty(screen, kind):
    """True only under the idle read's own composer rule; a dialog or a turn is for the other checks."""
    if kind not in IDLE or viewport_held(screen):
        return False
    lines = screen.splitlines()
    return IDLE[kind](lines) and _lone_composer(lines, kind)


def normalize(text):
    return ' '.join(text.split())


def prompt_fingerprint(text):
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode('utf-8')
    text = normalize(text)
    return hashlib.sha256(text.encode('utf-8')).hexdigest(), text[:60]


def own_prompt(text, digest, sent_at, now):
    """True only when the whole composer text hashes to the digest of what Drover last sent.

    A collapsed paste hides its text, so it is never ours at any age: the skill's 15-minute window is gone, and
    `sent_at` and `now` give no proof of ownership.
    """
    if not digest or not text:
        return False
    try:
        text = decode_screen(text)
    except ValueError:
        return False
    return '[Pasted Content' not in text and prompt_fingerprint(text)[0] == digest


@_unknown(False)
def queued_idle(screen):
    """A follow-up queue visible while no turn runs and no dialog holds it."""
    return not viewport_held(screen) and bool(queued_input_range(screen)) and not working(screen) and not blocking_dialog(screen)


@_unknown(False)
def queued_prompt(screen, prompt):
    """True only when the queue shows this prompt's whole scope address, or the whole prompt when it has none."""
    queued = queued_input_range(screen)
    if not queued or viewport_held(screen):
        return False
    body = '\n'.join(screen.splitlines()[queued[0]:queued[1]])
    # The complete scope address identifies our wake; an opaque paste or another message does not.
    # Matched through the words after it: scope /x must not match scope /x2 or /x.y.
    head, addressed, _ = prompt.partition('. Check owned progress')
    expected = head + addressed if addressed else prompt
    compact = lambda text: re.sub(r'\s+', '', text)
    if not compact(expected) or compact(expected) not in compact(body):
        return False
    marker = ' Worker completion reports ready for your review: '
    return marker not in prompt or compact(prompt.split(marker, 1)[1]) in compact(body)


def classify(agent, screen, item, now):
    """Name why an agent is stuck, or None. 'owner_draft' means typed input: leave it alone."""
    if agent is None:
        return 'exited'
    try:
        screen = decode_screen(screen)
    except ValueError:
        return 'screen_unknown'
    if viewport_held(screen):
        return 'viewport_held'
    kind = agent.get('agent')
    current = status(screen, kind)
    if current == 'working':
        return 'stale_working' if now - item.get('last_activity', now) >= STALE else None
    dialog = bottom_region(screen)
    if model_prompt(screen):
        return 'model_prompt'
    # The passive "less than 5% weekly limit" notice is not a refusal.
    tail = [line for line in screen.splitlines() if line.strip()][-12:]
    recent = dialog + '\n' + '\n'.join(tail)
    active = '\n'.join(l for l in recent.splitlines() if not re.search(r'less than \d+%', l, re.I))
    if re.search(r'^\s*■.*(?:rate limit|too many requests|429)|you.{0,10}hit your .{0,15}rate limit', active, re.I | re.M):
        return 'rate_limit'
    if re.search(r"hit your .{0,20}limit|usage limit|weekly limit (reached|exceeded)|reached your .{0,20}limit", active, re.I):
        return 'usage_limit'
    if re.search(r'trust this folder|trust and continue|do you trust', dialog, re.I):
        return 'trust'
    if re.search(r'allow once|allow always|requires approval|waiting for approval|would you like to run|new hook - review required', dialog, re.I):
        return 'approval'
    if re.search(r'\d+ questions?|shift\+.*answer', dialog, re.I):
        return 'question'
    if current == 'blocked':
        return 'question'  # refuse_dialog refuses this screen, so no composer text may read as sendable
    text = composer_text(screen, kind)
    if text and not composer_empty(screen, kind):
        ours = own_prompt(text, item.get('last_prompt_digest'), item.get('last_prompt_at'), now)
        sendable = ours and _lone_composer(screen.splitlines(), kind) and not suspected_dialog(screen)
        return 'unsent_prompt' if sendable and not turn_showing(screen) else 'owner_draft'
    # Tool output often says 'error:' or 'failed'; only Codex's own turn-error markers count.
    if current == 'idle' and any(re.search(r'stream disconnected|^\s*■', l, re.I) for l in tail):
        return 'error_turn'
    if agent.get('agent_status') == 'working' and now - item.get('last_activity', now) >= STALE:
        return 'stale_working'
    if blocking_dialog(screen):
        return 'question'
    if queued_idle(screen) and now - item.get('queued_since', now) >= WEDGE:
        return 'queued_wedged'
    if item.get('state') == 'paused' and item.get('pause_class'):
        return item['pause_class']
    return None


def _footer_lines(screen):
    """Footer rows under the last Codex composer; a row that is not a known footer gives no reading."""
    lines = screen.splitlines()
    prompts = [i for i, line in enumerate(lines) if re.match(r'^\s*›', line)]
    return [line for line in lines[prompts[-1] + 1:] if is_footer(line)] if prompts else []


@_unknown(None)
def footer_model(screen):
    """(model, effort) from the footer under the last composer, or None when cut off or absent."""
    for line in _footer_lines(screen):
        found = FOOTER_MODEL.search(line)
        if found:
            return re.sub(r'\s+', '-', found.group(1).lower()), found.group(2).lower()
    return None


@_unknown(None)
def context_left(screen):
    """The footer's `N% context left` as an int from 0 to 100, or None when it cannot be read."""
    if viewport_held(screen):
        return None
    for line in _footer_lines(screen):
        found = CONTEXT_LEFT.search(line)
        if found:
            left = int(found.group(1))
            return left if left <= 100 else None
    return None
