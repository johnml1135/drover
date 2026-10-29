"""Landing synchronization, queue holds, and recovery."""
import hashlib
import inspect
import json
import re
import time

from . import ci, origin
from .ledger import ops, states, landing as storage
from .ledger.db import tx
from .landing_batch import git, reviewed_tasks, build_tip, _gate, RETRY, KEEP_HOLDING, FETCH_TIMEOUT_S


def _control_guard(conn, epoch=None):
    from .port import DirectPort
    return DirectPort(conn).require_admission(epoch)['epoch']


def _controlled_git(conn, epoch):
    def run(repo, *args, **kwargs):
        if args and args[0] in ('merge', 'update-ref', 'push'):
            _control_guard(conn, epoch)
        return git(repo, *args, **kwargs)
    return run


def local_fast_forward(conn, repo, worktree, branch, tip, receipt, control_epoch=None):
    """Advance local main and its checkout, with a receipt and an explicit ancestry check."""
    control_epoch = _control_guard(conn, control_epoch)
    found = ci.problems(receipt, worktree, branch)
    sha = ci.head(worktree)
    if sha != tip or receipt.get('sha') != sha or not re.fullmatch(r'[0-9a-f]{40}', sha):
        found.append(f'tip {tip[:12]} is not the gated HEAD {sha[:12]}')
    if found:
        raise ci.CIError('refused: ' + '; '.join(found))
    holder = f'landing {sha[:12]}'
    try:
        storage.take_lock(conn, holder)
    except storage.LockHeld as error:
        raise ci.CIError(str(error)) from error
    try:
        ref = f'refs/heads/{branch}'
        old = git(repo, 'rev-parse', ref).stdout.strip()
        if git(repo, 'merge-base', '--is-ancestor', old, sha, check=False).returncode:
            raise ci.CIError('local main moved; rebase and regate')
        try:
            origin.fast_forward(repo, branch, sha, old, run=_controlled_git(conn, control_epoch))
        except origin.MainMoved as e:
            raise ci.CIError('local main moved; rebase and regate') from e
        except origin.DirtyCheckout:
            raise
        except origin.SyncError as e:
            raise ci.CIError(str(e)) from e
    finally:
        storage.drop_lock(conn, holder)


def fast_forward(conn, worktree, remote, branch, tip, receipt):
    """Compatibility entry point: sync, land locally, then push without force."""
    epoch = _control_guard(conn)
    origin.sync(worktree, remote, branch, run=_controlled_git(conn, epoch))
    local_fast_forward(conn, worktree, worktree, branch, tip, receipt, control_epoch=epoch)
    origin.push_main(worktree, remote, branch, guard=lambda: _control_guard(conn, epoch))


# ---- base red ------------------------------------------------------------------------------------------------

def _epic(conn, title):
    for e in ops.list_epics(conn):
        if e['title'] == title:
            return e['id']
    return ops.create_epic(conn, 'drover', title, plan_md='Landing alerts.')['id']


def _failure_signature(receipt):
    if receipt.get('failure_signature'):
        return receipt['failure_signature']
    failures = receipt.get('failures') or receipt.get('log_tail') or receipt.get('exit_code')
    return hashlib.sha256(json.dumps(failures, sort_keys=True).encode()).hexdigest()


def base_status(conn, repo, work, gate_fn, base_sha, result, repo_key, failure=None):
    """Diagnose the failed platform on the base; cache only the same failure context.

    Platform-aware callbacks accept platform/signature keywords. Legacy callers
    still run their full callback, whose receipt certifies all their platforms.
    """
    failure = failure or {}
    platform = failure.get('platform')
    signature = _failure_signature(failure)
    known = storage.base_status(conn, repo_key, base_sha, platform, signature)
    if known:
        return known
    tip, _, _ = build_tip(repo, work, base_sha, [])
    params = inspect.signature(gate_fn).parameters
    contextual = 'platform' in params or any(p.kind == p.VAR_KEYWORD for p in params.values())
    def diagnose(work, tip, tasks):
        if contextual and platform:
            return gate_fn(work, tip, tasks, platform=platform, signature=signature)
        return gate_fn(work, tip, tasks)
    _, red = _gate(diagnose, work, tip, [], result)
    status = 'red' if red else 'green'
    storage.record_base(conn, repo_key, base_sha, status, platform, signature)
    if not contextual and platform:
        storage.record_base(conn, repo_key, base_sha, status)
    return status


def base_red(conn, result, base_sha, tasks, repo_key):
    """Blame no task: hold landing and ask the owner."""
    result['outcome'] = 'base-red'
    result['base_sha'] = base_sha
    from .ledger.waiting import Waiting, BASE, cause_key
    waiting = Waiting(conn, 'landing')
    cause = cause_key('base', repo_key, base_sha)
    waiting.wait(('merge_queue', repo_key), BASE, cause_key(repo_key, base_sha), cause=cause,
                 data={'repo': repo_key, 'sha': base_sha, 'mode': 'blocked'})
    qid = waiting.ask(
        cause, 'epic', _epic(conn, 'Landing'), 'The base branch is red: landing is held',
        f'The full gate fails on the base commit {base_sha[:12]} itself, so no task was blamed and '
        f'{len(tasks)} reviewed task(s) are waiting. Fix the base, then choose Retry landing.',
        options=[{'label': RETRY, 'description': 'gate the base again and resume landing', 'recommended': True},
                 {'label': KEEP_HOLDING, 'description': 'stay held'}], allow_text=False)
    ops.event(conn, 'base-red', 'landing', {'sha': base_sha, 'question': qid, 'tasks': [t['id'] for t in tasks]})


def _dirty_wait(conn, repo, repo_name, error):
    from .ledger.waiting import Waiting, OWNER_ANSWER, cause_key, repo_key
    name = repo_key(repo_name, repo)
    waiting = Waiting(conn, 'landing')
    cause = cause_key('dirty', name)
    waiting.wait(('merge_queue', name), OWNER_ANSWER, cause_key(cause), cause=cause,
                 data={'mode': 'blocked', 'note': str(error)})
    if not any(r['cause'] == cause and r['question_id'] is not None for r in waiting.pending()):
        waiting.ask(cause, 'epic', _epic(conn, 'Landing'), f'Clean the default checkout: {name}', str(error),
                    options=[{'label': RETRY, 'recommended': True}], allow_text=False)
    ops.event(conn, 'landing-dirty', 'landing', {'repo': name, 'reason': str(error)})
    return {'outcome': 'refused', 'error': str(error)}


def sync_main(conn, repo, remote, branch, repo_name=None, control_epoch=None):
    """Sync or return the repo hold, recording an owner alert on fetch failure."""
    from .ledger.waiting import Waiting, OWNER_ANSWER, cause_key, repo_key
    from .launch import AdmissionHeld
    try:
        control_epoch = _control_guard(conn, control_epoch)
    except AdmissionHeld as error:
        return {'outcome': 'held', 'reason': str(error)}
    name = repo_key(repo_name, repo)
    waiting = Waiting(conn, 'landing')
    dirty = cause_key('dirty', name)
    cause = cause_key('origin', name)
    # Check every prerequisite before consuming any approval; claim the admitted probes together.
    with tx(conn):
        for candidate, outcome in ((dirty, 'held'), (cause, 'paused')):
            if waiting.state(candidate) in ('open', 'attempting'):
                if waiting.state(candidate) == 'attempting':
                    waiting.consume_retry(candidate)  # reopen abandoned probes, never replay them
                waiting.wait(('merge_queue', name), OWNER_ANSWER, cause_key(candidate), cause=candidate,
                             data={'mode': 'blocked'})
                return {'outcome': outcome, 'reason': 'owner retry required'}
        dirty_probe = waiting.state(dirty) == 'authorized' and waiting.consume_retry(dirty)
        origin_probe = waiting.state(cause) == 'authorized' and waiting.consume_retry(cause)
    try:
        origin.sync(repo, remote, branch, run=_controlled_git(conn, control_epoch), timeout=FETCH_TIMEOUT_S)
        if dirty_probe:
            sha = git(repo, 'rev-parse', branch, check=True).stdout.strip()
            origin.fast_forward(repo, branch, sha, sha, run=_controlled_git(conn, control_epoch))
    except AdmissionHeld as error:
        return {'outcome': 'held', 'reason': str(error)}
    except origin.DirtyCheckout as e:
        if origin_probe:
            ops.event(conn, 'origin-sync-failed', 'landing', {'repo': name, 'reason': str(e)})
        return _dirty_wait(conn, repo, repo_name, e)
    except origin.FetchFailed as e:
        if dirty_probe:
            _dirty_wait(conn, repo, repo_name, e)
        waiting.wait(('merge_queue', name), OWNER_ANSWER, cause_key(cause), cause=cause,
                     data={'mode': 'blocked', 'note': str(e)})
        if not any(r['cause'] == cause and r['question_id'] is not None for r in waiting.pending()):
            waiting.ask(cause, 'epic', _epic(conn, 'Landing'), f'Origin sync paused: {name}', str(e),
                        options=[{'label': 'Retry origin sync', 'recommended': True}], allow_text=False)
        ops.event(conn, 'origin-sync-failed', 'landing', {'repo': name, 'reason': str(e)})
        return {'outcome': 'paused', 'reason': str(e)}
    except origin.SyncError as e:
        reason = str(e)
        if dirty_probe:
            _dirty_wait(conn, repo, repo_name, e)
        if origin_probe:
            ops.event(conn, 'origin-sync-failed', 'landing', {'repo': name, 'reason': reason})
        for task in reviewed_tasks(conn, repo, repo_name):
            ops.set_status(conn, task['id'], states.NEEDS_OWNER, actor='landing', reason=reason)
        return {'outcome': 'needs-owner', 'reason': reason}
    if dirty_probe:
        ops.event(conn, 'landing-clean', 'landing', {'repo': name})
    ops.event(conn, 'origin-synced', 'landing', {'repo': name})
    sha = git(repo, 'rev-parse', branch, check=True).stdout.strip()
    ops.event(conn, 'base-observed', 'landing', {'repo': name, 'sha': sha})
    return None


def designate_base_repair(conn, repo_name, base_sha, task_id, head):
    """Explicit owner designation, pinned to one base and exact reviewed content."""
    from .ledger.waiting import repo_key
    name = repo_key(repo_name)
    task = ops.get_task(conn, task_id)
    if task['repo'].casefold() != name or not all(re.fullmatch('[0-9a-f]{40}', v) for v in (base_sha, head)):
        raise ValueError('repair must name its repository and exact base/head SHAs')
    with tx(conn):
        ops.set_setting(conn, 'landing_base_repair:' + name,
                        json.dumps(dict(base=base_sha, task=task_id, head=head)))
        ops.event(conn, 'landing.repair-designated', 'owner', dict(base=base_sha, head=head), task_id=task_id)


def _base_repair(conn, name, sha):
    repair = json.loads(ops.get_setting(conn, 'landing_base_repair:' + name, '{}'))
    return repair if repair.get('base') == sha else None


def _block_repair(conn, name, repair, result, reason):
    with tx(conn):
        if not repair.get('blocked'):
            repair['blocked'] = reason
            ops.set_setting(conn, 'landing_base_repair:' + name, json.dumps(repair))
            ops.event(conn, 'landing.repair-blocked', 'landing',
                      dict(reason=reason, base=repair['base'], head=repair['head']), task_id=repair['task'])
    result.update(outcome='repair-blocked', reason=repair['blocked'])
    return result


INFRA_BACKOFF_S = 60


def _recovery_key(name, base, task):
    # Replay SHAs change on each rebuild; reviewed content is the stable identity.
    identity = [name, base, task['id'], task['head']]
    return 'landing_recovery:' + hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def _clear_recovery(conn, keys=(), task_ids=()):
    """Forget recovery state by exact key, or for every key that recorded one of task_ids."""
    task_ids = set(task_ids)
    with tx(conn):
        doomed = set(keys)
        if task_ids:
            for key, value in conn.execute("SELECT key, value FROM settings WHERE key LIKE 'landing_recovery:%'").fetchall():
                if task_ids & set(json.loads(value).get('tasks', [])):
                    doomed.add(key)
        for key in doomed:
            conn.execute('DELETE FROM settings WHERE key=?', (key,))


def _infra_failure(conn, keys, tasks, result, detail):
    from .ledger.waiting import Waiting, OWNER_ANSWER, cause_key
    with tx(conn):
        prior = [json.loads(ops.get_setting(conn, key, '{}')) for key in keys]
        saved = max(prior, key=lambda r: r.get('failures', 0))
        count = saved.get('failures', 0) + 1
        saved.update(failures=count, next_after=time.time() + INFRA_BACKOFF_S, detail=detail,
                     tasks=sorted({*saved.get('tasks', []), *(t['id'] for t in tasks)}))
        result.update(outcome='infra-held' if count >= 2 else 'infra-retry', detail=detail)
        if count >= 2 and not saved.get('question'):
            reason = 'Landing infrastructure recovery exhausted: ' + detail.get('reason', 'infrastructure failure')
            qid = ops.ask_question(conn, 'task', tasks[0]['id'], 'Landing needs a healthy host',
                                   reason + '\n' + detail.get('log_excerpt', ''),
                                   options=[{'label': 'Acknowledged'}], actor='landing')
            saved['question'] = qid
            waiting = Waiting(conn, 'landing')
            cause = cause_key('question', qid)
            for task in tasks:
                waiting.wait(('task', task['id']), OWNER_ANSWER, cause_key(cause), cause=cause,
                             data={'mode': 'needs-owner', 'note': reason})
        for key in keys:
            ops.set_setting(conn, key, json.dumps(saved))


def _queue_stop(conn, waiting, name, sha, base_cause, repair, result):
    """Held result for an unmet merge-queue prerequisite (blocks a repair), else None."""
    from .ledger.waiting import BASE, cause_key
    if not repair and waiting.state(base_cause) in ('open', 'attempting'):
        if waiting.state(base_cause) == 'attempting':
            waiting.consume_retry(base_cause)  # abandoned attempt reopens, without granting I/O
        waiting.wait(('merge_queue', name), BASE, cause_key(name, sha), cause=base_cause,
                     data={'repo': name, 'sha': sha, 'mode': 'blocked'})
        return {**result, 'outcome': 'held'}
    if any(not repair or row['cause'] != base_cause for row in waiting.pending(('merge_queue', name))):
        return (_block_repair(conn, name, repair, result, 'Repair is waiting on another queue prerequisite') if repair
                else {**result, 'outcome': 'held'})


def _recovery_stop(conn, recovery_keys, result):
    """The infra-held or queued result while saved infrastructure recovery is not yet due, else None."""
    recovery = max((json.loads(ops.get_setting(conn, key, '{}')) for key in recovery_keys),
                   key=lambda r: r.get('failures', 0))
    if recovery.get('failures', 0) >= 2:
        if (question := recovery.get('question')) is None or ops.get_question(conn, question)['status'] == 'open':
            return {**result, 'outcome': 'infra-held', 'detail': recovery['detail']}
        _clear_recovery(conn, recovery_keys)  # owner acknowledged: one fresh attempt
    elif recovery:
        from . import resources
        if time.time() < recovery['next_after'] or resources.check(new_workers=1)['verdict'] != 'go':
            return {**result, 'outcome': 'queued', 'detail': recovery['detail']}
