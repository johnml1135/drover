"""Short ADR files a fresh agent loads because their `applies_to` globs match its task scope.

Only accepted `docs/adr/NNNN-*.md` files qualify: never the glossary, a README or a design doc. Every file is whole or
absent. The `applies_to` list is a flow list, so it parses as JSON and no YAML library is needed. Nothing here raises:
a file that cannot be read or parsed is reported in `skipped` and the brief goes on without it.
"""
import json
import re
import stat
from pathlib import Path, PurePosixPath

ADR_DIR = 'docs/adr'
ADR_NAME = re.compile(r'^(\d{4})-[^/\\]*\.md$')
_GLOB_CHARS = '*?['


def _front_matter(text):
    """The top-level `key: value` lines between the opening and closing `---`; ValueError when there is none."""
    lines = text.lstrip('﻿').splitlines()
    if not lines or lines[0].strip() != '---':
        raise ValueError('no front matter')
    fields = {}
    for line in lines[1:]:
        if line.strip() == '---':
            return fields
        key, sep, value = line.partition(':')
        if sep and key and not key[0].isspace() and key not in fields:
            fields[key.strip()] = value.strip()
    raise ValueError('front matter is not closed')


def applies_to(text):
    """The globs in a file's `applies_to` line; [] when it has none. ValueError when the front matter or list is malformed."""
    raw = _front_matter(text).get('applies_to')
    if raw is None:
        return []
    try:
        globs = json.loads(raw)
    except ValueError as error:
        raise ValueError(f'applies_to is not a JSON list: {error}') from None
    if not isinstance(globs, list) or not all(isinstance(g, str) and g for g in globs):
        raise ValueError('applies_to must be a list of non-empty strings')
    return globs


def _accepted(text):
    value = _front_matter(text).get('status', '').strip('"\' ')
    return value == 'accepted'


def _literal_prefix(glob):
    cut = min((glob.find(c) for c in _GLOB_CHARS if c in glob), default=len(glob))
    return cut


def _paths(scope):
    """Scope entries are 'path' or 'path:a-b'; the line range is not part of the path."""
    out = []
    for entry in scope or []:
        entry = str(entry).replace('\\', '/')
        path = re.sub(r':\d+(-\d+)?$', '', entry).removeprefix('./')
        if path:
            out.append(path)
    return out


def _narrowness(globs, paths):
    """The longest literal prefix among the globs matching any scope path, or None when nothing matches."""
    best = None
    for glob in globs:
        try:
            hit = any(PurePosixPath(p).full_match(glob) for p in paths)
        except (ValueError, TypeError):
            continue
        if hit:
            n = _literal_prefix(glob)
            best = n if best is None else max(best, n)
    return best


def _read_all(root, file_limit=None):
    """-> ({name: text}, [skipped]) for docs/adr/*.md at the checkout's HEAD for a WSL root, else on disk."""
    from . import compaction_hold, placement
    skipped = []
    if compaction_hold.active():
        return {}, [{'path': ADR_DIR, 'reason': 'compaction hold is active'}]
    texts = {}
    try:
        if placement.routes_through_wsl(root):
            listing = placement.git(root, 'ls-tree', '--name-only', 'HEAD', ADR_DIR + '/', timeout=15)
            if listing.returncode:
                return {}, []
            for rel in sorted(listing.stdout.splitlines()):
                rel = rel.strip()
                if not ADR_NAME.match(PurePosixPath(rel).name):
                    continue
                shown = placement.git(root, 'show', f'HEAD:{rel}', timeout=15)
                if shown.returncode:
                    skipped.append({'path': rel, 'reason': 'unreadable: git show failed'})
                else:
                    texts[rel] = shown.stdout
        else:
            folder = Path(placement.native(root)) / 'docs' / 'adr'
            if not folder.is_dir():
                return {}, []
            for entry in sorted(folder.iterdir()):
                if not ADR_NAME.match(entry.name):
                    continue
                rel = f'{ADR_DIR}/{entry.name}'
                try:
                    info = entry.lstat()   # never follow a link, never open a FIFO or device
                    if not stat.S_ISREG(info.st_mode):
                        skipped.append({'path': rel, 'reason': 'not a regular file'})
                    elif file_limit is not None and info.st_size > file_limit:
                        skipped.append({'path': rel, 'reason': f'over the {file_limit}-byte file limit'})
                    else:
                        texts[rel] = entry.read_bytes().decode('utf-8')
                except (OSError, UnicodeDecodeError) as error:
                    skipped.append({'path': rel, 'reason': f'unreadable: {type(error).__name__}'})
    except Exception as error:   # a read problem never stops the brief
        skipped.append({'path': ADR_DIR, 'reason': f'unreadable: {type(error).__name__}'})
    return texts, skipped


def select(root, scope, *, file_limit, total_limit):
    """-> {'files': [{'path','text'}], 'skipped': [{'path','reason'}]}, narrowest matching glob first, then newest ADR."""
    result = {'files': [], 'skipped': []}
    paths = _paths(scope)
    if not root or not paths:
        return result
    try:
        texts, skipped = _read_all(root, file_limit)
    except Exception as error:
        return {'files': [], 'skipped': [{'path': ADR_DIR, 'reason': f'unreadable: {type(error).__name__}'}]}
    result['skipped'].extend(skipped)
    ranked = []
    for rel, text in texts.items():
        try:
            if not _accepted(text):
                continue
            narrow = _narrowness(applies_to(text), paths)
        except ValueError as error:
            result['skipped'].append({'path': rel, 'reason': f'malformed front matter: {error}'})
            continue
        if narrow is None:
            continue
        if len(text.encode('utf-8')) > file_limit:
            result['skipped'].append({'path': rel, 'reason': f'over the {file_limit}-byte file limit'})
            continue
        ranked.append((-narrow, -int(ADR_NAME.match(PurePosixPath(rel).name).group(1)), rel, text))
    used, full = 0, False
    for _, _, rel, text in sorted(ranked):
        size = len(text.encode('utf-8'))
        if full or used + size > total_limit:
            full = True
            result['skipped'].append({'path': rel, 'reason': f'over the {total_limit}-byte total limit'})
            continue
        used += size
        result['files'].append({'path': rel, 'text': text})
    return result
