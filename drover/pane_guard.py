"""Blocking dialogs on a pane screen. Pure text rules: this module never talks to Herdr.

Herdr's output may arrive as bytes; every public function reads it as UTF-8.
"""
import re

MODEL_PROMPT = r'enter\s+select\s*[·•]\s*esc\s+back|Switch\s+to\s+gpt-'
STRONG_DIALOG = (MODEL_PROMPT + r'|enter\s+select|esc\s+back|allow once|allow always|trust this folder|'
          r'trust and continue|do you trust|new hook - review required|requires approval|waiting for approval|'
          r'would you like to run|'
          r'(?m:^\s*(?:[›❯]\s*)?\d+[.)]\s+(?:Switch to gpt-\S+|Keep current model.*|Allow (?:once|always).*|Deny(?:[,.].*)?|Yes(?:[,.].*)?|No(?:[,.].*)?|Trust (?:this|and).*|Continue(?:[,.].*)?)\s*$)')
DIALOG = STRONG_DIALOG + r'|\d+ questions?|shift\+.*answer|press enter to|update available'
SELECTED_OPTION = re.compile(r'^\s*[›❯]\s*\d+[.)]\s+')


def decode_screen(screen):
    """The screen as text. Anything that is not a screen is refused, so a failed read never reads as clear."""
    if isinstance(screen, (bytes, bytearray)):
        return bytes(screen).decode('utf-8', errors='replace')
    if isinstance(screen, str):
        return screen
    raise ValueError('pane screen unavailable; preserve the pane')


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
    footers = [i for i, line in enumerate(lines) if re.match(r'^\s*shift\+.*edit last queued message', line, re.I)]
    end = footers[-1] if footers else len(lines)
    if headings:
        return headings[-1], end
    if footers:
        entries = [i for i, line in enumerate(lines[:end]) if re.match(r'^\s*↳\s', line)]
        if entries:
            return entries[0], end
    return None


def working(screen):
    screen = decode_screen(screen)
    queued = queued_input_range(screen)
    surface = '\n'.join(screen.splitlines()[:queued[0]]) if queued else screen
    region = bottom_region(surface)
    return bool(re.search(r'esc to interrupt', region, re.I) or
                any(re.match(r'^\s*•\s*Working\b', line) for line in
                    [line for line in region.splitlines() if line.strip()][-8:]))


def model_prompt(screen):
    return bool(re.search(MODEL_PROMPT, bottom_region(screen), re.I))


def strong_dialog(screen):
    return bool(re.search(STRONG_DIALOG, bottom_region(screen), re.I))


def blocking_dialog(screen):
    return strong_dialog(screen) or (not working(screen) and
                                    bool(re.search(DIALOG, bottom_region(screen), re.I)))


def startup_keys(screen, fresh_pane=False, kind=None):
    """The one key that answers Codex folder trust in Drover's own new pane, or None.

    `kind` must name Codex: Claude's folder trust and every update dialog stay with the owner.
    """
    region = bottom_region(screen)
    if kind != 'codex' or not fresh_pane or working(screen) or model_prompt(screen):
        return None
    # Never let a concurrent approval/question modal inherit startup consent.
    if re.search(r'allow once|allow always|requires approval|waiting for approval|'
                 r'would you like to run|\d+ questions?|shift\+.*answer', region, re.I):
        return None
    # Nor a hook review or an update: Enter would answer a dialog the standing rule does not cover.
    if re.search(r'new hook - review required|update available|skip until next version', region, re.I):
        return None
    if re.search(r'trust this folder|trust and continue|do you trust', region, re.I):
        return ('enter',)
    return None


def refuse_dialog(screen, target=None):
    if model_prompt(screen):
        raise ValueError((str(target) + ': ' if target else '') + 'owner-only model_prompt; preserve the dialog and choose no option')
    if blocking_dialog(screen):
        raise ValueError((str(target) + ': ' if target else '') + 'blocking dialog; preserve owner input and choose no option')
