"""What a pane screen says about its agent. Pure text rules: this module never talks to Herdr.

`kind` is 'claude' or 'codex'. An unrecognised kind or layout reads as None, and None never permits input.
"""
import hashlib
import re

from .pane_guard import blocking_dialog, bottom_region, decode_screen, model_prompt, queued_input_range, strong_dialog, working

STALE = 900
WEDGE = 600  # Codex drains its follow-up queue when a turn ends; ten idle minutes with a queue is a wedge

WARNING_FOOTER = r'⚠\s*(?:\d+(?:\s+warnings?)?\s*·\s*f\d+(?:\s+to view)?|weekly limit: \d+% left\s*·\s*(?:resets at \d{2}:\d{2}|/status))'
# Current Codex uses friendly model labels; accept only these observed names with an effort.
FRIENDLY_MODEL = r'gpt-6\.1\s+(?:terra|sol)'
FRIENDLY_LOCATION = r'(?:~?/|[A-Z]:[\\/]|\\\\)[^\s·]*'
FOOTER = re.compile(
    r'(?:(?:← for agents\s*·\s*)?\? for shortcuts(?:\s+' + WARNING_FOOTER + r')?|\? fo(?:…|\.\.\.)|\?\s+⚠\s*\d+(?:\s*·\s*f\d+)?|'
    r'New activity\s*·\s*enter/esc latest(?:\s*·\s*\? (?:for )?shortcuts)?(?:\s+' + WARNING_FOOTER + r')?|'
    r'\d+% context left|' + FRIENDLY_MODEL + r'\s+(?:low|medium|high|xhigh)\s*·\s*' +
    FRIENDLY_LOCATION + r'(?:\s*·\s*[^·]+)?|'  # optional thread title / "Read briefs"
    r'gpt-[\w.\-]+…?(?:\s+(?:low|medium|high|xhigh|[a-z]{1,5}…))?(?:\s*·.*)?|'
    + WARNING_FOOTER + r')', re.I)
SEPARATOR = re.compile(r'[─━\- ]+')
EMPTY_COMPOSER = ('', 'Ask Codex to do anything')
PLACEHOLDER_MIN = 7  # a narrow pane cuts the placeholder to "Ask Cod"; shorter text may be an owner's draft
VIEWPORT_CONTROL = re.compile(
    r'^(?:New activity\s*·\s*)?(?:\^c copy\s*·\s*enter copy & follow\s*·\s*esc clear|'
    r'enter/esc latest\b.*|(?:↓\s*)?Back to bottom\s*·\s*esc)$', re.I)
FOOTER_MODEL = re.compile(r'\b(' + FRIENDLY_MODEL + r'|gpt-[\w.\-]*\w)\s+(low|medium|high|xhigh)\b', re.I)
CONTEXT_LEFT = re.compile(r'(?<![\d.])(\d{1,3})% context left', re.I)
STUCK_CLASSES = ('rate_limit', 'model_prompt', 'approval', 'question', 'trust', 'usage_limit', 'unsent_prompt', 'stale_working',
                 'error_turn', 'exited', 'wake_unverified', 'scope_stale', 'queued_wedged', 'viewport_held', 'viewport_unknown')


def empty_composer(text):
    if text in EMPTY_COMPOSER:
        return True
    text = text.rstrip('…').rstrip()
    return len(text) >= PLACEHOLDER_MIN and EMPTY_COMPOSER[1].startswith(text)


def is_footer(line):
    # Recognize full known footer forms, including truncation, without accepting arbitrary draft prefixes.
    return not line.strip() or bool(SEPARATOR.fullmatch(line)) or bool(FOOTER.fullmatch(line.strip()))


def _claude_footer(line):
    return bool(not line.strip() or re.fullmatch(r'[─━\- ]+', line) or re.match(r'^\s*⏵⏵ auto mode on', line))


def _codex_idle(lines):
    prompts = [i for i, line in enumerate(lines) if re.match(r'^\s*›', line)]
    if not prompts:
        return False
    index = prompts[-1]
    # A multiline owner draft can have an empty first line. Only the known footer,
    # blank rows and separators may follow the prompt; unknown text retains input.
    return empty_composer(lines[index].strip()[1:].strip()) and all(is_footer(line) for line in lines[index + 1:])


def _claude_idle(lines):
    prompts = [i for i, line in enumerate(lines) if re.match(r'^\s*❯', line)]
    if not prompts:
        return False
    index = prompts[-1]
    return lines[index].strip() == '❯' and all(_claude_footer(line) for line in lines[index + 1:])


IDLE = dict(claude=_claude_idle, codex=_codex_idle)
COMPOSER = dict(claude=(r'^\s*❯', _claude_footer), codex=(r'^\s*›', is_footer))


def viewport_held(screen):
    """A scroll/copy surface is owner UI, never evidence of the live turn/composer."""
    lines = decode_screen(screen).splitlines()
    prompts = [i for i, line in enumerate(lines) if re.match(r'^\s*›', line)]
    # Controls live below the composer or immediately above it. A quoted control
    # in transcript text is not an active viewport footer.
    region = lines[max(0, prompts[-1] - 2):] if prompts else lines[-3:]
    return any(VIEWPORT_CONTROL.fullmatch(line.strip()) for line in region)


def viewport_class(pane, screen):
    """Require the native host viewport at bottom as well as unheld application UI.

    `pane` is the parsed result of `pane get`: {'pane': {'scroll': {'offset_from_bottom': N}}}.
    """
    if viewport_held(screen):
        return 'viewport_held'
    try:
        offset = pane['pane']['scroll']['offset_from_bottom']
    except (KeyError, TypeError):
        return 'viewport_unknown'
    if type(offset) is not int or offset < 0:
        return 'viewport_unknown'
    return 'viewport_held' if offset else None


def status(screen, kind):
    """'idle', 'working' or 'blocked' only from a recognizable screen, else None."""
    screen = decode_screen(screen)
    if kind not in IDLE or viewport_held(screen):
        return None
    if strong_dialog(screen):
        return 'blocked'
    if working(screen):
        return 'working'
    if blocking_dialog(screen):
        return 'blocked'
    return 'idle' if IDLE[kind](screen.splitlines()) else None


def composer_text(screen, kind):
    """All text held in the live composer, or None when no live composer is visible.

    Blank rows and footers are skipped, not a stop: text under a blank row is still the owner's.
    """
    screen = decode_screen(screen)
    if kind not in COMPOSER or viewport_held(screen):
        return None
    mark, footer = COMPOSER[kind]
    lines = screen.splitlines()
    prompts = [i for i, line in enumerate(lines) if re.match(mark, line)]
    if not prompts:
        return None
    parts = [lines[prompts[-1]].strip()[1:].strip()]
    parts += [line.strip() for line in lines[prompts[-1] + 1:] if not footer(line)]
    return ' '.join(part for part in parts if part)


def composer_empty(screen, kind):
    """True only under the idle read's own composer rule; a dialog or a turn is for the other checks."""
    screen = decode_screen(screen)
    return kind in IDLE and not viewport_held(screen) and IDLE[kind](screen.splitlines())


def normalize(text):
    return ' '.join(text.split())


def prompt_fingerprint(text):
    text = normalize(decode_screen(text))
    return hashlib.sha256(text.encode('utf-8')).hexdigest(), text[:60]


def own_prompt(text, digest, sent_at, now):
    """True only when the whole composer text hashes to the digest of what Drover last sent.

    A collapsed paste hides its text, so it is never ours at any age: the skill's 15-minute window is gone, and
    `sent_at` and `now` give no proof of ownership.
    """
    if not digest or not text:
        return False
    text = decode_screen(text)
    return '[Pasted Content' not in text and prompt_fingerprint(text)[0] == digest


def queued_idle(screen):
    """A follow-up queue visible while no turn runs and no dialog holds it."""
    screen = decode_screen(screen)
    return not viewport_held(screen) and bool(queued_input_range(screen)) and not working(screen) and not blocking_dialog(screen)


def queued_prompt(screen, prompt):
    """True only when the queue shows this prompt's whole scope address, or the whole prompt when it has none."""
    screen = decode_screen(screen)
    queued = queued_input_range(screen)
    if not queued or viewport_held(screen):
        return False
    body = '\n'.join(screen.splitlines()[queued[0]:queued[1]])
    # The complete scope address identifies our wake; an opaque paste or another message does not.
    # The closing stop is part of it: scope /x must not match scope /x2.
    head, addressed, _ = prompt.partition('. Check owned progress')
    expected = head + '.' if addressed else prompt
    compact = lambda text: re.sub(r'\s+', '', text)
    if not compact(expected) or compact(expected) not in compact(body):
        return False
    marker = ' Worker completion reports ready for your review: '
    return marker not in prompt or compact(prompt.split(marker, 1)[1]) in compact(body)


def classify(agent, screen, item, now):
    """Name why an agent is stuck, or None. 'owner_draft' means typed input: leave it alone."""
    if agent is None:
        return 'exited'
    screen = decode_screen(screen)
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
    text = composer_text(screen, kind)
    if text is not None and not (text == '' or kind == 'codex' and empty_composer(text)):
        return 'unsent_prompt' if own_prompt(text, item.get('last_prompt_digest'), item.get('last_prompt_at'), now) else 'owner_draft'
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
    lines = decode_screen(screen).splitlines()
    prompts = [i for i, line in enumerate(lines) if re.match(r'^\s*›', line)]
    return [line for line in lines[prompts[-1] + 1:] if is_footer(line)] if prompts else []


def footer_model(screen):
    """(model, effort) from the footer under the last composer, or None when cut off or absent."""
    for line in _footer_lines(screen):
        found = FOOTER_MODEL.search(line)
        if found:
            return re.sub(r'\s+', '-', found.group(1).lower()), found.group(2).lower()
    return None


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
