"""The temporary-folder stage of a cleanup: prove each entry safe within a scan budget, then remove it."""
import shutil
import time
from pathlib import Path

from . import placement
from .disk_paths import live_tree, repository_root, safe_path, worktree_roots
from .disk_stage import item, within


class TempBudgetExceeded(ValueError):
    """A temp root used up its scan budget; whatever it did not reach stays unproven and is kept."""


def temp_facts(path, request, *, budget, cached, children):
    """Prove safety and measure in one pass; stop at the first protected descendant. `budget` is the root's clock;
    `cached` holds each path's facts and `children` each declared root's listing, both read once per stage."""
    roots = [Path(request['state_dir']).absolute()]
    roots += [Path(placement.native(repo['repo'])).absolute() for repo in request['repos'] if repo.get('repo')]
    roots += [root.absolute() for repo in request['repos'] for root in worktree_roots(repo)]
    if not safe_path(path) or live_tree(path, request) or             any(within(path, root) or within(root, path) for root in roots) or             any(repository_root(parent) for parent in path.parents):
        raise ValueError('protected subtree')
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
        if budget.late():
            raise TempBudgetExceeded('temp root scan budget exceeded')
        if placement.cleanup_link(current) or current.name.casefold() in ('.git', '.venv') or                 not placement.cleanup_contains(request['drive']['path'], str(current)) or repository_root(current):
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


def temporary(run) -> None:
    """Stage 2 on `run`: each root scanned within its own budget under the stage clock, and each entry's row added once
    decided. Past the clock's line the stage stops with the one `temp_deadline` row; what it has not reached stays."""
    request, evidence = run.request.decide_view(), run.evidence
    proof = evidence.processes()
    cached, children = {}, {}
    limit = request['paths'].get('temp_root_budget_s', 300)
    for root in sorted({Path(p).absolute() for p in request['paths'].get('temp_roots', [])}, key=lambda p: len(p.parts)):
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
                if run.clock.late():
                    line = True
                    break
                row = item(path, 'temp_retained')
                try:
                    row.update(temp_facts(path, request, budget=budget, cached=cached, children=children))
                    if row['age_s'] >= 7 * 86400:
                        if proof.busy(str(path)):
                            row['reason_code'] = getattr(proof, 'reason', None) or 'process_busy'
                        else:
                            row.update(action='candidate', reason_code='stale_temp')
                            if request['apply']:
                                current = evidence.fresh_processes()
                                if current.busy(str(path)):
                                    row.update(action='keep', reason_code=getattr(current, 'reason', None) or 'process_busy')
                                elif temp_facts(path, request, budget=budget, cached={}, children={}) == {k: row[k] for k in ('files', 'bytes', 'age_s')} and placement.cleanup_contains(request['drive']['path'], str(path)):
                                    shutil.rmtree(path) if path.is_dir() else path.unlink()
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
