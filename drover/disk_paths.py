"""Safe declared path stages. No inferred tool, repository or language policy."""
import shutil
import subprocess
from pathlib import Path

from . import placement


def result():
    return dict(items=[], errors=[], reclaimed_bytes=0)


def within(path, root):
    return path == root or root in path.parents


def facts(path, now, *, ignore_activity=()):
    """Checked tree facts. Any link or enumeration error retains the subtree."""
    path = Path(path)
    if placement.cleanup_link(path):
        raise ValueError('link or reparse point')
    info = path.stat()
    count, size, newest = (0, 0, (0 if path in ignore_activity else info.st_mtime)) if path.is_dir() else (1, info.st_size, info.st_mtime)
    if path.is_dir():
        for child in path.iterdir():
            child_facts = facts(child, now, ignore_activity=ignore_activity)
            count += child_facts['files']
            size += child_facts['bytes']
            newest = max(newest, now - child_facts['age_s'])
    return dict(files=count, bytes=size, age_s=max(0, now - newest))


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


def repository_tree(path, *, boundary=None):
    """Repository evidence protects even damaged, unregistered and bare trees.

    A worktree's own gitfile is allowed only at the output stage's checked boundary.
    Never follow links while looking for evidence; unreadable trees fail closed.
    """
    import os
    path = Path(path).absolute()
    def root(candidate):
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
    for parent in (path, *path.parents):
        if parent == boundary:
            break
        if not safe_path(parent) or root(parent):
            return True
    if path.is_dir():
        for child in path.iterdir():
            if not safe_path(child) or root(child) or child.is_dir() and repository_tree(child, boundary=path):
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


def protected_path(path, request):
    path = Path(path).absolute()
    roots = [Path(request['state_dir']).absolute()]
    roots += [Path(placement.native(repo[key])).absolute() for repo in request['repos']
              for key in ('repo', 'worktrees') if repo.get(key)]
    return not placement.cleanup_contains(request, str(path)) or live_tree(path, request) or not safe_path(path) or any(within(path, root) or within(root, path) for root in roots) or \
        repository_tree(path)


def temp_facts(path, request):
    if protected_path(path, request):
        raise ValueError('protected subtree')
    def prove(parent):
        for child in parent.iterdir():
            if protected_path(child, request):
                raise ValueError('protected descendant')
            if child.is_dir():
                prove(child)
    if path.is_dir():
        prove(path)
    return facts(path, request['now'])


def temporary(request):
    out = result()
    proof = placement.cleanup_processes()
    for root_text in request['paths'].get('temp_roots', []):
        root = Path(root_text).absolute()
        try:
            if not root.is_dir() or not safe_path(root):
                continue
            for path in root.iterdir():
                row = item(path, request, 'temp_retained')
                try:
                    row.update(temp_facts(path, request))
                    if row['age_s'] >= 7 * 86400:
                        if proof.busy(str(path)):
                            row['reason_code'] = getattr(proof, 'reason', None) or 'process_busy'
                        else:
                            row.update(action='candidate', reason_code='stale_temp')
                            if request['apply']:
                                current = placement.cleanup_processes()
                                if current.busy(str(path)):
                                    row.update(action='keep', reason_code=getattr(current, 'reason', None) or 'process_busy')
                                elif temp_facts(path, request) == {k: row[k] for k in ('files', 'bytes', 'age_s')} and placement.cleanup_contains(request, str(path)):
                                    shutil.rmtree(path) if path.is_dir() else path.unlink()
                                    row['action'] = 'removed'
                                    out['reclaimed_bytes'] += row['bytes']
                except (OSError, ValueError) as error:
                    row.update(action='keep', reason_code='protected', error=str(error))
                out['items'].append(row)
        except OSError as error:
            out['errors'].append(dict(path=str(root), error=str(error)))
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
                proof = placement.cleanup_processes()
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


def output_evidence(request, machine, entry, *, touched=()):
    from . import ownership
    from .disk_worktrees import git, outputs_at
    path = Path(placement.native(entry['worktree']))
    if path == Path(placement.native(machine['repo'])) or not path.is_dir() or not safe_path(path) or \
            placement.worktree_side(entry['worktree'], entry.get('locked')) != placement.cleanup_side() or \
            not placement.cleanup_contains(request, entry['worktree']):
        raise ValueError('not an eligible owning-side worktree')
    if placement.cleanup_processes().busy(str(path)):
        raise ValueError('process using worktree')
    if request['drive'].get('level') not in ('hold', 'critical') and facts(path, request['now'], ignore_activity=touched)['age_s'] < 3 * 86400:
        raise ValueError('worktree idle less than three days')
    head = git(path, 'rev-parse', '--verify', 'HEAD').strip()
    return path, head, ownership.read(str(path)), outputs_at(machine, head, path)


def outputs(request: dict) -> dict:
    """Remove only declared ignored/untracked files; source edits always survive."""
    from .disk_worktrees import entries, git, overlaps_worktrees, status
    out = result()
    for machine in request['repos']:
        try:
            listed = entries(machine['repo'])
            for entry in listed:
                try:
                    evidence = output_evidence(request, machine, entry)
                    path, head, tag, declarations = evidence
                    dirty, ignored = status(path)
                    tracked = set(git(path, 'ls-files', '-z').split('\0'))
                    touched = set()
                    for name in dict.fromkeys(dirty + ignored):
                        target = Path(name)
                        relative = target.relative_to(path)
                        if str(relative).replace('\\', '/') in tracked or not disposable_match(relative, declarations):
                            continue
                        row = item(target, request, 'declared_disposable', repo=machine['repo'])
                        try:
                            if not placement.cleanup_contains(request, str(target)):
                                row['reason_code'] = 'drive_unproven'
                            elif live_tree(target, request):
                                row['reason_code'] = 'live_owner'
                            elif repository_tree(target, boundary=path) or overlaps_worktrees(request, target, path):
                                row['reason_code'] = 'protected'
                            else:
                                row.update(facts(target, request['now']), owner=tag['owner'] if tag else None, action='candidate')
                                if request['apply']:
                                    current = next(e for e in entries(machine['repo']) if e['worktree'] == entry['worktree'])
                                    current_dirty, current_ignored = status(path)
                                    if current != entry or output_evidence(request, machine, current, touched=touched) != evidence or \
                                            name not in current_dirty + current_ignored or \
                                            git(path, 'ls-files', '-z', '--', str(relative)) or \
                                            facts(target, request['now']) != {k: row[k] for k in ('files', 'age_s', 'bytes')}:
                                        raise ValueError('disposable evidence changed')
                                    after = next(e for e in entries(machine['repo']) if e['worktree'] == entry['worktree'])
                                    if after != entry or overlaps_worktrees(request, target, path) or repository_tree(target, boundary=path) or \
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
    """Generic inventory, including excluded descendants; this stage never mutates."""
    from . import ownership
    from .disk_worktrees import entries, status, task_facts, merged, git
    out = result()
    def record(path, reason, owner=None):
        row = item(path, request, reason)
        row['owner'] = owner
        try:
            row.update(facts(path, request['now']))
        except (OSError, ValueError) as error:
            row['error'] = str(error)
        out['items'].append(row)
    for machine in request['repos']:
        try:
            listed = entries(machine['repo'])
            for entry in listed:
                path = Path(placement.native(entry['worktree']))
                if not placement.cleanup_contains(request, entry['worktree']):
                    continue
                # Foreign-side markers and owner notes are inventory only; never unlock.
                tag = ownership.read(str(path)) if path.exists() else None
                owner = tag['owner'] if tag else None
                record(path, 'worktree_inventory', owner)
                if tag and tag['owner'].startswith('drover:') and task_facts(request, path, tag)[0] and \
                        placement.worktree_side(entry['worktree'], entry.get('locked')) == placement.cleanup_side() and \
                        merged(machine, git(path, 'rev-parse', '--verify', 'HEAD').strip()):
                    record(path, 'merged_live_task', owner)
                    out['items'][-1].update(task_id=tag['owner'][len('drover:'):], repo=machine['name'])
                if placement.worktree_side(entry['worktree'], entry.get('locked')) != placement.cleanup_side():
                    continue
                try:
                    dirty, ignored = status(path)
                    touched = set()
                    for name in dict.fromkeys(dirty + ignored):
                        record(Path(name), 'untracked_or_ignored', owner)
                except (OSError, ValueError):
                    pass
            root = Path(placement.native(machine['worktrees']))
            registered = {str(Path(placement.native(e['worktree']))) for e in listed}
            if root.is_dir() and safe_path(root):
                for path in root.iterdir():
                    if str(path) not in registered and placement.cleanup_contains(request, str(path)):
                        record(path, 'unlisted_folder')
        except (OSError, ValueError) as error:
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
