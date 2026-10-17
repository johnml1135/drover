"""Task kinds and declarative repository read access, shared by all input paths."""
import re

from . import repos

KINDS = ('research', 'investigation', 'implementation', 'fix', 'review', 'docs', 'mechanical')
READ_ONLY_KINDS = ('research', 'investigation', 'review')


def check_kind(kind):
    if kind not in KINDS:
        raise ValueError(f'kind must be one of {", ".join(KINDS)}')
    return kind


def split_read(entry):
    if not isinstance(entry, str) or not entry or entry.count('@') > 1:
        raise ValueError('read_access entries must be repo or repo@branch')
    name, sep, branch = entry.partition('@')
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', name):
        raise ValueError(f'invalid read repo {name!r}')
    if sep and (not branch or branch.startswith(('-', '/')) or branch.endswith(('.', '/'))
                or '..' in branch or '//' in branch or '@{' in branch
                or re.search(r'[\s\x00-\x1f\x7f~^:?*\[\\]', branch)
                or any(p.startswith('.') or p.endswith('.lock') for p in branch.split('/'))):
        raise ValueError(f'invalid read branch {branch!r}')
    return name, branch or None


def read_access(entries, repo, configured=None):
    configured = repos.load_all() if configured is None else configured
    if entries is None:
        default = configured.get(str(repo).lower(), {}).get('default_branch', 'main')
        return [f'{repo}@{default}']
    if not isinstance(entries, (list, tuple)):
        raise ValueError('read_access must be a list')
    out = []
    for entry in entries:
        name, _ = split_read(entry)
        if name.lower() not in configured:
            raise ValueError(f'unknown read repo {name!r}')
        if entry not in out:
            out.append(entry)
    return out
