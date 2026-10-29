"""Shared CI documentation, receipt, and timestamp utilities."""
import datetime
import json
import re
from pathlib import Path


VERSION = 1


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


def _norm(path):
    return str(path).replace('\\', '/')


def _glob_re(pattern):
    out, i, p = '', 0, _norm(pattern)
    while i < len(p):
        if p.startswith('**/', i):
            out += '(?:.*/)?'
            i += 3
        elif p.startswith('**', i):
            out += '.*'
            i += 2
        elif p[i] == '*':
            out += '[^/]*'
            i += 1
        elif p[i] == '?':
            out += '[^/]'
            i += 1
        else:
            out += re.escape(p[i])
            i += 1
    return re.compile(out + r'\Z')


DOCS_EXTENSIONS = ('.md', '.rst', '.txt', '.adoc')


DOCS_EXACT = ('LICENSE', 'GLOSSARY.md')


def _glob_hit(path, pattern):
    rx = _glob_re(pattern)
    return bool(rx.match(path) or ('/' not in _norm(pattern) and rx.match(path.rsplit('/', 1)[-1])))


def docs_only(paths, globs):
    """True when there is at least one path and every path is docs: it matches a positive glob, no `!` glob, and
    has a docs extension (.md .rst .txt .adoc) or is exactly LICENSE or GLOSSARY.md. `**` crosses directories, `*`
    does not; a glob without a slash matches the file name at any depth (`*.md` covers docs/a/b.md)."""
    paths = [_norm(p) for p in paths]
    positive = [g for g in globs or () if not g.startswith('!')]
    negative = [g[1:] for g in globs or () if g.startswith('!')]
    if not paths or not positive:
        return False
    return all((p in DOCS_EXACT or p.lower().endswith(DOCS_EXTENSIONS))
               and any(_glob_hit(p, g) for g in positive) and not any(_glob_hit(p, g) for g in negative)
               for p in paths)


def status_problems(receipt):
    """Version and outcome problems that need no repository."""
    found = []
    if receipt.get('version') != VERSION:
        found.append(f'unknown receipt version {receipt.get("version")}')
    if not receipt.get('passed'):
        found.append(f'gate did not pass (exit {receipt.get("exit_code")}, tree unchanged {receipt.get("tree_unchanged")})')
    return found


def read_result(path):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _tail(path, lines=40):
    # Read backwards so large CI logs do not grow the hub's memory use.
    try:
        with open(path, 'rb') as stream:
            stream.seek(0, 2)
            end, chunks, count = stream.tell(), [], 0
            while end and count <= lines:
                start = max(0, end - 8192)
                stream.seek(start)
                chunk = stream.read(end - start)
                chunks.append(chunk)
                count += chunk.count(b'\n')
                end = start
        return b''.join(reversed(chunks)).decode('utf-8', errors='replace').splitlines()[-lines:]
    except OSError:
        return []
