"""Private, versioned configuration decisions and best-effort remote sync."""
import datetime
import json
import re
import subprocess
import tomllib
from pathlib import Path

from .lock import FileLock
from .placement import NO_WINDOW

IGNORE = 'ledger.db*\nruns/\nrescue/\nlogs/\nhub-token\n*.log\n.decisions.lock\n'
AUTHORS = ('owner', 'tuner', 'wizard', 'dispatcher')


class DecisionError(RuntimeError):
    pass


def _git(state, *args):
    try:
        result = subprocess.run(['git', '-C', str(state), '-c', 'user.name=Drover',
                                 '-c', 'user.email=drover@example.invalid', *args],
                                capture_output=True, text=True, errors='replace', timeout=30, **NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        raise DecisionError(f'git: {exc}') from exc
    if result.returncode:
        raise DecisionError(result.stderr.strip() or result.stdout.strip() or 'git failed')
    return result.stdout.strip()


def initialize(state_dir):
    """Initialize only this directory, excluding runtime data from its private git repo."""
    state = Path(state_dir)
    state.mkdir(parents=True, exist_ok=True)
    ignore = state / '.gitignore'
    old = ignore.read_text(encoding='utf-8') if ignore.exists() else ''
    missing = [line for line in IGNORE.splitlines() if line not in old.splitlines()]
    if missing:
        ignore.write_text(old + ('\n' if old and not old.endswith('\n') else '') + '\n'.join(missing) + '\n',
                          encoding='utf-8')
    if not (state / '.git').exists():
        _git(state, 'init')
    return state


def _sync_config(state):
    try:
        with open(state / 'config.toml', 'rb') as stream:
            return tomllib.load(stream).get('config_sync', {})
    except FileNotFoundError:
        return {}


def _lock(state):
    """Keep the runtime lock out of versioned configuration."""
    directory = state / '.git' if (state / '.git').is_dir() else state
    return FileLock(directory / '.decisions.lock', blocking=True)


def sync(state_dir, settings=None, reason=None, *, write_record=True):
    """Commit pending state changes; a configured push failure is a warning, never fatal."""
    state = Path(state_dir)
    with _lock(state):
        return _sync(state, settings, reason, write_record=write_record)


def _sync(state, settings=None, reason=None, *, write_record=True):
    if not (state / '.git').exists():
        raise DecisionError('state directory is not a git repo; run drover init to enable decision history')
    if write_record:
        pending = _git(state, 'status', '--porcelain')
        if pending:
            _record(state, 'Configuration', reason or 'Owner requested sync of the pending configuration edits.',
                   {'files': pending.splitlines()}, 'owner', commit=False)
    _git(state, 'add', '--all', '--', '.')
    changed = bool(_git(state, 'diff', '--cached', '--name-only'))
    if changed:
        _git(state, 'commit', '-m', 'Record Drover configuration changes')
    result = {'committed': changed, 'pushed': False}
    cfg = _sync_config(state) if settings is None else settings
    remote = cfg.get('remote')
    if cfg.get('auto_push', True) and remote:
        try:
            # -- makes even a URL beginning with '-' data rather than a git option.
            _git(state, 'push', '--', str(remote), 'HEAD')
            result['pushed'] = True
        except DecisionError as exc:
            result['warning'] = f'push failed; local history is safe: {exc}'
    return result


def _policy_version(state):
    path = state / 'policy.json'
    if path.exists():
        return str(json.loads(path.read_text(encoding='utf-8')).get('version', 'unversioned'))
    return 'unversioned'


def record(state_dir, title, reason, changes, author, *, sync_settings=None, commit=True):
    """Write a decision, preserve the prior reasons it supersedes, commit and optionally push.

    A state directory with git disabled still gets the Markdown record, without a commit.
    Changes may be text or a mapping containing policy_version and supersedes (record filenames).
    """
    if author not in AUTHORS:
        raise ValueError(f'author must be one of {AUTHORS}')
    state = Path(state_dir)
    with _lock(state):
        return _record(state, title, reason, changes, author, sync_settings=sync_settings, commit=commit)


def _record(state, title, reason, changes, author, *, sync_settings=None, commit=True):
    directory = state / 'decisions'
    directory.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r'[^a-z0-9]+', '-', str(title).lower()).strip('-')[:80] or 'decision'
    date = datetime.date.today().isoformat()
    path = directory / f'{date}-{slug}.md'
    number = 2
    while path.exists():
        path = directory / f'{date}-{slug}-{number}.md'
        number += 1
    version = changes.get('policy_version', _policy_version(state)) if isinstance(changes, dict) else _policy_version(state)
    supersedes = changes.get('supersedes') if isinstance(changes, dict) else None
    previous = []
    for earlier in sorted(directory.glob('*.md')):
        text = earlier.read_text(encoding='utf-8')
        if (supersedes is not None and earlier.name in supersedes) or (supersedes is None and f'\n# {title}\n' in text):
            previous.append(f'### [{earlier.name}]({earlier.name})\n\n{text.split("## Why\n", 1)[-1].split("## Earlier reasons", 1)[0].strip()}')
    body = changes if isinstance(changes, str) else json.dumps(changes, indent=2, ensure_ascii=False)
    path.write_text(f'---\ndate: {date}\nauthor: {author}\npolicy_version: {json.dumps(str(version))}\n---\n\n'
                    f'# {title}\n\n## What changed\n\n{body}\n\n## Why\n\n{reason}\n\n'
                    f'## Earlier reasons this supersedes\n\n' + ('\n\n'.join(previous) or 'None; this is the first decision on this subject.') + '\n',
                    encoding='utf-8')
    if commit and (state / '.git').exists():
        result = _sync(state, settings=sync_settings, write_record=False)
        if result.get('warning'):
            import logging
            logging.getLogger(__name__).warning(result['warning'])
    return path


def record_policy(state_dir, version, policy, reason, author='tuner', sync_settings=None):
    """Persist a policy snapshot and its reasons after the ledger transaction commits."""
    state = Path(state_dir)
    if author not in AUTHORS:
        raise ValueError(f'author must be one of {AUTHORS}')
    with _lock(state):
        (state / 'policy.json').write_text(json.dumps({'version': version, 'policy': policy}, indent=2) + '\n',
                                          encoding='utf-8')
        return _record(state, 'Policy', reason, {'policy_version': version, 'policy': policy}, author,
                       sync_settings=sync_settings)
