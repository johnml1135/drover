"""Frozen copies of code that `disk_stage` replaced, kept as the oracles tests/test_disk_stage.py matches."""
import copy
import heapq
from pathlib import Path

from drover import disk_inventory, placement_paths


def todays_translation(request, side, convert):
    """disk_runtime.py:32, :36 and :39-58 at 16a3623, verbatim but for `convert` passed in and `placement` read as
    `placement_paths`: the oracle `for_side` must match. Frozen here, because Task 33 replaces that code with
    `for_side` itself."""
    placement = placement_paths
    request = copy.deepcopy(request)
    request['side'] = side
    request['state_dir'] = convert(request['state_dir'])
    snapshot = request.get('tasks') or {}
    if snapshot:
        snapshot['active_worktrees'] = [convert(p) for p in snapshot.get('active_worktrees', [])]
        for task in snapshot.get('tasks', []):
            if task.get('worktree'):
                task['worktree'] = convert(task['worktree'])
    for repo in request['repos']:
        for key in ('repo', 'worktrees'):
            if repo.get(key):
                repo[key] = convert(repo[key])
    for key in ('temp_roots', 'report_roots'):
        request['paths'][key] = [convert(p) for p in request['paths'].get(key, [])
                                 if placement.cleanup_path_side(p) == side]
    request['paths']['caches'] = [c for c in request['paths'].get('caches', [])
                                  if placement.cleanup_path_side(c.get('path', c.get('cwd'))) == side]
    for cache in request['paths'].get('caches', []):
        key = 'path' if 'path' in cache else 'cwd'
        cache[key] = convert(cache[key])
    return request


ROOTS = ('/var/reports', '/var/reports/deep')
WHOLE = ('worktree_inventory', 'untracked_or_ignored', 'budget_usage', 'merged_live_task', 'process_out_of_scope',
         'process_visibility_unavailable', 'temp_root_elapsed', 'wsl_vhdx', 'wsl_vhdx_unavailable')


def root_of(row):
    """compact's root step at 16a3623 (disk_inventory.py:118-128) over ROOTS, as disk_side will pass it in."""
    if row.get('reason_code') in WHOLE:
        return None
    path = Path(row['path'])
    return row.get('root') or str(max((Path(r) for r in ROOTS if Path(r) in (path, *path.parents)),
                                      key=lambda r: len(r.parts), default=path.parent))


def todays_compact(items, roots):
    """`disk_inventory.compact` at e09ef3e over temp roots `roots`, kept as the oracle `StageRun` and `read_rows` match:
    serve grouped a stage's whole answer with it, before rows crossed as lines."""
    roots = [Path(placement_paths.native(p)) for p in roots]
    groups, answer = {}, []
    for original in items:
        row = dict(original)
        for key in ('dirty', 'ignored', 'excluded'):
            if key in row:
                row[key + '_count'] = len(row[key])
                row[key] = row[key][:disk_inventory.LIMIT]
        if row.get('reason_code') in WHOLE:
            answer.append(row)
            continue
        path = Path(row['path'])
        root = row.get('root') or str(max((r for r in roots if r == path or r in path.parents),
                                         key=lambda r: len(r.parts), default=path.parent))
        groups.setdefault((row['reason_code'], root, row['action']), []).append(row)
    for (reason, root, action), rows in groups.items():
        if len(rows) <= disk_inventory.LIMIT:
            answer.extend(rows)
        else:
            answer.append(dict(path=root, root=root, action=action, reason_code=reason, count=len(rows),
                               files=sum(r.get('files', 0) for r in rows), bytes=sum(r.get('bytes', 0) for r in rows),
                               age_s=min(r.get('age_s', 0) for r in rows), owner=None,
                               sample=heapq.nlargest(disk_inventory.LIMIT, rows,
                                                     key=lambda r: (r.get('bytes', 0), r['path']))))
    return answer
