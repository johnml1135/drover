"""One parent-authorized stage request. No ledger or coordinator lock here."""
import json
import subprocess
import sys

from . import placement


def serve(request: dict) -> dict:
    request = placement.cleanup_scope(request)
    answer = _serve(request)
    answer['errors'].extend(request['scope_errors'])
    return answer


def _serve(request):
    stage = request['stage']
    if stage == 1:
        argv = placement.trim_argv()
        result = dict(items=[], errors=[], reclaimed_bytes=0)
        if request['apply'] and argv:
            try:
                done = subprocess.run(argv, capture_output=True, text=True, timeout=120, shell=False)
                if done.returncode:
                    result['errors'].append(dict(reason_code='trim_unavailable', output=done.stderr[-2000:]))
            except (OSError, subprocess.TimeoutExpired) as error:
                result['errors'].append(dict(reason_code='trim_unavailable', error=str(error)))
        return result
    if stage == 2:
        from .disk_paths import temporary
        return temporary(request)
    if stage == 3:
        from .disk_paths import caches
        return caches(request)
    if stage == 4:
        from .disk_worktrees import settled
        return settled(request)
    if stage in (5, 6):
        from .disk_paths import outputs, report
        answer = outputs(request) if stage == 5 else report(request)
        if stage == 6:
            answer['items'].extend(_budgets(request))
        return answer
    return dict(items=[], errors=[], reclaimed_bytes=0)



def _budgets(request):
    from pathlib import Path
    from .disk_worktrees import entries
    from .disk_paths import safe_path
    answer = []
    seen = set()
    def size(path):
        if placement.cleanup_link(path):
            return 0
        if path.is_dir():
            return sum(size(p) for p in path.iterdir())
        return path.stat().st_size
    for machine in request['repos']:
        if not machine.get('budget_gb'):
            continue
        try:
            for entry in entries(machine['repo']):
                path = Path(placement.native(entry['worktree']))
                if not path.is_dir() or placement.worktree_side(entry['worktree'], entry.get('locked')) != placement.cleanup_side() or not placement.cleanup_contains(request, str(path)):
                    continue
                if not safe_path(path):
                    continue
                identity = placement.cleanup_path(path)
                if identity in seen:
                    continue
                seen.add(identity)
                answer.append(dict(path=str(path), identity=identity, repo=machine['name'],
                                   action='report', reason_code='budget_usage', bytes=size(path)))
        except (OSError, ValueError):
            continue
    return answer

if __name__ == '__main__':
    json.dump(serve(json.load(sys.stdin)), sys.stdout)
