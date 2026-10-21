"""Bounded cleanup measurements and reports; cached inventory never authorizes deletion."""
import hashlib
import heapq
import json
import stat
from pathlib import Path

from . import disk_state, placement

LIMIT = 20


class Inventory:
    """One tree traversal per root across report stages, with bounded directory summaries."""

    def __init__(self, now, request=None, *, ignore_activity=()):
        self.now, self.ignore_activity = now, ignore_activity
        self.directories, self.cache = {}, None
        if request and request.get('_inventory_id'):
            suffix = hashlib.sha256(json.dumps([request.get('side', placement.cleanup_side()),
                                                request['drive']['path']]).encode()).hexdigest()[:16]
            self.cache = (request['state_dir'], 'inventory-' + request['_inventory_id'] + '-' + suffix + '.json')
            try:
                self.directories = json.loads((Path(self.cache[0]) / 'disk' / self.cache[1]).read_text(encoding='utf-8'))
            except FileNotFoundError:
                pass

    def save(self):
        if self.cache:
            disk_state.save_file(*self.cache, self.directories)

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
            row['newest'] = 0 if path in self.ignore_activity else info.st_mtime
            if stat.S_ISDIR(info.st_mode):
                from .disk_paths import repository_root
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


def compact(items, request):
    """Retained reasons aggregate by reason/root; full samples and path lists are capped."""
    roots = [Path(placement.native(p)) for key in ('temp_roots', 'report_roots')
             for p in request['paths'].get(key, [])]
    roots += [Path(placement.native(m[k])) for m in request['repos'] for k in ('repo', 'worktrees')]
    groups, answer = {}, []
    for original in items:
        row = dict(original)
        for key in ('dirty', 'ignored', 'excluded'):
            if key in row:
                row[key + '_count'] = len(row[key])
                row[key] = row[key][:LIMIT]
        if row.get('reason_code') in ('worktree_inventory', 'untracked_or_ignored', 'budget_usage',
                                       'merged_live_task', 'process_out_of_scope', 'process_visibility_unavailable'):
            answer.append(row)
            continue
        path = Path(row['path'])
        root = row.get('root') or str(max((r for r in roots if r == path or r in path.parents),
                                         key=lambda r: len(r.parts), default=path.parent))
        groups.setdefault((row['reason_code'], root, row['action']), []).append(row)
    for (reason, root, action), rows in groups.items():
        if len(rows) <= LIMIT:
            answer.extend(rows)
        else:
            answer.append(dict(path=root, root=root, action=action, reason_code=reason, count=len(rows),
                               files=sum(r.get('files', 0) for r in rows), bytes=sum(r.get('bytes', 0) for r in rows),
                               age_s=min(r.get('age_s', 0) for r in rows), owner=None,
                               sample=heapq.nlargest(LIMIT, rows, key=lambda r: (r.get('bytes', 0), r['path']))))
    return answer


def bound_report(report):
    """Reduce samples, never totals, to reserve space below the 5 MB report budget."""
    foreign, unreadable, items = {}, {}, []
    for row in report['items']:
        reason = row.get('reason_code')
        if row.get('action') == 'report' and reason == 'process_out_of_scope':
            for side, count in (row.get('placements') or {row['side']: row['count']}).items():
                foreign[side] = max(foreign.get(side, 0), count)
        elif row.get('action') == 'report' and reason == 'process_visibility_unavailable':
            side = row['side']
            if row['count'] > unreadable.get(side, {}).get('count', -1):
                unreadable[side] = row
        else:
            items.append(row)
    items.extend(unreadable.values())
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
