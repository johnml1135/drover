"""Owner recording of work landed outside Drover."""
import re
import subprocess

from .. import placement, repos
from . import states
from .db import tx
from .ops_common import _j, event, events, stamp
from .ops_tasks import get_task


def record_external_landing(conn, task_id, sha, base_ref, gate, review, actor='owner'):
    """Record an exact commit already reachable from the task repository's configured base."""
    if actor != 'owner':
        raise PermissionError('only the owner may record an external landing')
    if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-fA-F]{40}', sha):
        raise ValueError('a full 40-character commit SHA is required')
    sha = sha.lower()
    if not all(isinstance(value, str) and value.strip() for value in (base_ref, gate, review)):
        raise ValueError('base ref, gate evidence and review evidence are required')
    with tx(conn):
        task = get_task(conn, task_id)
        if task.get('internal'):
            raise PermissionError('internal director accounting task is immutable')
        if task['status'] == states.LANDED:
            records = [e['data'].get('external_landing') for e in events(conn, task_id=task_id, kind='status')]
            if any(r and (r['sha'], r['base_ref'], r['gate'], r['review']) == (sha, base_ref, gate, review)
                   for r in records):
                return task
            raise states.IllegalTransition('task already landed with different evidence')
        if task['status'] != states.NEEDS_OWNER or not task['write']:
            raise states.IllegalTransition('only implementations awaiting the owner can be recorded')
        prog = repos.load_all().get(task['repo'].lower())
        if prog is None:
            raise ValueError('task repository is not configured')
        branch = repos.default_branch(prog)
        expected = f'{repos.remote_name(prog)}/{branch}' if prog['landing']['push'] else branch
        if base_ref != expected:
            raise ValueError('base ref must be the configured base')
        ref = ('refs/remotes/' if prog['landing']['push'] else 'refs/heads/') + expected
        repo = repos.native(repos.repo_path(prog))
        try:
            commit = placement.git(repo, 'cat-file', '-t', sha, timeout=30)
            base = placement.git(repo, 'rev-parse', '--verify', '--quiet', ref + '^{commit}', timeout=30)
            base_sha = base.stdout.strip()
            if (commit.returncode or commit.stdout.strip() != 'commit' or base.returncode
                    or not re.fullmatch(r'[0-9a-f]{40}', base_sha)):
                raise ValueError('commit or configured base cannot be verified')
            ancestor = placement.git(repo, 'merge-base', '--is-ancestor', sha, base_sha, timeout=30)
        except (OSError, subprocess.SubprocessError) as error:
            raise ValueError('commit reachability cannot be verified') from error
        if ancestor.returncode:
            raise ValueError('commit is not verifiably reachable from the configured base')
        evidence = dict(sha=sha, base_ref=base_ref, base_sha=base_sha, gate=gate, review=review)
        conn.execute('UPDATE tasks SET status=?, blocker=NULL, updated=? WHERE id=?',
                     (states.LANDED, stamp(), task_id))
        event(conn, 'status', actor, {'from': states.NEEDS_OWNER, 'to': states.LANDED,
                                    'reason': 'Owner recorded external landing', 'external_landing': evidence},
              task_id=task_id, epic_id=task['epic_id'])
        from .landing import record_merged
        record_merged(conn, {**task, 'last_sha': sha}, base_sha, _j(evidence))
        return get_task(conn, task_id)
