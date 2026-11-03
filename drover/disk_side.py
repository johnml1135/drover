"""One parent-authorized stage request. No ledger or coordinator lock here."""
import copy
import json
import sys

from . import placement, process
from .disk_paths import processes
from .git_worktrees import Listings


def serve(request: dict) -> dict:
    request = copy.deepcopy(request)
    request['paths'], request['scope_errors'] = placement.cleanup_scope(request['drive']['path'], request['paths'])
    from .disk_inventory import Inventory, compact
    request['_inventory'] = Inventory(request['now'], request.get('side', placement.cleanup_side()),
                                      request['drive']['path'], request['state_dir'], request.get('_inventory_id'))
    request['_listings'] = Listings([m['repo'] for m in request['repos']])
    proof = None
    if request['stage'] in (2, 3, 4, 5):
        proof = request['_processes'] = processes(request)     # its diagnostics gathered in the request
    answer = _serve(request)
    if not request['apply'] and request['stage'] < 6:
        request['_inventory'].save()
    answer['peak_intermediate_bytes'] = request['_inventory'].peak_intermediate_bytes
    answer['items'] = compact(answer['items'], request['paths'], request['repos'])
    if proof:
        foreign = getattr(proof, 'out_of_scope', 0)
        if foreign:
            answer['items'].append(dict(action='report', reason_code='process_out_of_scope', count=foreign))
        unreadable = list(request['_process_diagnostics'].values())
        if unreadable:
            answer['items'].append(dict(action='report', reason_code='process_visibility_unavailable',
                                        count=len(unreadable), processes=unreadable))
        answer['items'].extend(dict(action='report', **failure) for failure in request.get('_process_query_failures', []))
    answer['errors'].extend(request['scope_errors'])
    return answer


def _serve(request):
    stage = request['stage']
    if stage == 1:
        argv = placement.trim_argv()
        result = dict(items=placement.cleanup_vhdx_report(), errors=[], reclaimed_bytes=0)
        if request['apply'] and argv:
            done = process.run(process.Command(argv, 120))
            if done.kind != 'exited':
                result['errors'].append(dict(reason_code='trim_unavailable', error=done.detail))
            elif done.code:
                result['errors'].append(dict(reason_code='trim_unavailable', output=done.stderr[-2000:]))
        return result
    if stage == 2:
        from .disk_temp import temporary
        return temporary(request)
    if stage == 3:
        from .disk_paths import caches
        return caches(request)
    if stage == 4:
        from .disk_worktrees import settled
        return settled(request)
    if stage == 5 and request.get('budget'):
        from .disk_budget import purge
        return purge(request)
    if stage in (5, 6):
        from .disk_paths import outputs, report
        return outputs(request) if stage == 5 else report(request)
    return dict(items=[], errors=[], reclaimed_bytes=0)



if __name__ == '__main__':
    json.dump(serve(json.load(sys.stdin)), sys.stdout)
