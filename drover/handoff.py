"""The file a director leaves for the session after it: where it lives, what it must hold, and Drover's own when it wrote none.

The folder is the hub's, under the state folder, and named so any role id stays one path component on Windows, where a
`:` would open a stream. `launch_usage.rotation_handoff` is another thing: that one is for headless workers.
"""
import json
import os
import re
from pathlib import Path
from urllib.parse import quote

from . import config, placement

SECTIONS = ('Goal', 'Done', 'Decisions and rejected options', 'Standing rules', 'Files touched', 'Unverified work',
            'Next step', 'What not to redo')
UNKNOWN = 'unknown'
HEADING = re.compile(r'#{1,6}\s+(.*?)\s*#*')
MAX_TASKS = 50
RESTATED = 'restated.md'
PENDING = 'pane_handoff:'       # a role's setting: the file its next session must take up, until that session restates it
ROTATED = 'rotated'


def folder(cfg, role_id) -> Path:
    return config.state_dir(cfg) / 'directors' / quote(str(role_id), safe='').replace('.', '%2E')


def path_for(cfg, role_id, generation) -> Path:
    return folder(cfg, role_id) / f'handoff-{int(generation)}.md'


def restated_path(path) -> Path:
    return Path(path).with_name(RESTATED)


def pending(port, role_id) -> str | None:
    return port.get_setting(PENDING + role_id) or None


def set_pending(port, role_id, path) -> None:
    port.set_setting(PENDING + role_id, str(path) if path else '')


def owed(port, role_id, clean) -> bool:
    """True while a pending handoff may start its role with no wake: the rotation's own start, and one more after a clean end.
    Two clean ends since the rotation wait for a wake, and a failed end has the replacement budget for its limit. A launch held
    before any agent ran is no attempt. `clean` is the caller's list of outcomes that say a run ended well.
    """
    if pending(port, role_id) is None:
        return False
    ended = [row['outcome'] for row in port.conn.execute(
        "SELECT r.outcome FROM director_runs d JOIN runs r ON r.id=d.run_id WHERE d.role_id=? AND r.outcome IS NOT 'admission-held' "
        'ORDER BY d.run_id DESC LIMIT 2', (role_id,))]
    return any(outcome == ROTATED or outcome not in clean for outcome in ended)


def for_launch(port, role_id, where, mode) -> dict:
    """What a new run's spec needs from a rotation. A resumed session keeps its context, so it takes no handoff."""
    path = pending(port, role_id)
    return dict(handoff=agent_path(path, where)) if path and mode == 'fresh' else {}


def agent_path(path, where) -> str:
    """The path as the agent spells it."""
    return placement.to_wsl_path(path) if where == 'wsl' else str(path)


def request_text(path) -> str:
    """One short line, as every pane write is. It asks for a file and nothing else."""
    return f"Drover: context is nearly full. Write your handoff to {path}, with a ## heading and real content for each of: {', '.join(SECTIONS)}."


def missing(text) -> list[str]:
    """The sections with no heading of their own or nothing under it. A heading of any other name is that section's text."""
    wanted = {section.casefold(): section for section in SECTIONS}
    filled, current = set(), None
    for line in text.splitlines():
        heading = HEADING.fullmatch(line.strip())
        if heading and heading[1].casefold() in wanted:
            current = wanted[heading[1].casefold()]
        elif current and line.strip():
            filled.add(current)
    return [section for section in SECTIONS if section not in filled]


def valid(path) -> bool:
    try:
        return not missing(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False


def follow(cfg, role_id, was, now) -> bool:
    """A session resumed into generation `now` keeps the file it wrote, at the path that generation reads. False if it has none."""
    old = path_for(cfg, role_id, was)
    try:
        if not valid(old):
            return False
        os.replace(old, path_for(cfg, role_id, now))
    except OSError:
        return False
    return True


def clear(path) -> None:
    """An OSError leaves the file in place, so the caller must not go on as if it were gone."""
    Path(path).unlink(missing_ok=True)


def _task_line(task):
    return f"- {task['id']} ({task['status']}): {' '.join(str(task['title']).split())}"


def fallback(role_id, state, ledger_facts) -> str:
    """Drover's own file, from the last checkpoint and the open tasks. Whatever those cannot say is marked `unknown`."""
    checkpoint = state.get('checkpoint') or {}
    done = f"As of checkpoint {state.get('checkpoint_revision')}:\n{json.dumps(checkpoint, indent=1, sort_keys=True)}" if checkpoint else None
    tasks = [_task_line(task) for task in (ledger_facts.get('open_tasks') or [])[:MAX_TASKS]]
    filled = {'Done': done, 'Next step': '\n'.join(tasks) or None}
    body = ''.join(f'## {section}\n{filled.get(section) or UNKNOWN}\n\n' for section in SECTIONS)
    return f'Drover wrote this for {role_id}: the director left no handoff of its own.\n\n{body}'


def write(path, text) -> None:
    """Whole or not at all, so nothing ever reads half a file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + '.tmp')
    partial.write_text(text, encoding='utf-8', newline='\n')
    os.replace(partial, path)
