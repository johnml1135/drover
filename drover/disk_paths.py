"""Safe declared path stages. No inferred tool, repository or language policy."""
import subprocess
from functools import partial
from pathlib import Path

from . import placement

OUTPUT_IDLE_S = 2 * 3600  # disk_cleanup.worktree_output_idle_s default


def result():
    return dict(items=[], errors=[], reclaimed_bytes=0)


def within(path, root):
    parts, parent = placement.cleanup_parts(path), placement.cleanup_parts(root)
    return parts[:len(parent)] == parent


def facts(path, now, *, ignore_activity=(), request=None):
    """Strict safety facts. Only report-only requests may reuse an inventory."""
    from .disk_inventory import Inventory
    inventory = request.get('_inventory') if request and not request['apply'] and not ignore_activity else None
    row = (inventory or Inventory(now, ignore_activity=ignore_activity)).measure(path, strict=True)
    return {key: row[key] for key in ('files', 'bytes', 'age_s')}


def item(path, request, reason, **extra):
    return dict(path=str(path), action='keep', reason_code=reason, owner=None,
                files=0, age_s=0, bytes=0, **extra)


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
    import os
    cfg = candidate / 'config'
    try:
        env = dict(os.environ, GIT_DIR=str(candidate))
        bare = placement.git(candidate, 'rev-parse', '--is-bare-repository',
                             env=env, timeout=60)
        if bare.returncode == 0 and bare.stdout.strip() in ('true', 'false'):
            return True
        # Git owns its config grammar (BOM, quoting, comments, casing).
        parsed = placement.git(candidate, 'config', '--file', str(cfg), '--list',
                               env=env, timeout=60)
        if parsed.returncode or parsed.stderr:
            return True
        keys = [line.partition('=')[0].casefold() for line in parsed.stdout.splitlines()]
        return any(key == 'core.bare' or key.startswith(('include.', 'includeif.')) for key in keys)
    except (OSError, ValueError, subprocess.SubprocessError):
        return True  # No certain parse, including unavailable Git: retain.


def repository_tree(path, *, boundary=None, proven=None):
    """Repository evidence protects even damaged, unregistered and bare trees.

    A worktree's own gitfile is allowed only at the output stage's checked boundary.
    Never follow links while looking for evidence; unreadable trees fail closed. `proven` is as for safe_path.
    """
    path = Path(path).absolute()
    checked = []
    for parent in (path, *path.parents):
        if proven is not None and (parent, boundary) in proven:
            break
        if boundary is not None and placement.cleanup_parts(parent) == placement.cleanup_parts(boundary):
            break
        if not safe_path(parent, proven) or repository_root(parent):
            return True
        checked.append((parent, boundary))
    if proven is not None:
        proven.update(key for key in checked if key[0] != path)
    if path.is_dir():
        for child in path.iterdir():
            if not safe_path(child) or repository_root(child) or child.is_dir() and repository_tree(child, boundary=path):
                return True
    return False


def live_tree(path, request):
    """Fresh live paths protect both containing trees and their descendants."""
    from .disk_worktrees import task_facts
    snapshot = request.get('tasks') or {}
    owners = list(snapshot.get('active_worktrees', []))
    owners += [t['worktree'] for t in snapshot.get('tasks', []) if t.get('worktree')]
    parts = placement.cleanup_parts(path)
    for owner in owners:
        other = placement.cleanup_parts(owner)
        if (parts[:len(other)] == other or other[:len(parts)] == parts) and \
                task_facts(request, Path(placement.native(owner)), None)[0]:
            return True
    return False


def worktree_roots(machine, *, this_side=False):
    """A repo's worktree roots: the Windows root and the WSL ext4 root (ADR 0007). `this_side` keeps only the roots
    this process can open; each side inventories and removes only what it owns."""
    roots = [machine['worktrees']] if machine.get('worktrees') else []   # per-path side checks stay with the callers
    from . import repos
    wsl = repos.worktree_root(machine, 'wsl') if machine.get('name') else None   # derived: never a machine fact
    if wsl and not (this_side and placement.cleanup_side() != 'wsl'):
        roots.append(wsl)
    return [Path(placement.native(r)) for r in roots]


def protected_path(path, request):
    path = Path(path).absolute()
    roots = [Path(request['state_dir']).absolute()]
    roots += [Path(placement.native(repo['repo'])).absolute() for repo in request['repos'] if repo.get('repo')]
    roots += [root.absolute() for repo in request['repos'] for root in worktree_roots(repo)]
    return not placement.cleanup_contains(request, str(path)) or live_tree(path, request) or not safe_path(path) or any(within(path, root) or within(root, path) for root in roots) or \
        repository_tree(path)


def caches(request):
    """Literal machine argv, cwd and inherited environment, with bounded execution."""
    out = result()
    for entry in request['paths'].get('caches', []):
        if 'path' in entry:
            folder_cache(request, entry, out)
            continue
        row = item(entry['cwd'], request, 'declared_cache')
        row.update(argv=entry['argv'], action='candidate')
        if request['apply']:
            try:
                done = subprocess.run(entry['argv'], cwd=entry['cwd'], shell=False,
                                      timeout=120, capture_output=True, text=True)
                row.update(action='executed', returncode=done.returncode,
                           output=(done.stdout + done.stderr)[-4000:])
                if done.returncode:
                    out['errors'].append(dict(row))
            except (OSError, subprocess.TimeoutExpired) as error:
                out['errors'].append(dict(row, error=str(error)))
        out['items'].append(row)
    return out


def folder_cache(request, entry, out):
    root = Path(entry['path']).absolute()
    if protected_path(root, request):
        out['items'].append(item(root, request, 'protected'))
        return
    if not root.is_dir() or not safe_path(root):
        return
    aggressive = request.get('mode') != 'daily' and request['drive'].get('level') in ('warn', 'queue', 'hold', 'critical')
    try:
        candidates = []
        def leaves(parent):
            for path in parent.iterdir():
                if protected_path(path, request):
                    out['items'].append(item(path, request, 'protected'))
                elif path.is_dir():
                    yield from leaves(path)
                elif path.is_file():
                    yield path
        for path in leaves(root):
            info = path.stat()
            activity = info.st_atime if info.st_atime > info.st_mtime else info.st_mtime
            if aggressive or request['now'] - activity > entry.get('max_age_days', 30) * 86400:
                candidates.append((activity, str(path), path, info))
        for activity, _, path, info in sorted(candidates):
            if aggressive and request['apply']:
                free = placement.cleanup_free(root)
                if type(free) not in (int, float) or not __import__('math').isfinite(free) or free < 0 or free >= request['drive']['warn_gb']:
                    break
            row = item(path, request, 'folder_cache')
            row.update(action='candidate', files=1, bytes=info.st_size, age_s=max(0, request['now'] - activity))
            if request['apply']:
                proof = placement.cleanup_processes(request=request)
                if proof.busy(str(path)):
                    row.update(action='keep', reason_code=getattr(proof, 'reason', None) or 'process_busy')
                elif protected_path(path, request):
                    row.update(action='keep', reason_code='protected')
                elif path.stat() == info and placement.cleanup_contains(request, str(path)):
                    path.unlink()
                    row['action'] = 'removed'
                    out['reclaimed_bytes'] += info.st_size
            out['items'].append(row)
    except (OSError, ValueError) as error:
        out['errors'].append(dict(path=str(root), error=str(error)))


def disposable_match(relative, declarations):
    """Match a path or any containing directory against committed relative globs."""
    import fnmatch
    parts = Path(relative).parts
    def match(names, pattern):
        if not pattern:
            return not names
        if pattern[0] == '**':
            return match(names, pattern[1:]) or bool(names) and match(names[1:], pattern)
        return bool(names) and fnmatch.fnmatchcase(names[0], pattern[0]) and match(names[1:], pattern[1:])
    return any(match(parts[:end], tuple(pattern.split('/')))
               for pattern in declarations for end in range(1, len(parts) + 1))


def output_evidence(request, machine, entry, *, touched=(), fresh=False):
    from . import ownership
    from .disk_worktrees import head_at, outputs_at
    path = Path(placement.native(entry['worktree']))
    def reject(condition, detail):
        raise ValueError(f'not an eligible owning-side worktree: {condition} ({detail})')
    if placement.cleanup_parts(path) == placement.cleanup_parts(machine['repo']):
        reject('main_checkout', f'repo={machine["repo"]!r}')
    if not path.is_dir():
        reject('not_directory', f'path={str(path)!r}')
    if not safe_path(path):
        reject('unsafe_path', f'path={str(path)!r}')
    side = placement.worktree_side(entry['worktree'], entry.get('locked'))
    if side != placement.cleanup_side():
        reject('owner_side', f'owner={side!r}, worker={placement.cleanup_side()!r}, lock={entry.get("locked")!r}')
    if not placement.cleanup_contains(request, entry['worktree']):
        reject('physical_drive', f'path_side={placement.cleanup_path_side(entry["worktree"])!r}, '
               f'host={placement.physical_disk_path(entry["worktree"])!r}, '
               f'drive={request["drive"]["path"]!r}, '
               f'drive_host={placement.physical_disk_path(request["drive"]["path"])!r}')
    if (placement.cleanup_processes(request=request) if fresh else request.get('_processes') or placement.cleanup_processes(request=request)).busy(str(path)):
        raise ValueError('process using worktree')
    # This worker cannot see Windows processes; a budget carries what the coordinator found them naming inside WSL.
    if any(within(named, path) for named in (request.get('budget') or {}).get('windows_paths') or ()):
        raise ValueError('windows process using worktree')
    # Disposable paths only: whole-worktree removal keeps its own three-day settlement (disk_cleanup_decisions).
    # A drive in trouble waives the idle rule; a budget purge, which is about WSL and not that drive, never does.
    limit = request.get('paths', {}).get('worktree_output_idle_s', OUTPUT_IDLE_S)
    if (request.get('budget') or request['drive'].get('level') not in ('hold', 'critical')) and facts(path, request['now'], ignore_activity=touched, request=request)['age_s'] < limit:
        raise ValueError(f'worktree idle less than {limit:g} seconds')
    head = head_at(path)
    return path, head, ownership.read(str(path)), outputs_at(machine, path)


def prune_emptied(target, removed, touched):
    """Folders a removal emptied go with it, deepest first, up to the target; rmdir only ever takes an empty one."""
    folders = set()
    for name in removed:
        parent = Path(name).parent
        while parent not in folders:
            folders.add(parent)
            if parent == target:
                break
            parent = parent.parent
    for folder in sorted(folders, key=lambda p: len(p.parts), reverse=True):
        try:
            folder.rmdir()
            touched.add(folder.parent)
        except OSError:
            pass


def remove_output(request, out, machine, entry, evidence, touched, target, measured, row):
    """The one removal of declared outputs: `target` is proven again now, as one unit, immediately before its paths
    (`measured`, name -> facts when listed) go, and any change keeps them. Bytes count as each path actually goes."""
    from .disk_worktrees import registration_identity, entries, tracked_at, overlaps_worktrees, status
    path = evidence[0]
    try:
        current = next(e for e in entries(machine['repo']) if placement.cleanup_parts(e['worktree']) == placement.cleanup_parts(entry['worktree']))
        current_dirty, current_ignored = status(path)
        if registration_identity(current) != registration_identity(entry):
            raise ValueError('disposable registration changed')
        fresh_evidence = output_evidence(request, machine, current, touched=touched, fresh=True)
        tracked = {str(path / name) for name in tracked_at(path, target.relative_to(path))}
        if placement.cleanup_parts(fresh_evidence[0]) != placement.cleanup_parts(evidence[0]) or \
                fresh_evidence[1:] != evidence[1:] or \
                set(measured) - set(current_dirty + current_ignored) or \
                tracked & set(measured) or (tracked and str(target) in measured) or \
                any(facts(Path(name), request['now']) != known for name, known in measured.items()):
            raise ValueError('disposable evidence changed')
        after = next(e for e in entries(machine['repo']) if placement.cleanup_parts(e['worktree']) == placement.cleanup_parts(entry['worktree']))
        proven = set()
        if registration_identity(after) != registration_identity(entry) or overlaps_worktrees(request, target, path) or \
                any(repository_tree(Path(name), boundary=path, proven=proven) for name in measured) or \
                not placement.cleanup_contains(request, str(target)) or live_tree(target, request):
            raise ValueError('disposable side marker changed')
        for name, known in measured.items():
            leaf = Path(name)
            # status expands ignored directories to leaves; only empty directories reach here.
            leaf.rmdir() if leaf.is_dir() else leaf.unlink()
            touched.add(leaf.parent)
            out['reclaimed_bytes'] += known['bytes']
        if str(target) not in measured:
            prune_emptied(target, measured, touched)
        row['action'] = 'removed'
    except (OSError, ValueError, StopIteration, subprocess.SubprocessError) as error:
        row.update(action='keep', reason_code='output_evidence_unavailable', error=str(error))


def output_units(names, path, tracked, declarations, whole):
    """Group listed paths into what is revalidated as one: each path alone, or with `whole` (a budget purge) each
    shortest declared disposable prefix with every listed path under it. Tracked and undeclared paths are left out."""
    units, matched = {}, {}
    for name in dict.fromkeys(names):
        relative = Path(name).relative_to(path)
        if str(relative).replace('\\', '/') in tracked:
            continue
        for end in range(1, len(relative.parts) + 1):
            prefix = relative.parts[:end]
            if prefix not in matched:
                matched[prefix] = disposable_match(Path(*prefix), declarations)
            if matched[prefix]:
                units.setdefault(path.joinpath(*prefix) if whole else Path(name), []).append(name)
                break
    return units


def deadline_row(request, machine, left, later, reason='output_deadline'):
    """The one item a stage that ran out of time ends with: the worktrees it did not reach, and the repos after them.
    An item, not an error: the unreached worktrees are left alone, and every removal is re-proven."""
    return dict(item(machine['repo'], request, reason, count=len(left) + len(later),
                     worktrees=[e['worktree'] for e in left], repos=[m['repo'] for m in later]), action='report')


def disposable_rows(request, out, *, deadline=None):
    """Yield (row, remove) for each unit of declared ignored/untracked outputs, its row already in `out`; calling
    remove() runs the one revalidated removal. Whatever is not eligible is recorded in `out` as kept, never yielded.
    Registrations are listed once for the run to judge a first pass; remove() lists them afresh. Past `deadline`
    (a disk_budget clock time) it stops between units with an `output_deadline` row, so the worker can still answer."""
    from . import disk_budget
    from .disk_worktrees import entries, tracked_at, overlaps_worktrees, status
    budget = bool(request.get('budget'))
    late = lambda: deadline is not None and disk_budget._clock() >= deadline
    try:
        registered = [placement.cleanup_parts(e['worktree']) for m in request['repos'] for e in entries(m['repo'])]
    except (OSError, ValueError, subprocess.SubprocessError):
        registered = None       # each unit lists afresh and reports the failure on its own row
    for number, machine in enumerate(request['repos']):
        try:
            listed = entries(machine['repo'])
            for position, entry in enumerate(listed):
                if late():
                    out['items'].append(deadline_row(request, machine, listed[position:], request['repos'][number + 1:]))
                    return
                try:
                    evidence = output_evidence(request, machine, entry)
                    path, head, tag, declarations = evidence
                    idle = dict(idle_s=facts(path, request['now'], request=request)['age_s']) if budget else {}
                    expand = request['apply'] or budget
                    dirty, ignored = status(path, expand=expand)
                    if not expand and declarations and any(
                            Path(name).is_dir() and not disposable_match(Path(name).relative_to(path), declarations)
                            for name in dirty + ignored):
                        dirty, ignored = status(path)
                    tracked = set(tracked_at(path))
                    touched = set()     # shared by this worktree's removals, in whatever order they run
                    units = output_units(dirty + ignored, path, tracked, declarations, budget)
                    for count, (target, names) in enumerate(units.items(), 1):
                        def row_for(where):
                            return item(where, request, 'declared_disposable', repo=machine['repo'], root=str(path), **idle)
                        row, measured = row_for(target), {}
                        try:
                            if not placement.cleanup_contains(request, str(target)):
                                row['reason_code'] = 'drive_unproven'
                            elif live_tree(target, request):
                                row['reason_code'] = 'live_owner'
                            elif overlaps_worktrees(request, target, path, registered=registered):
                                row['reason_code'] = 'protected'
                            else:
                                proven = set()
                                for name in names:
                                    leaf = Path(name)
                                    if safe_path(leaf, proven) and not (repository_tree(leaf, boundary=path, proven=proven) if request['apply'] else request['_inventory'].contains_repository(leaf)):
                                        measured[name] = facts(leaf, request['now'], request=request)
                                    elif len(names) > 1:
                                        out['items'].append(dict(row_for(leaf), reason_code='protected'))
                                if measured:
                                    rows = list(measured.values())
                                    row.update(files=sum(r['files'] for r in rows), bytes=sum(r['bytes'] for r in rows),
                                               age_s=min(r['age_s'] for r in rows), owner=tag['owner'] if tag else None,
                                               action='candidate')
                                else:
                                    row['reason_code'] = 'protected'
                        except (OSError, ValueError, StopIteration, subprocess.SubprocessError) as error:
                            row.update(action='keep', reason_code='output_evidence_unavailable', error=str(error))
                        out['items'].append(row)
                        if row['action'] == 'candidate':
                            yield row, partial(remove_output, request, out, machine, entry, evidence, touched, target, measured, row)
                        if count < len(units) and late():
                            out['items'].append(deadline_row(request, machine, listed[position:], request['repos'][number + 1:]))
                            return
                except (OSError, ValueError, StopIteration, subprocess.SubprocessError) as error:
                    out['items'].append(item(entry['worktree'], request, 'output_evidence_unavailable',
                                             repo=machine['repo'], error=str(error)))
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            out['items'].append(item(machine['repo'], request, 'repository_unavailable', error=str(error)))
            out['errors'].append(dict(repo=machine['repo'], reason_code='repository_unavailable', error=str(error)))


def outputs(request: dict) -> dict:
    """Remove only declared ignored/untracked files; source edits always survive."""
    from .disk_budget import deadline
    out = result()
    for _, remove in disposable_rows(request, out, deadline=deadline(request)):
        if request['apply']:
            remove()
    return out


def report(request: dict) -> dict:
    """One bounded summary per worktree/folder; this stage never mutates."""
    from . import ownership
    from .disk_inventory import Inventory
    from .disk_worktrees import entries, status, task_facts, merged, head_at
    inventory = request.get('_inventory') or Inventory(request['now'])
    out = result()
    budgeted = set()
    def record(path, reason, owner=None):
        row = item(path, request, reason)
        row.update(inventory.measure(path), owner=owner)
        out['items'].append(row)
        return row
    for machine in request['repos']:
        try:
            listed = entries(machine['repo'])
            for entry in listed:
                path = Path(placement.native(entry['worktree']))
                if not placement.cleanup_contains(request, entry['worktree']):
                    continue
                tag = ownership.read(str(path)) if path.exists() else None
                owner = tag['owner'] if tag else None
                worktree_row = record(path, 'worktree_inventory', owner)
                if tag and tag['owner'].startswith('drover:') and task_facts(request, path, tag)[0] and \
                        placement.worktree_side(entry['worktree'], entry.get('locked')) == placement.cleanup_side() and \
                        merged(machine, head_at(path)):
                    record(path, 'merged_live_task', owner).update(task_id=tag['owner'][len('drover:'):], repo=machine['name'])
                if machine.get('budget_gb') and placement.cleanup_path(path) not in budgeted and safe_path(path) and \
                        placement.worktree_side(entry['worktree'], entry.get('locked')) == placement.cleanup_side():
                    row = record(path, 'budget_usage', owner)
                    if not row.get('incomplete'):
                        row.update(identity=placement.cleanup_path(path), repo=machine['name'], action='report')
                        budgeted.add(row['identity'])
                    else:
                        out['items'].pop()  # Partial inventories cannot establish budget usage.
                if placement.worktree_side(entry['worktree'], entry.get('locked')) != placement.cleanup_side():
                    continue
                try:
                    dirty, ignored = status(path, expand=False)
                    if dirty or ignored:
                        row = item(path, request, 'untracked_or_ignored')
                        row.update(inventory.summarize(dirty + ignored), owner=owner)
                        out['items'].append(row)
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    worktree_row['status_error'] = str(error)
            registered = {str(Path(placement.native(e['worktree']))) for e in listed}
            for root in worktree_roots(machine, this_side=True):
                if root.is_dir() and safe_path(root):
                    for path in root.iterdir():
                        if str(path) not in registered and placement.cleanup_contains(request, str(path)):
                            record(path, 'unlisted_folder')
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            out['errors'].append(dict(repo=machine['repo'], error=str(error)))
    for root_text in request['paths'].get('report_roots', []):
        root = Path(root_text)
        if not root.is_dir() or not safe_path(root):
            continue
        try:
            for path in root.iterdir():
                record(path, 'declared_report')
        except OSError as error:
            out['errors'].append(dict(path=str(root), error=str(error)))
    return out
