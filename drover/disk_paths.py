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
