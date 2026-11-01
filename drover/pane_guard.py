"""Blocking dialogs on a pane screen. Pure text rules: this module never talks to Herdr.

Herdr's output may arrive as bytes; every public function reads it as strict UTF-8. A capture that cannot be read
exactly raises ValueError here, because a bool from these rules would say "no dialog" about a screen nobody read.
"""
import re
import unicodedata

MODEL_PROMPT = r'enter\s+select\s*[·•]\s*esc\s+back|Switch\s+to\s+gpt-'
STRONG_DIALOG = (MODEL_PROMPT + r'|enter\s+select|esc\s+back|allow once|allow always|trust this folder|'
          r'trust and continue|do you trust|new hook - review required|requires approval|waiting for approval|'
          r'would you like to run|'
          r'(?m:^\s*(?:[›❯]\s*)?\d+[.)]\s+(?:Switch to gpt-\S+|Keep current model.*|Allow (?:once|always).*|Deny(?:[,.].*)?|Yes(?:[,.].*)?|No(?:[,.].*)?|Trust (?:this|and).*|Continue(?:[,.].*)?)\s*$)')
DIALOG = STRONG_DIALOG + r'|\d+ questions?|shift\+.*answer|press enter to|update available'
SELECTED_OPTION = re.compile(r'^\s*[›❯]\s*\d+[.)]\s+')
# The only screen the standing trust rule answers, row for row. Task 15 replaces it with a real capture.
CODEX_TRUST = ('Trust this folder?', '› 1. Trust and continue', '2. Quit')
ABOVE_COMPOSER = 12  # rows searched above the composer; bottom_region's own lookback stops at 8
OPTION_ROW = re.compile(r'^(?:[›❯]\s*)?\d+[.)]\s')
YES_NO = re.compile(r'[\[(]\s*y(?:es)?\s*/\s*no?\s*[\])]', re.I)
QUEUE_HINT = re.compile(r'^\s*shift\+', re.I)   # any such row, as a narrow pane cuts or wraps the queue's own hint
UNREADABLE = ('Cc', 'Cf', 'Cs', 'Zl', 'Zp')  # controls, invisible format marks and line breaks can hide a row's start


def decode_screen(screen):
    """The screen as text, or ValueError. Anything not read exactly is refused, so a bad read never reads as clear."""
    if isinstance(screen, (bytes, bytearray)):
        screen = bytes(screen).decode('utf-8')
    if not isinstance(screen, str):
        raise ValueError('pane screen unavailable; preserve the pane')
    screen = screen.replace('\r\n', '\n')
    if any(c == '\ufffd' or (c not in '\n\t' and unicodedata.category(c) in UNREADABLE) for c in screen):
        raise ValueError('pane screen unreadable; preserve the pane')
    return screen


def bottom_region(screen):
    """Exclude transcript above the active composer/dialog separator.

    Footer separators after a composer do not hide the composer or its dialog.
    A selected numbered option is a modal row, not a composer boundary.
    Without a separator, use the composer and any adjacent modal/status rows.
    Startup modals without a composer retain the visible screen.
    """
    lines = decode_screen(screen).splitlines()
    options = [i for i, line in enumerate(lines) if SELECTED_OPTION.match(line)]
    composers = [i for i, line in enumerate(lines)
                 if re.match(r'^\s*[›❯]', line) and not SELECTED_OPTION.match(line)]
    modal = bool(options) and (not composers or options[-1] > composers[-1])
    end = options[-1] if modal else (composers[-1] if composers else len(lines))
    separators = [i for i, line in enumerate(lines[:end])
                  if re.fullmatch(r'\s*[─━-]{3,}\s*', line)]
    if separators:
        start = separators[-1] + 1
    elif composers and not modal:
        start = end
        for i in range(max(0, end - 8), end):
            if re.match(DIALOG, lines[i].strip(), re.I) or re.search(
                    r'^\s*•\s*Working\b|^\s*Working\b.*esc to interrupt', lines[i], re.I):
                start = i
                break
    else:
        start = 0
    return '\n'.join(lines[start:])


def queued_input_range(screen):
    lines = decode_screen(screen).splitlines()
    headings = [i for i, line in enumerate(lines) if re.match(r'^\s*Queued follow-up inputs\b', line, re.I)]
    footers = [i for i, line in enumerate(lines) if QUEUE_HINT.match(line)]
    if headings:
        below = [i for i in footers if i > headings[-1]]
        return headings[-1], below[-1] if below else len(lines)
    if footers:
        entries = [i for i, line in enumerate(lines[:footers[-1]]) if re.match(r'^\s*↳\s', line)]
        if entries:
            return entries[0], footers[-1]
    return None


def _shows_working(surface):
    region = bottom_region(surface)
    return bool(re.search(r'esc to interrupt', region, re.I) or
                any(re.match(r'^\s*•\s*Working\b', line) for line in
                    [line for line in region.splitlines() if line.strip()][-8:]))


def working(screen):
    """The rows above a queue, or the whole screen: a row that only looks like a queue must not hide a live turn."""
    screen = decode_screen(screen)
    queued = queued_input_range(screen)
    return _shows_working(screen) or bool(queued) and _shows_working('\n'.join(screen.splitlines()[:queued[0]]))


def model_prompt(screen):
    return bool(re.search(MODEL_PROMPT, bottom_region(screen), re.I))


def strong_dialog(screen):
    return bool(re.search(STRONG_DIALOG, bottom_region(screen), re.I))


def _above_composer(screen):
    lines = decode_screen(screen).splitlines()
    composers = [i for i, line in enumerate(lines)
                 if re.match(r'^\s*[›❯]', line) and not SELECTED_OPTION.match(line)]
    return lines[max(0, composers[-1] - ABOVE_COMPOSER):composers[-1]] if composers else []


def _separator(line):
    return bool(re.fullmatch(r'\s*[─━-]{3,}\s*', line))


def adjacent_dialog(screen):
    """A dialog word anywhere in the rows over the composer, joined so a wrapped or look-alike word still matches.

    A real `─` row still ends the search: scrollback above it stays scrollback (rule 12). A `---` row does not, because
    any agent can print one.
    """
    rows = _above_composer(screen)
    cuts = [i for i, row in enumerate(rows) if re.fullmatch(r'\s*[─━]{3,}\s*', row)]
    text = unicodedata.normalize('NFKC', ' '.join(rows[cuts[-1] + 1:] if cuts else rows))
    return bool(re.search(DIALOG, ' '.join(text.split()), re.I))


def suspected_dialog(screen):
    """No dialog word, but the rows over the composer are shaped like a choice: not a dialog, and not idle either.

    An option row counts anywhere in the unbroken block over the composer, so a hint under the options cannot hide them.
    """
    block = []
    for row in reversed(_above_composer(screen)):
        if row.strip() and not _separator(row):
            block.append(unicodedata.normalize('NFKC', row).strip())
        elif block:
            break
    if not block:
        return False
    return bool(any(OPTION_ROW.match(row) for row in block) or block[0].endswith('?') or YES_NO.search(block[0]))


def blocking_dialog(screen):
    if strong_dialog(screen):
        return True
    return not working(screen) and (bool(re.search(DIALOG, bottom_region(screen), re.I)) or adjacent_dialog(screen))


def startup_keys(screen, fresh_pane=False, kind=None):
    """The one key that answers exactly Codex's folder trust dialog in Drover's own new pane, or None.

    A positive match on the whole screen: every other dialog, wording or selected row stays with the owner. Whether
    the repo is trusted is the caller's check. An unreadable screen gets no key.
    """
    try:
        rows = tuple(line.strip() for line in decode_screen(screen).splitlines() if line.strip())
    except ValueError:
        return None
    return ('enter',) if kind == 'codex' and fresh_pane and rows == CODEX_TRUST else None


def refuse_dialog(screen, target=None):
    if model_prompt(screen):
        raise ValueError((str(target) + ': ' if target else '') + 'owner-only model_prompt; preserve the dialog and choose no option')
    if blocking_dialog(screen):
        raise ValueError((str(target) + ': ' if target else '') + 'blocking dialog; preserve owner input and choose no option')
