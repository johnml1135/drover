"""One parent-authorized stage request. No ledger or coordinator lock here."""
import json
import subprocess
import sys

from . import placement


def serve(request: dict) -> dict:
    request = placement.cleanup_scope(request)
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
