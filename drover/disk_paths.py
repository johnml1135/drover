"""Safe declared path stages. No inferred tool, repository or language policy."""
import shutil
import subprocess
import time
from pathlib import Path

from . import placement


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


def safe_path(path):
    """All ancestors must be concrete; protected names override broad declarations."""
    path = Path(path).absolute()
    if any(part.casefold() in ('.git', '.venv') for part in path.parts):
        return False
    for parent in (path, *path.parents):
        try:
            if placement.cleanup_link(parent):
                return False
        except FileNotFoundError:
            continue
    return True


def repository_root(candidate):
    import os
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
    cfg = candidate / 'config'
    if cfg.exists():
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
            if any(key == 'core.bare' or key.startswith(('include.', 'includeif.')) for key in keys):
                return True
        except (OSError, ValueError, subprocess.SubprocessError):
            return True  # No certain parse, including unavailable Git: retain.
    return False


def repository_tree(path, *, boundary=None):
    """Repository evidence protects even damaged, unregistered and bare trees.

    A worktree's own gitfile is allowed only at the output stage's checked boundary.
    Never follow links while looking for evidence; unreadable trees fail closed.
    """
    path = Path(path).absolute()
    for parent in (path, *path.parents):
        if boundary is not None and placement.cleanup_parts(parent) == placement.cleanup_parts(boundary):
            break
        if not safe_path(parent) or repository_root(parent):
            return True
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


class TempBudgetExceeded(ValueError):
    """A temp root used up its scan budget; whatever it did not reach stays unproven and is kept."""


def temp_facts(path, request):
    """Prove safety and measure in one pass; stop at the first protected descendant."""
    roots = [Path(request['state_dir']).absolute()]
    roots += [Path(placement.native(repo['repo'])).absolute() for repo in request['repos'] if repo.get('repo')]
    roots += [root.absolute() for repo in request['repos'] for root in worktree_roots(repo)]
    if not safe_path(path) or live_tree(path, request) or \
            any(within(path, root) or within(root, path) for root in roots) or \
            any(repository_root(parent) for parent in path.parents):
        raise ValueError('protected subtree')
    cached = request.setdefault('_temp_facts', {})
    children = request.setdefault('_temp_children', {})
    declared = {Path(p).absolute() for p in request['paths'].get('temp_roots', [])}
    def visit(current):
        if current in cached:
            value = cached[current]
            if isinstance(value, Exception):
                raise value
            return value
        try:
            value = scan(current)
        except TempBudgetExceeded:
            raise  # a budget stop is not a fact about this path; never cache it
        except (OSError, ValueError) as error:
            cached[current] = error
            raise
        if current.is_dir():
            cached[current] = value
        return value
    def scan(current):
        deadline = request.get('_temp_deadline')
        if deadline is not None and time.monotonic() > deadline:
            raise TempBudgetExceeded('temp root scan budget exceeded')
        if placement.cleanup_link(current) or current.name.casefold() in ('.git', '.venv') or \
                not placement.cleanup_contains(request, str(current)) or repository_root(current):
            raise ValueError('protected descendant')
        info = current.stat()
        files, size, newest = 0, 0, info.st_mtime
        if current.is_dir():
            entries = list(current.iterdir()) if current in declared else current.iterdir()
            if current in declared:
                children[current] = entries
            for child in entries:
                count, length, activity = visit(child)
                files += count
                size += length
                newest = max(newest, activity)
        else:
            files, size = 1, info.st_size
        return files, size, newest
    files, size, newest = visit(path)
    return dict(files=files, bytes=size, age_s=max(0, request['now'] - newest))


def temporary(request):
    out = result()
    proof = request.get('_processes') or placement.cleanup_processes(request=request)
    request['_temp_facts'], request['_temp_children'] = {}, {}
    for root in sorted({Path(p).absolute() for p in request['paths'].get('temp_roots', [])}, key=lambda p: len(p.parts)):
        started = time.monotonic()
        request['_temp_deadline'] = started + request['paths'].get('temp_root_budget_s', 300)
        partial = False
        try:
            if not root.is_dir() or not safe_path(root):
                continue
            children = request['_temp_children']
            if root not in children:
                children[root] = list(root.iterdir())
            for path in children[root]:
                row = item(path, request, 'temp_retained')
                try:
                    row.update(temp_facts(path, request))
                    if row['age_s'] >= 7 * 86400:
                        if proof.busy(str(path)):
                            row['reason_code'] = getattr(proof, 'reason', None) or 'process_busy'
                        else:
                            row.update(action='candidate', reason_code='stale_temp')
                            if request['apply']:
                                current = placement.cleanup_processes(request=request)
                                if current.busy(str(path)):
                                    row.update(action='keep', reason_code=getattr(current, 'reason', None) or 'process_busy')
                                elif temp_facts(path, dict(request, _temp_facts={}, _temp_children={})) == {k: row[k] for k in ('files', 'bytes', 'age_s')} and placement.cleanup_contains(request, str(path)):
                                    shutil.rmtree(path) if path.is_dir() else path.unlink()
                                    row['action'] = 'removed'
                                    out['reclaimed_bytes'] += row['bytes']
                except TempBudgetExceeded:
                    partial = True
                    row.update(action='keep', reason_code='temp_root_partial')
                except (OSError, ValueError) as error:
                    row.update(action='keep', reason_code='protected', error=str(error))
                out['items'].append(row)
                if partial:
                    break
        except OSError as error:
            out['errors'].append(dict(path=str(root), error=str(error)))
        finally:
            if partial:
                out['items'].append(dict(path=str(root), action='report', reason_code='temp_root_partial',
                                         budget_s=request['paths'].get('temp_root_budget_s', 300)))
            out['items'].append(dict(path=str(root), action='report', reason_code='temp_root_elapsed',
                                     elapsed_s=time.monotonic() - started))
    return out


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
    from .disk_worktrees import git, outputs_at
    path = Path(placement.native(entry['worktree']))
    if placement.cleanup_parts(path) == placement.cleanup_parts(machine['repo']) or not path.is_dir() or not safe_path(path) or \
            placement.worktree_side(entry['worktree'], entry.get('locked')) != placement.cleanup_side() or \
            not placement.cleanup_contains(request, entry['worktree']):
        raise ValueError('not an eligible owning-side worktree')
    if (placement.cleanup_processes(request=request) if fresh else request.get('_processes') or placement.cleanup_processes(request=request)).busy(str(path)):
        raise ValueError('process using worktree')
    if request['drive'].get('level') not in ('hold', 'critical') and facts(path, request['now'], ignore_activity=touched, request=request)['age_s'] < 3 * 86400:
        raise ValueError('worktree idle less than three days')
    head = git(path, 'rev-parse', '--verify', 'HEAD').strip()
    return path, head, ownership.read(str(path)), outputs_at(machine, path)


def outputs(request: dict) -> dict:
    """Remove only declared ignored/untracked files; source edits always survive."""
    from .disk_worktrees import registration_identity, entries, git, overlaps_worktrees, status
    out = result()
    for machine in request['repos']:
        try:
            listed = entries(machine['repo'])
            for entry in listed:
                try:
                    evidence = output_evidence(request, machine, entry)
                    path, head, tag, declarations = evidence
                    dirty, ignored = status(path, expand=request['apply'])
                    if not request['apply'] and declarations and any(
                            Path(name).is_dir() and not disposable_match(Path(name).relative_to(path), declarations)
                            for name in dirty + ignored):
                        dirty, ignored = status(path)
                    tracked = set(git(path, 'ls-files', '-z').split('\0'))
                    touched = set()
                    for name in dict.fromkeys(dirty + ignored):
                        target = Path(name)
                        relative = target.relative_to(path)
                        if str(relative).replace('\\', '/') in tracked or not disposable_match(relative, declarations):
                            continue
                        row = item(target, request, 'declared_disposable', repo=machine['repo'], root=str(path))
                        try:
                            if not placement.cleanup_contains(request, str(target)):
                                row['reason_code'] = 'drive_unproven'
                            elif live_tree(target, request):
                                row['reason_code'] = 'live_owner'
                            elif not safe_path(target) or (repository_tree(target, boundary=path) if request['apply'] else request['_inventory'].contains_repository(target)) or overlaps_worktrees(request, target, path):
                                row['reason_code'] = 'protected'
                            else:
                                row.update(facts(target, request['now'], request=request), owner=tag['owner'] if tag else None, action='candidate')
                                if request['apply']:
                                    current = next(e for e in entries(machine['repo']) if placement.cleanup_parts(e['worktree']) == placement.cleanup_parts(entry['worktree']))
                                    current_dirty, current_ignored = status(path)
                                    if registration_identity(current) != registration_identity(entry):
                                        raise ValueError('disposable registration changed')
                                    fresh_evidence = output_evidence(request, machine, current, touched=touched, fresh=True)
                                    if placement.cleanup_parts(fresh_evidence[0]) != placement.cleanup_parts(evidence[0]) or \
                                            fresh_evidence[1:] != evidence[1:] or \
                                            name not in current_dirty + current_ignored or \
                                            git(path, 'ls-files', '-z', '--', str(relative)) or \
                                            facts(target, request['now']) != {k: row[k] for k in ('files', 'age_s', 'bytes')}:
                                        raise ValueError('disposable evidence changed')
                                    after = next(e for e in entries(machine['repo']) if placement.cleanup_parts(e['worktree']) == placement.cleanup_parts(entry['worktree']))
                                    if registration_identity(after) != registration_identity(entry) or overlaps_worktrees(request, target, path) or repository_tree(target, boundary=path) or \
                                            not placement.cleanup_contains(request, str(target)) or live_tree(target, request):
                                        raise ValueError('disposable side marker changed')
                                    # status expands ignored directories to leaves; only empty directories reach here.
                                    target.rmdir() if target.is_dir() else target.unlink()
                                    touched.add(target.parent)
                                    row['action'] = 'removed'
                                    out['reclaimed_bytes'] += row['bytes']
                        except (OSError, ValueError, StopIteration, subprocess.SubprocessError) as error:
                            row.update(action='keep', reason_code='output_evidence_unavailable', error=str(error))
                        out['items'].append(row)
                except (OSError, ValueError, StopIteration, subprocess.SubprocessError) as error:
                    out['items'].append(item(entry['worktree'], request, 'output_evidence_unavailable',
                                             repo=machine['repo'], error=str(error)))
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            out['items'].append(item(machine['repo'], request, 'repository_unavailable', error=str(error)))
            out['errors'].append(dict(repo=machine['repo'], reason_code='repository_unavailable', error=str(error)))
    return out


def report(request: dict) -> dict:
    """One bounded summary per worktree/folder; this stage never mutates."""
    from . import ownership
    from .disk_inventory import Inventory
    from .disk_worktrees import entries, status, task_facts, merged, git
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
                        merged(machine, git(path, 'rev-parse', '--verify', 'HEAD').strip()):
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
