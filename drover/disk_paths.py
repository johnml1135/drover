"""Safe declared path stages. No inferred tool, repository or language policy."""
import subprocess
from functools import partial
from pathlib import Path

from . import placement, process
from .disk_inventory import repository_root, safe_path, worktree_roots
from .disk_stage import item, within

OUTPUT_IDLE_S = 2 * 3600  # disk_cleanup.worktree_output_idle_s default


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


def live_tree(path, evidence):
    """Fresh live paths protect both containing trees and their descendants. `evidence` is the stage's."""
    snapshot = evidence.fresh_snapshot() or {}      # a stale snapshot makes no path live
    owners = list(snapshot.get('active_worktrees', []))
    owners += [t['worktree'] for t in snapshot.get('tasks', []) if t.get('worktree')]
    parts = placement.cleanup_parts(path)
    for owner in owners:
        other = placement.cleanup_parts(owner)
        if (parts[:len(other)] == other or other[:len(parts)] == parts) and \
                evidence.task_facts(Path(placement.native(owner)), None)[0]:
            return True
    return False


def protected_path(path, request, evidence):
    path = Path(path).absolute()
    roots = [Path(request['state_dir']).absolute()]
    roots += [Path(placement.native(repo['repo'])).absolute() for repo in request['repos'] if repo.get('repo')]
    roots += [root.absolute() for repo in request['repos'] for root in worktree_roots(repo)]
    return not placement.cleanup_contains(request['drive']['path'], str(path)) or live_tree(path, evidence) or not safe_path(path) or any(within(path, root) or within(root, path) for root in roots) or \
        repository_tree(path)


def caches(run) -> None:
    """Stage 3 on `run`: literal machine argv, cwd and inherited environment, with bounded execution. Past the clock's
    line it stops with the one `cache_deadline` row before the next entry or removal starts."""
    request = run.request.decide_view()
    for entry in request['paths'].get('caches', []):
        if run.clock.late():
            return run.stop('cache_deadline', None, [], [])
        if 'path' in entry:
            if folder_cache(run, request, entry):
                return
            continue
        row = item(entry['cwd'], 'declared_cache')
        row.update(argv=entry['argv'], action='candidate')
        if request['apply']:
            if not run.clock.can_mutate():      # it would not end in time: it never starts, and the stage stops
                run.add(dict(row, action='keep', reason_code='removal_deadline'))
                return run.stop('cache_deadline', None, [], [])
            done = process.run(process.Command(entry['argv'], 120, cwd=entry['cwd']))
            if done.kind != 'exited':
                run.error(dict(row, error=done.detail))
            else:
                row.update(action='executed', returncode=done.code, output=(done.stdout + done.stderr)[-4000:])
                if done.code:
                    run.error(dict(row))
        run.add(row)
        run.checkpoint()


def folder_cache(run, request, entry):
    """One declared cache folder, its old leaves removed on apply; True when the clock's line stopped the stage."""
    root = Path(entry['path']).absolute()
    if protected_path(root, request, run.evidence):
        run.add(item(root, 'protected'))
        return False
    if not root.is_dir() or not safe_path(root):
        return False
    aggressive = request.get('mode') != 'daily' and request['drive'].get('level') in ('warn', 'queue', 'hold', 'critical')
    try:
        candidates = []
        def leaves(parent):
            for path in parent.iterdir():
                if protected_path(path, request, run.evidence):
                    run.add(item(path, 'protected'))
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
            if run.clock.late():
                run.stop('cache_deadline', None, [], [])
                return True
            if aggressive and request['apply']:
                free = placement.cleanup_free(root)
                if type(free) not in (int, float) or not __import__('math').isfinite(free) or free < 0 or free >= request['drive']['warn_gb']:
                    break
            row = item(path, 'folder_cache')
            row.update(action='candidate', files=1, bytes=info.st_size, age_s=max(0, request['now'] - activity))
            if request['apply']:
                proof = run.evidence.fresh_processes().proof
                if not run.clock.can_mutate():      # the line passed while it was proven: it stays, as in stage 2
                    run.add(dict(row, action='keep', reason_code='removal_deadline'))
                    return run.stop('cache_deadline', None, [], []) or True
                if proof.busy(str(path)):
                    row.update(action='keep', reason_code=getattr(proof, 'reason', None) or 'process_busy')
                elif protected_path(path, request, run.evidence):
                    row.update(action='keep', reason_code='protected')
                elif path.stat() == info and placement.cleanup_contains(request['drive']['path'], str(path)):
                    path.unlink()
                    row['action'] = 'removed'
                    run.reclaimed(info.st_size)
            run.add(row)
            if request['apply']:
                run.checkpoint()        # between removals: this one is done or never started
    except (OSError, ValueError) as error:
        run.error(dict(path=str(root), error=str(error)))
    return False


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


def output_evidence(request, machine, entry, *, evidence, touched=(), fresh=False):
    """A worktree's (path, head, tag, declarations) when its outputs may be judged. `evidence` is the stage's: a first
    look takes its first-pass process proof, and a removal's last look a `fresh` one."""
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
    if not placement.cleanup_contains(request['drive']['path'], entry['worktree']):
        reject('physical_drive', f'path_side={placement.cleanup_path_side(entry["worktree"])!r}, '
               f'host={placement.physical_disk_path(entry["worktree"])!r}, '
               f'drive={request["drive"]["path"]!r}, '
               f'drive_host={placement.physical_disk_path(request["drive"]["path"])!r}')
    if (evidence.fresh_processes().proof if fresh else evidence.processes()).busy(str(path)):
        raise ValueError('process using worktree')
    # This worker cannot see Windows processes; a budget carries what the coordinator found them naming inside WSL.
    if any(within(named, path) for named in (request.get('budget') or {}).get('windows_paths') or ()):
        raise ValueError('windows process using worktree')
    # Disposable paths only: whole-worktree removal keeps its own three-day settlement (disk_cleanup_decisions).
    # A drive in trouble waives the idle rule; a budget purge, which is about WSL and not that drive, never does.
    limit = request.get('paths', {}).get('worktree_output_idle_s', OUTPUT_IDLE_S)
    if (request.get('budget') or request['drive'].get('level') not in ('hold', 'critical')) and evidence.measure(path, ignore_activity=touched)['age_s'] < limit:
        raise ValueError(f'worktree idle less than {limit:g} seconds')
    head = evidence.head(path)
    return path, head, evidence.tag(path), evidence.declarations(machine, path)


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


def remove_output(request, run, machine, entry, evidence, touched, target, measured, row):
    """The one removal of declared outputs: `target` is proven again now, as one unit, immediately before its paths
    (`measured`, name -> facts when listed) go, and any change keeps them. Bytes count on `run` as each path goes."""
    from . import disk_evidence     # disk_evidence reaches config, which loads this module
    path = evidence[0]
    try:
        current = run.evidence.row(machine['repo'], entry['worktree'])
        current_dirty, current_ignored = run.evidence.status(path)
        if disk_evidence.registration_identity(current or {}) != disk_evidence.registration_identity(entry):
            raise ValueError('disposable registration changed')
        fresh_evidence = output_evidence(request, machine, current, evidence=run.evidence, touched=touched, fresh=True)
        tracked = {str(path / name) for name in run.evidence.tracked(path, target.relative_to(path))}
        if placement.cleanup_parts(fresh_evidence[0]) != placement.cleanup_parts(evidence[0]) or \
                fresh_evidence[1:] != evidence[1:] or \
                set(measured) - set(current_dirty + current_ignored) or \
                tracked & set(measured) or (tracked and str(target) in measured) or \
                any(run.evidence.measure(Path(name)) != known for name, known in measured.items()):
            raise ValueError('disposable evidence changed')
        proof = run.evidence.fresh_registrations(machine['repo'], target, path, pointer=True)
        if proof.refused:
            first = proof.refused[0]
            return row.update(action='keep', reason_code='registration_unknown' if first.why == 'unknown' else 'output_evidence_unavailable',
                              error=f'disposable registration changed ({first.detail})')
        proven = set()
        if proof.overlaps or \
                any(repository_tree(Path(name), boundary=path, proven=proven) for name in measured) or \
                not placement.cleanup_contains(request['drive']['path'], str(target)) or live_tree(target, run.evidence):
            raise ValueError('disposable side marker changed')
        for name, known in measured.items():
            leaf = Path(name)
            # status expands ignored directories to leaves; only empty directories reach here.
            leaf.rmdir() if leaf.is_dir() else leaf.unlink()
            touched.add(leaf.parent)
            run.reclaimed(known['bytes'])
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


def disposable_rows(run):
    """Yield (row, remove) for each unit of declared ignored/untracked outputs, its row not yet added: the consumer
    adds it once decided, after remove(), the one revalidated removal, if it calls that. Whatever is not eligible is
    added as kept, never yielded. Past the clock's line it stops between units with the one `output_deadline` row."""
    from .disk_evidence import overlaps     # disk_evidence reaches config, which loads this module
    request, stage = run.request.decide_view(), run.evidence
    budget = bool(request.get('budget'))
    for number, machine in enumerate(request['repos']):
        try:
            listed = stage.listed(machine['repo']).rows
            for position, entry in enumerate(listed):
                run.checkpoint()        # between units: the last worktree's removals are done or never started
                if run.clock.late():
                    return run.stop('output_deadline', machine, listed[position:], request['repos'][number + 1:])
                try:
                    evidence = output_evidence(request, machine, entry, evidence=stage)
                    path, head, tag, declarations = evidence
                    idle = dict(idle_s=stage.measure(path)['age_s']) if budget else {}
                    expand = request['apply'] or budget
                    dirty, ignored = stage.status(path, expand=expand)
                    if not expand and declarations and any(
                            Path(name).is_dir() and not disposable_match(Path(name).relative_to(path), declarations)
                            for name in dirty + ignored):
                        dirty, ignored = stage.status(path)
                    tracked = set(stage.tracked(path))
                    touched = set()     # shared by this worktree's removals, in whatever order they run
                    units = output_units(dirty + ignored, path, tracked, declarations, budget)
                    for count, (target, names) in enumerate(units.items(), 1):
                        def row_for(where):
                            return item(where, 'declared_disposable', repo=machine['repo'], root=str(path), **idle)
                        row, measured = row_for(target), {}
                        try:
                            if not placement.cleanup_contains(request['drive']['path'], str(target)):
                                row['reason_code'] = 'drive_unproven'
                            elif live_tree(target, stage):
                                row['reason_code'] = 'live_owner'
                            elif overlaps(target, path, stage.every_registered()):    # a failed repo raises: each unit stays
                                row['reason_code'] = 'protected'
                            else:
                                proven = set()
                                for name in names:
                                    leaf = Path(name)
                                    if safe_path(leaf, proven) and not (repository_tree(leaf, boundary=path, proven=proven) if request['apply'] else stage.inventory.contains_repository(leaf)):
                                        measured[name] = stage.measure(leaf)
                                    elif len(names) > 1:
                                        run.add(dict(row_for(leaf), reason_code='protected'))
                                if measured:
                                    rows = list(measured.values())
                                    row.update(files=sum(r['files'] for r in rows), bytes=sum(r['bytes'] for r in rows),
                                               age_s=min(r['age_s'] for r in rows), owner=tag['owner'] if tag else None,
                                               action='candidate')
                                else:
                                    row['reason_code'] = 'protected'
                        except (OSError, ValueError, StopIteration, subprocess.SubprocessError) as error:
                            row.update(action='keep', reason_code='output_evidence_unavailable', error=str(error))
                        if row['action'] == 'candidate':
                            yield row, partial(remove_output, request, run, machine, entry, evidence, touched, target, measured, row)
                        else:
                            run.add(row)
                        if count < len(units) and run.clock.late():
                            return run.stop('output_deadline', machine, listed[position:], request['repos'][number + 1:])
                except (OSError, ValueError, StopIteration, subprocess.SubprocessError) as error:
                    run.add(item(entry['worktree'], 'output_evidence_unavailable', repo=machine['repo'], error=str(error)))
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            run.add(item(machine['repo'], 'repository_unavailable', error=str(error)))
            run.error(dict(repo=machine['repo'], reason_code='repository_unavailable', error=str(error)))


def outputs(run) -> None:
    """Remove only declared ignored/untracked files; source edits always survive. Each unit's row is added once its
    removal has run, and sent before the next unit starts."""
    for row, remove in disposable_rows(run):
        if run.request.apply:
            remove()
        run.add(row)
        if run.request.apply:
            run.checkpoint()


def report(run) -> None:
    """Stage 6 on `run`: one bounded summary per worktree/folder, each row whole before it is added; this stage never
    mutates. Past the clock's line it stops with the one `report_deadline` row, naming what it did not reach."""
    request, evidence = run.request.decide_view(), run.evidence
    inventory = evidence.inventory
    budgeted = set()
    def measured(path, reason, owner=None, **extra):
        row = item(path, reason)
        row.update(inventory.measure(path), owner=owner, **extra)
        return row
    repos = request['repos']
    for number, machine in enumerate(repos):
        if run.clock.late():
            return run.stop('report_deadline', machine, [], repos[number:])
        try:
            listed = evidence.listed(machine['repo']).rows
            for position, entry in enumerate(listed):
                if run.clock.late():
                    return run.stop('report_deadline', machine, listed[position:], repos[number + 1:])
                path = Path(placement.native(entry['worktree']))
                if not placement.cleanup_contains(request['drive']['path'], entry['worktree']):
                    continue
                tag = evidence.tag(path) if path.exists() else None
                owner = tag['owner'] if tag else None
                this_side = placement.worktree_side(entry['worktree'], entry.get('locked')) == placement.cleanup_side()
                rows = []
                try:        # a later read that fails still lets the rows built before it cross, as today
                    worktree_row = measured(path, 'worktree_inventory', owner)
                    rows.append(worktree_row)
                    if tag and tag['owner'].startswith('drover:') and evidence.task_facts(path, tag)[0] and this_side and \
                            evidence.merged(machine, evidence.head(path)):
                        rows.append(measured(path, 'merged_live_task', owner, task_id=tag['owner'][len('drover:'):],
                                             repo=machine['name']))
                    if machine.get('budget_gb') and placement.cleanup_path(path) not in budgeted and safe_path(path) and this_side:
                        row = measured(path, 'budget_usage', owner)
                        if not row.get('incomplete'):       # Partial inventories cannot establish budget usage.
                            row.update(identity=placement.cleanup_path(path), repo=machine['name'], action='report')
                            budgeted.add(row['identity'])
                            rows.append(row)
                    if this_side:
                        try:
                            dirty, ignored = evidence.status(path, expand=False)
                            if dirty or ignored:
                                row = item(path, 'untracked_or_ignored')
                                row.update(inventory.summarize(dirty + ignored), owner=owner)
                                rows.append(row)
                        except (OSError, ValueError, subprocess.SubprocessError) as error:
                            worktree_row['status_error'] = str(error)
                finally:
                    for row in rows:
                        run.add(row)
                run.checkpoint()
            registered = {str(Path(placement.native(e['worktree']))) for e in listed}
            for root in worktree_roots(machine, this_side=True):
                if root.is_dir() and safe_path(root):
                    for path in root.iterdir():
                        if run.clock.late():
                            return run.stop('report_deadline', machine, [], repos[number + 1:])
                        if str(path) not in registered and placement.cleanup_contains(request['drive']['path'], str(path)):
                            run.add(measured(path, 'unlisted_folder'))
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            run.error(dict(repo=machine['repo'], error=str(error)))
        run.checkpoint()
    for root_text in request['paths'].get('report_roots', []):
        root = Path(root_text)
        if not root.is_dir() or not safe_path(root):
            continue
        try:
            for path in root.iterdir():
                if run.clock.late():
                    return run.stop('report_deadline', None, [], [])
                run.add(measured(path, 'declared_report'))
        except OSError as error:
            run.error(dict(path=str(root), error=str(error)))
        run.checkpoint()
