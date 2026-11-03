"""Bounded cleanup measurements and reports; cached inventory never authorizes deletion."""
import hashlib
import heapq
import json
import os
import stat
from pathlib import Path

from . import disk_state, git_client, placement

LIMIT = 20
CACHE_LIMIT_BYTES = 4 * 1024 * 1024


class Inventory:
    """One tree traversal per root across report stages, with bounded directory summaries."""

    def __init__(self, now, side=None, drive_path=None, state_dir=None, inventory_id=None, *, ignore_activity=()):
        """With `inventory_id`, the run's cache in `state_dir`, keyed by `side` and `drive_path`, is read and saved."""
        self.now = now
        self.ignore_activity = {placement.cleanup_parts(path) for path in ignore_activity}
        self.directories, self.cache = {}, None
        self.peak_intermediate_bytes = 0
        if inventory_id:
            suffix = hashlib.sha256(json.dumps([side, drive_path]).encode()).hexdigest()[:16]
            self.cache = (state_dir, 'inventory-' + inventory_id + '-' + suffix + '.json')
            try:
                cache_path = Path(self.cache[0]) / 'disk' / self.cache[1]
                if cache_path.stat().st_size <= CACHE_LIMIT_BYTES:
                    self.directories = json.loads(cache_path.read_text(encoding='utf-8'))
                    self.peak_intermediate_bytes = cache_path.stat().st_size
            except FileNotFoundError:
                pass

    def save(self):
        if self.cache:
            # Keep a bounded subset of complete summaries. A cache miss rescans;
            # no partial tree measurement can become deletion evidence.
            bounded, size = {}, 2
            for key in sorted(self.directories, key=lambda path: len(Path(path).parts)):
                row = self.directories[key]
                length = len(json.dumps({key: row}, ensure_ascii=True).encode('utf-8'))
                if size + length > CACHE_LIMIT_BYTES:
                    continue
                bounded[key] = row
                size += length
            disk_state.save_file(*self.cache, bounded)
            actual = (Path(self.cache[0]) / 'disk' / self.cache[1]).stat().st_size
            self.peak_intermediate_bytes = max(self.peak_intermediate_bytes, actual)

    def measure(self, path, *, strict=False):
        row = self._scan(Path(path))
        if strict and row.get('error'):
            raise ValueError(row['error'])
        return dict(files=row['files'], bytes=row['bytes'], age_s=max(0, self.now - row['newest']),
                    largest=row['largest'], **({'error': row['error']} if row.get('error') else {}),
                    **({'incomplete': True} if row.get('incomplete') else {}))

    def _scan(self, path):
        key = str(path)
        if key in self.directories:
            return self.directories[key]
        row = dict(files=0, bytes=0, newest=0, largest=[], repository=False)
        try:
            if placement.cleanup_link(path):
                raise ValueError('link or reparse point: ' + key)
            info = path.stat()
            row['newest'] = 0 if placement.cleanup_parts(path) in self.ignore_activity else info.st_mtime
            if stat.S_ISDIR(info.st_mode):
                row['repository'] = path.name.casefold() in ('.git', '.venv') or repository_root(path)
                for child in path.iterdir():
                    part = self._scan(child)
                    row['repository'] |= part['repository']
                    if part.get('incomplete'):
                        row['incomplete'] = True
                    row['files'] += part['files']
                    row['bytes'] += part['bytes']
                    row['newest'] = max(row['newest'], part['newest'])
                    row['largest'] = heapq.nlargest(LIMIT, row['largest'] + part['largest'], key=lambda r: (r['bytes'], r['path']))
                    if part.get('error'):
                        row['error'] = part['error']
                self.directories[key] = row
            else:
                row.update(files=1, bytes=info.st_size, largest=[dict(path=key, bytes=info.st_size)])
        except (OSError, ValueError) as error:
            row['error'] = str(error)[:500]
            row['incomplete'] = isinstance(error, OSError)
        return row

    def contains_repository(self, path):
        row = self._scan(Path(path))
        return row['repository'] or bool(row.get('error'))

    def summarize(self, paths):
        """Aggregate disjoint status paths, including collapsed ignored directories."""
        roots, seen = [], set()
        for path in sorted(set(map(Path, paths)), key=lambda p: len(p.parts)):
            if not any(p in seen for p in (path, *path.parents)):
                roots.append(path)
                seen.add(path)
        parts = [self.measure(path) for path in roots]
        return dict(files=sum(p['files'] for p in parts), bytes=sum(p['bytes'] for p in parts),
                    age_s=min((p['age_s'] for p in parts), default=0),
                    largest=heapq.nlargest(LIMIT, (v for p in parts for v in p['largest']),
                                          key=lambda r: (r['bytes'], r['path'])))


# Rows that always cross whole: fixed reason codes, never grouped under a root.
WHOLE = ('worktree_inventory', 'untracked_or_ignored', 'budget_usage', 'merged_live_task', 'process_out_of_scope',
         'process_visibility_unavailable', 'temp_root_elapsed', 'wsl_vhdx', 'wsl_vhdx_unavailable')


def root_of(policy, repos):
    """compact's root step, for `StageRun`: the root a row groups under, or None for a row that crosses whole, which
    a row with no path also does, as the process rows serve adds. `policy` is the disk_cleanup settings, whose temp and
    report roots group rows, as do `repos` and their worktree roots."""
    roots = [Path(placement.native(p)) for key in ('temp_roots', 'report_roots')
             for p in policy.get(key, [])]
    roots += [Path(placement.native(m['repo'])) for m in repos]
    roots += [r for m in repos for r in worktree_roots(m)]

    def root(row):
        if row.get('reason_code') in WHOLE or 'path' not in row:
            return None
        path = Path(row['path'])
        return row.get('root') or str(max((r for r in roots if r == path or r in path.parents),
                                         key=lambda r: len(r.parts), default=path.parent))
    return root


def bound_report(report):
    """Reduce samples, never totals, to reserve space below the 5 MB report budget."""
    foreign, unreadable, queries, items = {}, {}, {}, []
    for row in report['items']:
        reason = row.get('reason_code')
        if row.get('action') == 'report' and reason == 'process_out_of_scope':
            for side, count in (row.get('placements') or {row['side']: row['count']}).items():
                foreign[side] = max(foreign.get(side, 0), count)
        elif row.get('action') == 'report' and reason == 'process_visibility_unavailable' and 'count' in row:
            side = row['side']
            previous = unreadable.get(side)
            if previous is None:
                unreadable[side] = dict(row)
            elif 'processes' in previous or 'processes' in row:
                processes = {(p['pid'], p.get('name')): p for source in (previous, row)
                             for p in source.get('processes', [])}
                previous.update(processes=list(processes.values()), count=len(processes))
            elif row['count'] > previous['count']:
                unreadable[side] = dict(row)
        elif row.get('action') == 'report' and reason in ('process_query_timeout', 'process_visibility_unavailable'):
            queries[row['side']] = row
        else:
            items.append(row)
    items.extend(unreadable.values())
    items.extend(queries.values())
    if foreign:
        items.append(dict(action='report', reason_code='process_out_of_scope',
                          count=sum(foreign.values()), placements=foreign))
    report['items'] = items
    def trim(node, cap):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ('sample', 'largest', 'dirty', 'ignored', 'excluded') and isinstance(value, list):
                    node[key] = value[:cap]
                else:
                    trim(value, cap)
        elif isinstance(node, list):
            for value in node:
                trim(value, cap)
    for cap in (10, 5, 2, 1, 0):
        if len(json.dumps(report).encode('utf-8')) < 4_500_000:
            break
        trim(report, cap)


def safe_path(path, proven=None):
    """All ancestors must be concrete; protected names override broad declarations. `proven`, a set kept only for one
    validation pass, spares a folder's many paths from proving the same ancestors again."""
    path = Path(path).absolute()
    if any(part.casefold() in ('.git', '.venv') for part in path.parts):
        return False
    checked = []
    for parent in (path, *path.parents):
        if proven is not None and parent in proven:
            break
        try:
            if placement.cleanup_link(parent):
                return False
        except FileNotFoundError:
            continue
        checked.append(parent)
    if proven is not None:
        proven.update(parent for parent in checked if parent != path)
    return True


def repository_root(candidate):
    marker = candidate / '.git'
    if candidate.name == '.git':
        return True
    try:
        marker.lstat()
        return True
    except (FileNotFoundError, NotADirectoryError):
        pass
    # Even partial administrative layouts may contain unpublished objects.
    if any((candidate / name).exists() for name in ('HEAD', 'objects', 'refs')):
        return True
    return repository_config(candidate) if (candidate / 'config').exists() else False


def repository_config(candidate):
    """Git's config/administrative evidence, separate from path safety policy."""
    cfg = candidate / 'config'
    try:      # anything but a clean YES, a timeout or no git included, is evidence: retain
        ask = lambda *args: git_client.ask(candidate, *args, env=dict(os.environ, GIT_DIR=str(candidate)),
                                           timeout_s=git_client.NORMAL, no=())
        bare = ask('rev-parse', '--is-bare-repository')
        if bare.answer is git_client.Answer.YES and bare.out.strip() in ('true', 'false'):
            return True
        parsed = ask('config', '--file', str(cfg), '--list')     # git owns its config grammar (BOM, quoting, comments, casing)
        if parsed.answer is not git_client.Answer.YES or parsed.err:
            return True
        keys = [line.partition('=')[0].casefold() for line in parsed.out.splitlines()]
        return any(key == 'core.bare' or key.startswith(('include.', 'includeif.')) for key in keys)
    except Exception:   # 3M1: any failure, unavailable Git included, is no certain parse: retain
        return True


def worktree_roots(machine, *, this_side=False):
    """A repo's worktree roots: the Windows root and the WSL ext4 root (ADR 0007). `this_side` keeps only the roots
    this process can open; each side inventories and removes only what it owns."""
    roots = [machine['worktrees']] if machine.get('worktrees') else []   # per-path side checks stay with the callers
    from . import repos
    wsl = repos.worktree_root(machine, 'wsl') if machine.get('name') else None   # derived: never a machine fact
    if wsl and not (this_side and placement.cleanup_side() != 'wsl'):
        roots.append(wsl)
    return [Path(placement.native(r)) for r in roots]
