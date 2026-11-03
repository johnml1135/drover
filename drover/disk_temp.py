"""The temporary-folder stage of a cleanup: external build roots first, each unit idle past its limit removed on a
fresh proof; then the temp roots, each entry proven safe within a scan budget, then removed."""
import shutil
import time
from pathlib import Path

from . import placement
from .disk_authority import LazyProof, rule_build_unit, rule_build_unit_fresh, rule_temp_entry
from .disk_inventory import repository_root, safe_path
from .disk_stage import item, within


class TempBudgetExceeded(ValueError):
    """A temp root used up its scan budget; whatever it did not reach stays unproven and is kept."""


def _builds(request):
    return {Path(entry['path']).absolute() for entry in request['paths'].get('build_roots', [])}


def remove_entry(path):
    """The stage's one removal: a folder's tree, or a file. Never follows a link: rmtree refuses one, and every walk
    before it kept a tree holding one."""
    shutil.rmtree(path) if path.is_dir() else path.unlink()


def temp_facts(path, request, *, budget, cached, children, run):
    """Ask `rule_temp_entry`, then prove the subtree safe and measure it in one pass; stop at the first protected
    descendant. `budget` is the root's clock; `cached` holds each path's facts and `children` each declared root's
    listing, both read once per stage. `run` is the stage's, whose request and evidence the rule reads."""
    ruling = rule_temp_entry(run, path)
    if ruling.action != 'candidate':
        raise ValueError(ruling.detail)     # kept as `protected`, the rule's words its error
    declared = {Path(p).absolute() for p in request['paths'].get('temp_roots', [])}
    builds = _builds(request)       # a build root below an entry is its own rule's: the entry stays whole
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
        if budget.late():
            raise TempBudgetExceeded('temp root scan budget exceeded')
        if placement.cleanup_link(current) or current.name.casefold() in ('.git', '.venv') or current in builds or \
                not placement.cleanup_contains(request['drive']['path'], str(current)) or repository_root(current):
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


def build_facts(path, request, *, clock, device):
    """(files, bytes, age_s) of one build unit, its age that of its newest path, walked without following a link. A
    link, `.git`, `.venv`, repository evidence, or a path off `device`, its build root's, anywhere in it, the unit
    itself included (a mount, which on WSL is no link), raises ValueError, which keeps the unit; past the clock's line
    it raises TempBudgetExceeded."""
    files = size = 0
    newest, pending = None, [path]
    while pending:
        if clock.late():
            raise TempBudgetExceeded('stage line reached')
        current = pending.pop()
        if placement.cleanup_link(current) or current.name.casefold() in ('.git', '.venv'):
            raise ValueError('protected descendant')
        info = current.stat()
        if info.st_dev != device:
            raise ValueError('on another device than its build root')
        newest = info.st_mtime if newest is None else max(newest, info.st_mtime)
        if current.is_dir():
            if repository_root(current):
                raise ValueError('protected descendant')
            pending.extend(current.iterdir())
        else:
            files, size = files + 1, size + info.st_size
    return dict(files=files, bytes=size, age_s=max(0, request['now'] - newest))


def build_roots(run, request) -> bool:
    """Stage 2's external build roots, each immediate subfolder one unit, judged by `rule_build_unit` on the proof for
    regenerable output. On apply a candidate goes after a fresh such proof and a walk that finds it unchanged. True when
    the clock's line stopped the stage, with the one `temp_deadline` row: what it has not reached stays."""
    evidence = run.evidence
    first = LazyProof(lambda: evidence.processes(regenerable=True))      # taken only when a unit reaches it
    for entry in sorted(request['paths'].get('build_roots', []), key=lambda e: e['path']):
        root = Path(entry['path']).absolute()
        if run.clock.late():
            run.stop('temp_deadline', None, [], [])
            return True
        try:
            if not safe_path(root):
                run.add(item(root, 'protected'))
                continue
            if not root.is_dir():       # not made yet: nothing to judge
                continue
            device = root.stat().st_dev     # every path of every unit must be on the root's own device
            units = sorted(p for p in root.iterdir() if placement.cleanup_link(p) or p.is_dir())   # a file is no unit
        except OSError as error:
            run.error(dict(path=str(root), error=str(error)))
            continue
        for path in units:
            if run.clock.late():
                run.stop('temp_deadline', None, [], [])
                return True
            row = item(path, 'build_root_recent')
            try:
                measured = build_facts(path, request, clock=run.clock, device=device)
                row.update(measured)
                ruling = rule_build_unit(run, path, measured, entry=entry, processes=first)
                if ruling.action == 'candidate' and request['apply']:
                    if not run.clock.can_mutate():      # it would not end in time: it never starts, and the stage stops
                        run.add(dict(row, reason_code='removal_deadline'))
                        run.stop('temp_deadline', None, [], [])
                        return True
                    fresh = evidence.fresh_processes(regenerable=True)
                    again = build_facts(path, request, clock=run.clock, device=device)
                    ruling = rule_build_unit_fresh(run, path, measured, again, entry=entry, processes=fresh)
                row.update(action=ruling.action, reason_code=ruling.reason_code,
                           **({'error': ruling.detail} if ruling.detail else {}))
                if ruling.action == 'candidate' and request['apply']:
                    remove_entry(path)
                    row['action'] = 'removed'
                    run.reclaimed(row['bytes'])
            except TempBudgetExceeded:
                run.add(dict(row, action='keep', reason_code='build_root_partial'))
                run.stop('temp_deadline', None, [], [])
                return True
            except (OSError, ValueError) as error:
                row.update(action='keep', reason_code='protected', error=str(error))
            run.add(row)
            if request['apply']:
                run.checkpoint()        # between units: this one's removal is done or never started
    return False


def temporary(run) -> None:
    """Stage 2 on `run`: the build roots, then each temp root scanned within its own budget under the stage clock, and
    each entry's row added once decided. Past the clock's line the stage stops with the one `temp_deadline` row; what
    it has not reached stays. A temp root in a build root, or a temp entry that is one, is the build root's alone."""
    request, evidence = run.request.decide_view(), run.evidence
    proof = evidence.processes()
    if build_roots(run, request):
        return
    builds = _builds(request)
    cached, children = {}, {}
    limit = request['paths'].get('temp_root_budget_s', 300)
    for root in sorted({Path(p).absolute() for p in request['paths'].get('temp_roots', [])}, key=lambda p: len(p.parts)):
        if any(within(root, build) for build in builds):
            continue
        if run.clock.late():
            return run.stop('temp_deadline', None, [], [])
        started = time.monotonic()
        budget = run.clock.within(limit)
        partial = line = False
        try:
            if not root.is_dir() or not safe_path(root):
                continue
            if root not in children:
                children[root] = list(root.iterdir())
            for path in children[root]:
                if path in builds:
                    continue
                if run.clock.late():
                    line = True
                    break
                row = item(path, 'temp_retained')
                try:
                    row.update(temp_facts(path, request, budget=budget, cached=cached, children=children, run=run))
                    if row['age_s'] >= 7 * 86400:
                        if proof.busy(str(path)):
                            row['reason_code'] = getattr(proof, 'reason', None) or 'process_busy'
                        else:
                            row.update(action='candidate', reason_code='stale_temp')
                            if request['apply']:
                                current = evidence.fresh_processes().proof
                                if current.busy(str(path)):
                                    row.update(action='keep', reason_code=getattr(current, 'reason', None) or 'process_busy')
                                elif temp_facts(path, request, budget=budget, cached={}, children={}, run=run) == {k: row[k] for k in ('files', 'bytes', 'age_s')} and placement.cleanup_contains(request['drive']['path'], str(path)):
                                    remove_entry(path)
                                    row['action'] = 'removed'
                                    run.reclaimed(row['bytes'])
                except TempBudgetExceeded:
                    partial, line = True, run.clock.late()      # the root's budget, or the stage's own line
                    row.update(action='keep', reason_code='temp_root_partial')
                except (OSError, ValueError) as error:
                    row.update(action='keep', reason_code='protected', error=str(error))
                run.add(row)
                if request['apply']:
                    run.checkpoint()        # between entries: this one's removal is done or never started
                if partial:
                    break
        except OSError as error:
            run.error(dict(path=str(root), error=str(error)))
        finally:
            if partial:
                run.add(dict(path=str(root), action='report', reason_code='temp_root_partial', budget_s=limit))
            run.add(dict(path=str(root), action='report', reason_code='temp_root_elapsed',
                         elapsed_s=time.monotonic() - started))
        if line:
            return run.stop('temp_deadline', None, [], [])
        run.checkpoint()
