"""Safe declared path stages. No inferred tool, repository or language policy."""
import subprocess
from functools import partial
from pathlib import Path

from . import placement, process
from .disk_authority import DisposableUnit, disposable_match, protected_path, rule_cache_file, rule_disposable, \
    rule_disposable_fresh, rule_unit, rule_unit_fresh
from .disk_evidence import registration_identity
from .disk_inventory import safe_path, worktree_roots
from .disk_stage import item


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
    if protected_path(root, run):
        run.add(item(root, 'protected'))
        return False
    if not root.is_dir() or not safe_path(root):
        return False
    try:
        candidates = []
        def leaves(parent):
            for path in parent.iterdir():
                if protected_path(path, run):
                    run.add(item(path, 'protected'))
                elif path.is_dir():
                    yield from leaves(path)
                elif path.is_file():
                    yield path
        for path in leaves(root):
            info = path.stat()
            ruling = rule_cache_file(run, path, info, entry=entry)
            if ruling.action == 'candidate':
                candidates.append((ruling.facts['activity'], str(path), path, info, ruling.facts['aggressive']))
        for activity, _, path, info, aggressive in sorted(candidates):
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
                elif protected_path(path, run):
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
    path = evidence[0]
    try:
        current = run.evidence.row(machine['repo'], entry['worktree'])
        current_dirty, current_ignored = run.evidence.status(path)
        if registration_identity(current or {}) != registration_identity(entry):
            raise ValueError('disposable registration changed')
        fresh = rule_disposable_fresh(run, machine, current, run.evidence.fresh_processes(), touched=touched)
        if fresh.action != 'candidate':
            return row.update(action='keep', reason_code=fresh.reason_code, error=fresh.detail)
        fresh_evidence = tuple(fresh.facts[key] for key in ('path', 'head', 'tag', 'declarations'))
        tracked = {str(path / name) for name in run.evidence.tracked(path, target.relative_to(path))}
        if placement.cleanup_parts(fresh_evidence[0]) != placement.cleanup_parts(evidence[0]) or \
                fresh_evidence[1:] != evidence[1:] or \
                set(measured) - set(current_dirty + current_ignored) or \
                tracked & set(measured) or (tracked and str(target) in measured) or \
                any(run.evidence.measure(Path(name)) != known for name, known in measured.items()):
            raise ValueError('disposable evidence changed')
        unit = DisposableUnit(machine, entry, evidence, touched, target, measured, row)
        ruling = rule_unit_fresh(run, unit, run.evidence.fresh_registrations(machine['repo'], target, path, pointer=True))
        if ruling.action != 'candidate':
            return row.update(action='keep', reason_code=ruling.reason_code, error=ruling.detail)
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
                    judged = rule_disposable(run, machine, entry)
                    if judged.action != 'candidate':
                        run.add(item(entry['worktree'], judged.reason_code, repo=machine['repo'], error=judged.detail))
                        continue
                    evidence = tuple(judged.facts[key] for key in ('path', 'head', 'tag', 'declarations'))
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
                            ruling = rule_unit(run, target, path, names)
                            for leaf in (ruling.facts or {}).get('protected', ()):
                                run.add(dict(row_for(leaf), reason_code='protected'))
                            if ruling.action == 'candidate':
                                measured = ruling.facts['measured']
                                rows = list(measured.values())
                                row.update(files=sum(r['files'] for r in rows), bytes=sum(r['bytes'] for r in rows),
                                           age_s=min(r['age_s'] for r in rows), owner=tag['owner'] if tag else None,
                                           action='candidate')
                            else:
                                row['reason_code'] = ruling.reason_code
                                if ruling.detail is not None:       # a read failed partway: kept as unknown
                                    row['error'] = ruling.detail
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
