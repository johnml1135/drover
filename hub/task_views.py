"""Task presentation: safe Markdown and nonblocking cached repository state."""
import html
import ntpath
from pathlib import Path
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from markupsafe import Markup
from drover import repos


def first_paragraph(text):
    return re.split(r'\n\s*\n', (text or '').strip(), maxsplit=1)[0]


def _inline(text):
    # Escape all user HTML. Only this renderer's fixed tags can reach Markup.
    tokens = []

    def save(value):
        tokens.append(value)
        return f'\x00{len(tokens) - 1}\x00'

    text = html.escape(text).replace('\x00', '')
    text = re.sub(r'`([^`]+)`', lambda m: save('<code>' + m[1] + '</code>'), text)

    def link(m):
        label, url = m[1], html.unescape(m[2]).strip()
        try:
            parsed = urlsplit(url)
        except ValueError:
            return label
        if (parsed.scheme.lower() not in ('https', 'http', 'mailto', '')
                or url.startswith('//') or '\\' in url or re.search(r'[\s\x00-\x1f]', url)):
            return label
        return save(f'<a href="{html.escape(url, quote=True)}" rel="nofollow noopener">{label}</a>')

    text = re.sub(r'\[([^\[\]]+)\]\(([^\s()]*)\)', link, text)
    text = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'(?<!\*)\*([^*]+)\*(?!\*)', r'<em>\1</em>', text)
    # Tokens cannot be nested except code in a link label; expand until done.
    for _ in range(2):
        text = re.sub(r'\x00(\d+)\x00', lambda m: tokens[int(m[1])], text)
    return text


def markdown(text):
    """Conservative Markdown subset: paragraphs, headings, lists, links and code.

    Raw HTML is always text, including inside fenced code. No user-provided
    tag or attribute is ever trusted.
    """
    out, paragraph, listing, code = [], [], None, None

    def flush():
        nonlocal listing
        if paragraph:
            out.append('<p>' + _inline('\n'.join(paragraph)) + '</p>')
            paragraph.clear()
        if listing:
            out.append(f'</{listing}>')
            listing = None

    for line in (text or '').splitlines():
        if line.startswith('```'):
            if code is not None:
                out.append('<pre><code>' + html.escape('\n'.join(code)) + '</code></pre>')
                code = None
            else:
                flush()
                code = []
            continue
        if code is not None:
            code.append(line)
            continue
        if not line.strip():
            flush()
            continue
        heading = re.match(r'^(#{1,6})\s+(.*)', line)
        bullet = re.match(r'^\s*(?:[-*+]\s+|\d+\.\s+)(.*)', line)
        if heading:
            flush()
            n = len(heading[1])
            out.append(f'<h{n}>' + _inline(heading[2]) + f'</h{n}>')
        elif bullet:
            tag = 'ol' if re.match(r'^\s*\d', line) else 'ul'
            if listing != tag:
                flush()
                out.append(f'<{tag}>')
                listing = tag
            out.append('<li>' + _inline(bullet[1]) + '</li>')
        else:
            if listing:
                flush()
            paragraph.append(line)
    flush()
    if code is not None:
        out.append('<pre><code>' + html.escape('\n'.join(code)) + '</code></pre>')
    return Markup('\n'.join(out))


def _join(base, name):
    return ntpath.join(base, name) if repos.WIN_RE.match(base) else str(Path(base) / name)


def probe(prog):
    """Two bounded git calls per repo, only on the cache's background thread."""
    def git(*args):
        r = subprocess.run(['git', '-C', repos.native(prog['repo']), *args],
                           capture_output=True, text=True, timeout=1)
        if r.returncode:
            raise OSError(r.stderr[-200:])
        return r.stdout
    branches = set(git('for-each-ref', '--format=%(refname:short)', 'refs/heads', 'refs/remotes').splitlines())
    paths, branch_paths = set(), {}
    for record in git('worktree', 'list', '--porcelain').split('\n\n'):
        lines = dict(line.split(' ', 1) for line in record.splitlines() if ' ' in line)
        path = lines.get('worktree')
        if path and Path(repos.native(path)).is_dir():
            paths.add(str(Path(repos.native(path)).resolve()))
            if lines.get('branch', '').startswith('refs/heads/'):
                branch_paths[lines['branch'][11:]] = path
    return dict(branches=branches, paths=paths, branch_paths=branch_paths)


class RepoStates:
    def __init__(self, ttl=5, loader=probe):
        self.ttl, self.loader = ttl, loader
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='task-state')
        self.cache, self.pending = {}, {}
        self.lock = threading.Lock()

    def get(self, prog):
        key, now = (prog['repo'], prog['worktrees']), time.monotonic()
        with self.lock:
            future = self.pending.get(key)
            if future and future.done():
                try:
                    result = future.result()
                except Exception:
                    result = dict(branches=set(), paths=set(), branch_paths={}, error=True)
                self.cache[key] = (now, result)
                del self.pending[key]
            cached = self.cache.get(key)
            if (cached is None or now - cached[0] >= self.ttl) and key not in self.pending:
                self.pending[key] = self.pool.submit(self.loader, dict(prog))
            return cached[1] if cached else None

    def close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)


def derive(task, prog, disk=None):
    branch = task.get('branch') or f'drover/{task["id"]}'
    planned = _join(prog['worktrees'], f'drover-{task["id"]}') if prog else 'unknown repo'
    wt = task.get('worktree') or (disk or {}).get('branch_paths', {}).get(branch) or planned
    checked = disk is not None and not disk.get('error')
    branch_exists = checked and (branch in disk['branches'] or
                                f'{prog.get("remote", "origin")}/{branch}' in disk['branches'])
    # A name persisted in the ledger but absent from disk is missing. A generated
    # name is planned until it is observed. Pending/error is displayed explicitly.
    bstate = 'exists' if branch_exists else 'missing' if task.get('branch') else 'planned'
    wt_exists = checked and wt != 'unknown repo' and str(Path(repos.native(wt)).resolve()) in disk['paths']
    wstate = 'exists' if wt_exists else 'missing' if task.get('worktree') else 'planned'
    return dict(repo_config_path=prog['repo'] if prog else '', unknown_repo=prog is None,
                branch_name=branch, branch_state=bstate, worktree_name=wt, worktree_state=wstate,
                state_pending=prog is not None and not checked,
                first_paragraph=first_paragraph(task.get('description') or task.get('goal')))
