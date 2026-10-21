"""Safe declared path stages. No inferred tool, repository or language policy."""
import shutil
from pathlib import Path

from . import placement


def result():
    return dict(items=[], errors=[], reclaimed_bytes=0)


def within(path, root):
    return path == root or root in path.parents


def facts(path, now):
    """Checked tree facts. Any link or enumeration error retains the subtree."""
    path = Path(path)
    if placement.cleanup_link(path):
        raise ValueError('link or reparse point')
    info = path.stat()
    count, size, newest = (0, 0, info.st_mtime) if path.is_dir() else (1, info.st_size, info.st_mtime)
    if path.is_dir():
        for child in path.iterdir():
            child_facts = facts(child, now)
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
    return all(not placement.cleanup_link(parent) for parent in (path, *path.parents) if parent.exists() or parent.is_symlink())


def temporary(request):
    out = result()
    now = request['now']
    state = Path(request['state_dir']).absolute()
    scratch = state / 'scratch'
    protected = [Path(repo[key]).absolute() for repo in request['repos']
                 for key in ('repo', 'worktrees') if repo.get(key)]
    proof = placement.cleanup_processes()
    for root_text in request['paths'].get('temp_roots', []):
        root = Path(root_text).absolute()
        if not root.exists() or not safe_path(root):
            continue
        # Broad roots cannot select the state directory; visit its sole eligible subtree explicitly.
        roots = [scratch] if within(root, state) and not within(root, scratch) else [root]
        if within(state, root) and scratch not in roots:
            roots.append(scratch)
        for eligible in roots:
            if not eligible.is_dir() or not safe_path(eligible):
                continue
            try:
                candidates = list(eligible.iterdir())
            except OSError as error:
                out['errors'].append(dict(path=str(eligible), error=str(error)))
                continue
            for path in candidates:
                row = item(path, request, 'temp_retained')
                try:
                    if (within(path, state) and not within(path, scratch)) or within(state, path) or \
                            any(within(path, p) or within(p, path) for p in protected) or not safe_path(path):
                        row['reason_code'] = 'protected'
                    else:
                        row.update(facts(path, now))
                        if row['age_s'] >= 7 * 86400 and not proof.busy(str(path)):
                            row.update(action='candidate', reason_code='stale_temp')
                            if request['apply'] and facts(path, now) == {k: row[k] for k in ('files', 'bytes', 'age_s')} \
                                    and safe_path(path) and not placement.cleanup_processes().busy(str(path)):
                                shutil.rmtree(path) if path.is_dir() else path.unlink()
                                row['action'] = 'removed'
                                out['reclaimed_bytes'] += row['bytes']
                except (OSError, ValueError) as error:
                    row['reason_code'] = 'unavailable'
                    row['error'] = str(error)
                out['items'].append(row)
    return out


def caches(request):
    """Literal machine argv, cwd and inherited environment, with bounded execution."""
    import subprocess
    out = result()
    for entry in request['paths'].get('caches', []):
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


def output_evidence(request, machine, entry):
    from . import ownership
    from .disk_worktrees import git, outputs_at, status, task_facts
    path = Path(placement.native(entry['worktree']))
    if path == Path(placement.native(machine['repo'])) or not path.is_dir() or not safe_path(path) or \
            placement.worktree_side(entry['worktree'], entry.get('locked')) != placement.cleanup_side() or \
            not placement.cleanup_contains(request, entry['worktree']):
        raise ValueError('not an eligible owning-side worktree')
    tag = ownership.read(str(path))
    if task_facts(request, path, tag)[0] or placement.cleanup_processes().busy(str(path)):
        raise ValueError('live worktree')
    head = git(path, 'rev-parse', '--verify', 'HEAD').strip()
    declarations = outputs_at(machine, head, path)
    git(path, 'diff', '--quiet', '--')
    git(path, 'diff', '--cached', '--quiet', '--')
    dirty, ignored = status(path)
    roots = [path / rel for rel in declarations]
    if any(not any(within(Path(name), root) for root in roots) for name in dirty):
        raise ValueError('dirty worktree')
    if git(path, 'rev-parse', '--verify', 'HEAD').strip() != head or ownership.read(str(path)) != tag:
        raise ValueError('worktree identity changed')
    return path, head, tag, declarations


def outputs(request: dict) -> dict:
    """Only committed, untracked, safely contained outputs in clean idle checkouts."""
    from .disk_worktrees import entries, git, overlaps_worktrees
    out = result()
    for machine in request['repos']:
        try:
            listed = entries(machine['repo'])
        except (OSError, ValueError) as error:
            out['errors'].append(dict(repo=machine['repo'], error=str(error)))
            continue
        for entry in listed:
            try:
                path, head, tag, declarations = output_evidence(request, machine, entry)
                for relative in declarations:
                    target = path / relative
                    if not target.exists() and not target.is_symlink():
                        continue
                    row = item(target, request, 'declared_output', repo=machine['repo'])
                    try:
                        if overlaps_worktrees(request, target, path) or not within(target, path) or not safe_path(target) or \
                                any(not safe_path(p) for p in target.rglob('*')):
                            raise ValueError('protected output contents')
                        if git(path, 'ls-files', '-z', '--', relative):
                            raise ValueError('output contains tracked files')
                        row.update(facts(target, request['now']), owner=tag['owner'] if tag else None,
                                   action='candidate')
                        if request['apply']:
                            current = next(e for e in entries(machine['repo']) if e['worktree'] == entry['worktree'])
                            if current != entry or output_evidence(request, machine, current) != (path, head, tag, declarations) \
                                    or facts(target, request['now']) != {k: row[k] for k in ('files', 'age_s', 'bytes')} \
                                    or not safe_path(target) or any(not safe_path(p) for p in target.rglob('*')):
                                raise ValueError('output evidence changed')
                            after = next(e for e in entries(machine['repo']) if e['worktree'] == entry['worktree'])
                            if after != entry or overlaps_worktrees(request, target, path):
                                raise ValueError('output side marker changed')
                            shutil.rmtree(target) if target.is_dir() else target.unlink()
                            row['action'] = 'removed'
                            out['reclaimed_bytes'] += row['bytes']
                    except (OSError, ValueError) as error:
                        row.update(action='keep', error=str(error))
                    out['items'].append(row)
            except (OSError, ValueError):
                # Stage 4/6 inventories the worktree that cannot authorize output deletion.
                continue
    return out


def report(request: dict) -> dict:
    """Generic inventory, including excluded descendants; this stage never mutates."""
    from . import ownership
    from .disk_worktrees import entries, status
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
                if placement.worktree_side(entry['worktree'], entry.get('locked')) != placement.cleanup_side():
                    continue
                try:
                    dirty, ignored = status(path)
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
