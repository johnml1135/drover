"""One parent-authorized stage request. No ledger or coordinator lock here."""
import json
import subprocess
import sys

from . import placement


def serve(request: dict) -> dict:
    request = placement.cleanup_scope(request)
    from .disk_inventory import Inventory, compact
    request['_inventory'] = Inventory(request['now'], request)
    proof = None
    if request['stage'] in (2, 3, 4, 5):
        proof = request['_processes'] = placement.cleanup_processes()
    answer = _serve(request)
    if not request['apply'] and request['stage'] < 6:
        request['_inventory'].save()
    answer['items'] = compact(answer['items'], request)
    if proof:
        for reason, count, sample in (
                ('process_out_of_scope', getattr(proof, 'out_of_scope', 0), []),
                ('process_visibility_unavailable', len(getattr(proof, 'unreadable', [])), getattr(proof, 'unreadable', [])[:20])):
            if count:
                answer['items'].append(dict(action='report', reason_code=reason, count=count, sample=sample))
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
        return outputs(request) if stage == 5 else report(request)
    return dict(items=[], errors=[], reclaimed_bytes=0)



if __name__ == '__main__':
    json.dump(serve(json.load(sys.stdin)), sys.stdout)
