"""Checked worktree evidence and path-specific eviction; branches always survive."""
from pathlib import Path
import subprocess

from . import ownership, placement, repos
from .disk_paths import facts, item, result, safe_path, within, disposable_match, repository_tree


def git(repo, *args, env=None):
    done = placement.git(str(repo), *map(str, args), timeout=60, env=env)
    if done.returncode:
        raise ValueError(done.stderr.strip() or 'git command failed')
    return done.stdout


def entries(repo):
    rows = []
    for block in git(repo, 'worktree', 'list', '--porcelain', '-z').split('\0\0'):
        row = {}
        for field in block.split('\0'):
            key, _, value = field.partition(' ')
            if key:
                row[key] = value
        if 'worktree' in row:
            row['worktree'] = placement.cleanup_path(row['worktree'])
            rows.append(row)
    return rows


def outputs_at(machine, head, path):
    return repos.cleanup_config(repos.load(machine, revision=head, checkout=str(path)))['disposable']


def status(path, *, expand=True):
    """NUL-safe full status; expand ignored directories without following links."""
    raw = git(path, 'status', '--porcelain=v1', '-z', '--untracked-files=all' if expand else '--untracked-files=normal', '--ignored=matching')
    fields = iter(raw.split('\0'))
    dirty, ignored = [], []
    for field in fields:
        if not field:
            continue
        code, name = field[:2], field[3:]
        target = Path(path) / name
        if code == '!!':
            # Keep empty directories and every descendant, including protected/link entries.
            def leaves(p):
                if placement.cleanup_link(p):
                    return [str(p)]
                if p.is_dir():
                    children = list(p.iterdir())
                    return [s for child in children for s in leaves(child)] or [str(p)]
                return [str(p)]
            ignored.extend(leaves(target) if expand else [str(target)])
        else:
            dirty.append(str(target))
            if 'R' in code or 'C' in code:
                next(fields, None)
    return dirty, ignored


def task_facts(request, path, tag):
    snapshot = request.get('tasks') or {}
    at = snapshot.get('at')
    fresh = type(at) in (float, int) and 0 <= request['now'] - at <= 900
    if not fresh:
        return bool(tag and tag['owner'].startswith('drover:')), False
    live = any(placement.cleanup_parts(p) == placement.cleanup_parts(path)
               for p in snapshot.get('active_worktrees', []))
    terminal = False
    found = False
    for task in snapshot.get('tasks', []):
        owns = bool(tag and tag['owner'] == 'drover:' + str(task.get('id')))
        at_path = task.get('worktree') and placement.cleanup_parts(task['worktree']) == placement.cleanup_parts(path)
        if owns or at_path:
            live |= task.get('terminal') is not True or task.get('live') is True
        if owns:
            found = True
            terminal |= task.get('terminal') is True and task.get('live') is False
    if tag and tag['owner'].startswith('drover:') and not found:
        live = True
    return live, terminal


def merged(machine, head):
    repo = machine['repo']
    base = git(repo, 'rev-parse', '--verify', machine['remote'] + '/' + machine['default_branch'] + '^{commit}').strip()
    ancestor = placement.git(repo, 'merge-base', '--is-ancestor', head, base, timeout=60)
    if ancestor.returncode == 0:
        return True
    if ancestor.returncode != 1:
        raise ValueError('ancestry unavailable')
    cherry = git(repo, 'cherry', base, head).splitlines()
    if cherry and all(line.startswith('- ') for line in cherry):
        return True
    tree = placement.git(repo, 'merge-tree', '--write-tree', base, head, timeout=60)
    if tree.returncode not in (0, 1):
        raise ValueError('merge-tree unavailable')
    return tree.returncode == 0 and tree.stdout.splitlines()[0] == git(repo, 'rev-parse', base + '^{tree}').strip()


def inspect(request, machine, entry):
    path = Path(placement.native(entry['worktree']))
    row = item(path, request, 'unsettled', repo=machine['repo'], name=machine['name'],
               head=entry.get('HEAD'), branch=entry.get('branch'), side_lock=entry.get('locked'))
    lock = entry.get('locked')
    if placement.worktree_side(entry['worktree'], lock) != placement.cleanup_side():
        row['reason_code'] = 'other_side_or_lock_note'
        return row
    if not placement.cleanup_contains(request, entry['worktree']):
        row['reason_code'] = 'other_drive'
        return row
    if path == Path(placement.native(machine['repo'])):
        row['reason_code'] = 'main_checkout'
        return row
    proof = request.get('_processes') or placement.cleanup_processes()
    if task_facts(request, path, None)[0] or proof.busy(str(path)):
        row['reason_code'] = getattr(proof, 'reason', None) or 'busy'
        return row
    if not safe_path(path):
        row['reason_code'] = 'protected'
        return row
    if not path.exists():
        row.update(action='candidate', reason_code='missing')
        return row
    tag = ownership.read(str(path))
    row.update(tag=tag, owner=tag['owner'] if tag else None)
    live, terminal = task_facts(request, path, tag)
    if live:
        row['reason_code'] = 'busy'
        return row
    dirty, ignored = status(path, expand=request['apply'])
    row.update(dirty=dirty, ignored=ignored)
    if dirty:
        row['reason_code'] = 'dirty'
        return row
    head = git(path, 'rev-parse', '--verify', 'HEAD').strip()
    row.update(head=head, gitdir=git(path, 'rev-parse', '--absolute-git-dir').strip())
    row.update(facts(path, request['now'], request=request))
    lease = tag and tag['v'] == 2 and tag['repo'] == machine['name'] and tag['lease_until'] > request['now']
    if not lease:
        row['reason_code'] = 'orphan'
    elif terminal or merged(machine, head):
        row.update(action='candidate', reason_code='settled')
    return row


def settled(request: dict) -> dict:
    out = result()
    for machine in request['repos']:
        try:
            listed = entries(machine['repo'])
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            out['errors'].append(dict(repo=machine['repo'], error=str(error)))
            continue
        for entry in listed:
            try:
                row = inspect(request, machine, entry)
                row = quarantine(request, machine, row)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                row = item(entry['worktree'], request, 'unavailable', error=str(error))
                if request['apply']:
                    quarantine(request, machine, row)
            if request.get('approved_path'):
                if row['path'] != request['approved_path']:
                    continue
                if row['reason_code'] in ('unsettled', 'orphan'):
                    row['action'] = 'candidate'
            if request['apply'] and row['action'] == 'candidate':
                row = remove(request, row)
                if row.get('error'):
                    out['errors'].append(dict(row))
                if row['action'] == 'removed':
                    out['reclaimed_bytes'] += row['bytes']
            out['items'].append(row)
        if not request.get('approved_path'):
            loose = unlisted(request, machine, listed)
            out['items'].extend(loose)
            out['reclaimed_bytes'] += sum(r['bytes'] for r in loose if r['action'] == 'removed')
            out['errors'].extend(r for r in loose if r.get('error') and r.get('unlisted'))
    return out


def ignored_outside(path, ignored, declarations):
    return [name for name in ignored if repository_tree(Path(name), boundary=path) or
            not disposable_match(Path(name).relative_to(path), declarations)]


def remove(request: dict, selected: dict) -> dict:
    """Revalidate all evidence, then journal before unlocking or deleting by path."""
    import uuid
    from . import disk_state
    row = dict(selected)
    machine = next(m for m in request['repos'] if m['repo'] == row['repo'])
    path = Path(row['path'])
    record, unlocked = None, False
    try:
        entry = next((e for e in entries(machine['repo'])
                      if str(Path(placement.native(e['worktree']))) == str(path)), None)
        if row.get('unlisted'):
            if entry is not None or snapshot(machine, path) != row['head']:
                raise ValueError('snapshot changed')
            fingerprint = identity(path) + repr((row.get('head'), row.get('tag'), row.get('branch'),
                                               row.get('gitdir'), row.get('side_lock')))
            if fingerprint != row['quarantine_identity']:
                raise ValueError('snapshot activity changed')
            current = dict(row)
        else:
            if entry is None:
                raise ValueError('registration changed')
            current = inspect({k: v for k, v in request.items() if k != '_processes'}, machine, entry)
            if row['reason_code'] == 'quarantine_elapsed' and current['reason_code'] == 'orphan':
                fingerprint = identity(path) + repr((current.get('head'), current.get('tag'), current.get('branch'),
                                                   current.get('gitdir'), current.get('side_lock')))
                if fingerprint == row['quarantine_identity']:
                    current.update(action='candidate', reason_code='quarantine_elapsed')
        if request.get('approved_path') == str(path) and current['reason_code'] in ('unsettled', 'orphan'):
            current['action'] = 'candidate'
        after = next((e for e in entries(machine['repo'])
                      if str(Path(placement.native(e['worktree']))) == str(path)), None)
        if after != entry or (path.exists() and ownership.read(str(path)) != current.get('tag')):
            current.update(action='keep', reason_code='identity_changed')
            return quarantine(request, machine, current) if row.get('unlisted') else current
        keys = ('head', 'branch', 'side_lock', 'gitdir', 'tag')
        if current['action'] != 'candidate' or any(current.get(k) != row.get(k) for k in keys):
            return dict(current, action='keep', reason_code='revalidation_changed')
        if overlaps_worktrees(request, path, path):
            return dict(current, action='keep', reason_code='nested_worktree')
        if current['reason_code'] != 'missing':
            excluded = ignored_outside(path, current['ignored'], outputs_at(machine, current['head'], machine['repo'] if row.get('unlisted') else path))
            if excluded:
                return dict(current, action='keep', reason_code='ignored_content', excluded=excluded)
        rescue = 'refs/drover/disk-rescue/' + uuid.uuid4().hex if not row.get('branch') else None
        target = row['branch'].removeprefix('refs/heads/') if row.get('branch') else row['head']
        args = ['worktree', 'add'] + ([] if row.get('branch') else ['--detach']) + ['--', str(path), target]
        record = dict(v=1, id=uuid.uuid4().hex, phase='planned', ts=request['now'],
                      side=placement.cleanup_side(), repo=machine['repo'], path=str(path),
                      head=row['head'], branch=row.get('branch'), side_lock=row.get('side_lock'),
                      rescue_ref=rescue, restore_argv=placement.git_argv(machine['repo'], args),
                      lock_argv=placement.git_argv(machine['repo'], ['worktree', 'lock', '--reason',
                                                  row['side_lock'], '--', str(path)]) if row.get('side_lock') else None)
        disk_state.append_manifest(request['state_dir'], record)
        if rescue:
            git(machine['repo'], 'update-ref', rescue, row['head'], '')
            if git(machine['repo'], 'rev-parse', rescue).strip() != row['head']:
                raise ValueError('rescue ref verification failed')
        if not safe_path(path) or not placement.cleanup_contains(request, str(path)):
            raise ValueError('worktree path became linked or protected')
        if row.get('unlisted') and unlisted_live(request, path):
            row.update(action='keep', reason_code='live_owner')
            quarantine(request, machine, row)
            disk_state.append_manifest(request['state_dir'], dict(record, phase='failed'))
            return row
        if row.get('side_lock'):
            git(machine['repo'], 'worktree', 'unlock', '--', path)
            unlocked = True
        if row.get('unlisted'):
            import shutil
            shutil.rmtree(path)
        else:
            git(machine['repo'], 'worktree', 'remove', '--force', '--', path)
        row.update(action='removed', manifest=record['id'])
        disk_state.append_manifest(request['state_dir'], dict(record, phase='completed'))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        if unlocked:
            # On removal failure, restore only the marker we just proved and unlocked.
            placement.git(machine['repo'], 'worktree', 'lock', '--reason', row['side_lock'], '--', str(path), timeout=60)
        row.update(action='keep' if row['action'] != 'removed' else 'removed', error=str(error))
        if record:
            try:
                disk_state.append_manifest(request['state_dir'], dict(record, phase='failed'))
            except OSError:
                pass
    return row


def identity(path):
    """Fingerprint activity and identity, without walking links or trusting scan age."""
    import hashlib
    values = []
    def visit(p):
        if placement.cleanup_link(p):
            raise ValueError('linked snapshot')
        info = p.stat()
        values.append((str(p.relative_to(path)), info.st_dev, info.st_ino, info.st_size,
                       info.st_mtime_ns, info.st_ctime_ns))
        if p.is_dir():
            for child in sorted(p.iterdir()):
                visit(child)
    visit(path)
    return hashlib.sha256(repr(values).encode()).hexdigest()


def quarantine(request, machine, row):
    from . import disk_state
    key = repr((placement.cleanup_parts(machine['repo']), placement.cleanup_parts(row['path'])))
    if row.get('unlisted') and unlisted_live(request, Path(row['path'])):
        row.update(action='keep', reason_code='live_owner')
    eligible = row['reason_code'] == 'orphan'
    if eligible:
        excluded = ignored_outside(Path(row['path']), row['ignored'],
                                   outputs_at(machine, row['head'], machine['repo'] if row.get('unlisted') else row['path']))
        if excluded:
            row.update(reason_code='ignored_content', excluded=excluded)
            eligible = False
    fingerprint = (identity(Path(row['path'])) + repr((row.get('head'), row.get('tag'),
                    row.get('branch'), row.get('gitdir'), row.get('side_lock')))) if eligible else None
    def observe(state):
        records = state.setdefault('quarantine', {})
        previous = records.get(key)
        if not eligible:
            records.pop(key, None)
            return False
        if not previous or previous['identity'] != fingerprint:
            records[key] = dict(identity=fingerprint, since=request['now'])
            return False
        return request['now'] - previous['since'] >= 3 * 86400
    if request['apply']:
        if disk_state.update(request['state_dir'], observe):
            row.update(action='candidate', reason_code='quarantine_elapsed', quarantine_identity=fingerprint)
    return row


def snapshot(machine, path):
    """Prove an unlisted directory equals this repo's HEAD, using a disposable index."""
    import os
    import tempfile
    if not safe_path(path) or any(p.name == '.git' for p in path.rglob('*')):
        raise ValueError('independent or broken git metadata')
    if placement.cleanup_processes().busy(str(path)):
        raise ValueError('snapshot busy')
    head = git(machine['repo'], 'rev-parse', '--verify', 'HEAD^{commit}').strip()
    with tempfile.TemporaryDirectory(prefix='disk-index-') as directory:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(directory) / 'index'))
        git(machine['repo'], 'read-tree', head, env=env)
        git(machine['repo'], '--work-tree=' + str(path), 'diff', '--exit-code', head, '--', env=env)
        others = git(machine['repo'], '--work-tree=' + str(path), 'ls-files', '--others', '-z', env=env)
        if others:
            raise ValueError('snapshot has untracked data')
    return head


def unlisted_live(request, path):
    tag = ownership.read(str(path))
    return task_facts(request, path, tag)[0] or bool(
        tag and tag['v'] == 2 and tag['lease_until'] > request['now'])


def unlisted(request, machine, listed):
    out = []
    root = Path(placement.native(machine['worktrees']))
    if not root.name.endswith('.worktrees') or not root.is_dir() or not safe_path(root):
        return out
    registered = {str(Path(placement.native(e['worktree']))) for e in listed}
    for path in root.iterdir():
        if str(path) in registered or not placement.cleanup_contains(request, str(path)):
            continue
        row = item(path, request, 'unknown_folder', repo=machine['repo'], name=machine['name'])
        try:
            if not path.is_dir():
                raise ValueError('not a checkout directory')
            if placement.worktree_side(str(path)) != placement.cleanup_side():
                raise ValueError('snapshot side unavailable')
            if unlisted_live(request, path):
                row.update(reason_code='live_owner')
                quarantine(request, machine, row)
                out.append(row)
                continue
            head = snapshot(machine, path)
            row.update(head=head, branch=None, side_lock=None, unlisted=True, ignored=[],
                       tag=None, reason_code='orphan', **facts(path, request['now'], request=request))
            row = quarantine(request, machine, row)
            if request['apply'] and row['action'] == 'candidate':
                row = remove(request, row)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            row.update(action='keep', reason_code='unavailable', error=str(error))
            quarantine(request, machine, row)
        out.append(row)
    return out


def overlaps_worktrees(request, target, own):
    """Registrations protect descendants even when their pointer or folder is broken."""
    target_parts = placement.cleanup_parts(str(target))
    own_parts = placement.cleanup_parts(str(own))
    for machine in request['repos']:
        for entry in entries(machine['repo']):
            other = placement.cleanup_parts(entry['worktree'])
            if other != own_parts and (other[:len(target_parts)] == target_parts or
                                       target_parts[:len(other)] == other):
                return True
    return False
