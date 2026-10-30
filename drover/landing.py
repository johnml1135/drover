"""Form and gate merge groups, attribute failures, and land by fast-forward."""
import time
import threading
from pathlib import Path
from . import ci, placement, origin
from .ledger import ops, states, landing as storage
from .landing_batch import (
    MIN_SIZE, MAX_SIZE, default_size, BATCHING_OFF_MEDIAN_S, MIN_GATES_FOR_OFF, VERDICT,
    RETRY, KEEP_HOLDING, PRIORITY_ORDER, FETCH_TIMEOUT_S, git, policy_batch_size, velocity,
    effective_batch_size, branch_head, review_landed, task_files, reviewed_tasks, candidates,
    form_batch, build_tip, _recorder, _path_matches, owners_of, attribute, bisect, requeue,
    _failure_detail, _gate,
)
from .landing_control import (
    _control_guard, _controlled_git, local_fast_forward, fast_forward, _epic, _failure_signature,
    base_status, base_red, _dirty_wait, sync_main, designate_base_repair, _base_repair,
    _block_repair, _recovery_key, _clear_recovery, _infra_failure, _queue_stop, _recovery_stop, INFRA_BACKOFF_S,
)
def __getattr__(name):   # DEFAULT_SIZE stays importable; it follows the active config
    if name == 'DEFAULT_SIZE':
        return default_size()
    raise AttributeError(name)
ORIGIN_OBSERVE_INTERVAL_S = 120
def run_batch(conn, repo, gate_fn, *, observe=None, workdir, remote='origin', branch='main', size=None, repo_name=None,
              decider=None, actor='landing', push=False, control_epoch=None, required_platforms=None):
    """Form, run full CI, attribute, land one batch. Returns a result dict; every task is landed, requeued or untouched.
    The hub supplies a stable per-repo workdir and calls CI.resume before rebuilding it.
    CIInterrupted preserves that checkout and checkpoint for the next service pass.
    push=False lands on the local branch only: the base is the local branch (so one batch builds on the last) and
    nothing is pushed. Fetch failure pauses the repo; divergent main needs owner intervention."""
    repo = Path(repo)
    result = {'outcome': 'empty', 'landed': [], 'dropped': {}, 'tip': None, 'gates': 0, 'red': 0, 'bisect_gates': 0,
              'decisions': 0, 'gate_s': [], 'batch': []}
    from .launch import AdmissionHeld
    try:
        result['control_epoch'] = _control_guard(conn, control_epoch)
    except AdmissionHeld as error:
        return {**result, 'outcome': 'held', 'reason': str(error)}
    if synced := sync_main(conn, repo, remote, branch, repo_name, control_epoch=result['control_epoch']):
        return {**result, **synced}
    from .ledger.waiting import Waiting, repo_key, cause_key
    waiting = Waiting(conn, actor)
    name = repo_key(repo_name, repo)
    sha = git(repo, 'rev-parse', branch, check=True).stdout.strip()
    base_cause = cause_key('base', name, sha)
    repair = _base_repair(conn, name, sha)
    if repair and repair.get('blocked'):
        return _block_repair(conn, name, repair, result, repair['blocked'])
    if stop := _queue_stop(conn, waiting, name, sha, base_cause, repair, result):
        return stop
    tasks = form_batch(conn, repo, branch, 1 if repair else size, repo_name, task_id=repair and repair['task'])
    if repair:
        if len(tasks) != 1 or tasks[0]['head'] != repair['head']:
            return _block_repair(conn, name, repair, result, 'Repair needs a fresh exact-head review')
        tasks = [dict(tasks[0], base_repair=True)]
    result['batch'] = [t['id'] for t in tasks]
    if not tasks:
        return result
    if required_platforms is None:
        from . import repos
        try:
            required_platforms = repos.gate_platforms(repos.load({'name': repo_name or 'landing', 'repo': str(repo)}, sha, repo))
        except (repos.RepoError, OSError, ValueError, KeyError) as error:
            return {**result, 'outcome': 'refused', 'error': f'cannot determine required CI platforms: {error}'}
    result['required_platforms'] = required_platforms
    recovery_keys = [_recovery_key(name, sha, task) for task in tasks]
    if stop := _recovery_stop(conn, recovery_keys, result):
        return stop
    if waiting.state(base_cause) in ('authorized', 'attempting'):
        if not waiting.consume_retry(base_cause):
            return {**result, 'outcome': 'held'}
        storage.forget_base(conn, name, sha)
    interrupted = False
    work = Path(workdir)
    try:
        _drive(conn, repo, work, gate_fn, tasks, result, branch, remote, branch, decider, push, repo_name, observe)
    except ci.CIDeferred as error:
        interrupted = True
        result.update(outcome='queued', reason=str(error))
    except ci.CIInfrastructure as error:
        _infra_failure(conn, recovery_keys, tasks, result, _failure_detail(error.detail))
    except AdmissionHeld as error:
        result.update(outcome='held', reason=str(error))
    except BaseException as e:
        interrupted = isinstance(e, ci.CIInterrupted)
        result['outcome'], result['error'] = ('infra-uncertain' if interrupted else 'error'), repr(e)
        result.setdefault('detail', _failure_detail({'reason': str(e)}))
        storage.record_batch(conn, result)
        raise
    finally:
        waiting.finish_retry(base_cause, completed=result['outcome'] in ('landed', 'infra-retry', 'infra-held'),
                             deferred=result['outcome'] == 'queued')
        if not interrupted:
            from . import ownership
            if ownership.owned_by(str(work), 'session:merge-group:' + work.name):
                placement.unlock_worktree(repo, str(work))
            git(repo, 'worktree', 'remove', '--force', str(work), check=False)
            (work.parent / ('.' + work.name + '.merge-group.json')).unlink(missing_ok=True)
    if repair and result['outcome'] in ('base-red', 'red', 'refused', 'conflict'):
        _block_repair(conn, name, repair, result, 'Designated repair did not pass fresh full CI: ' + result['outcome'])
    _clear_recovery(conn, recovery_keys if result['outcome'] == 'landed' else (), [*result['landed'], *result['dropped']])
    storage.record_batch(conn, result)
    return result
def _drive(conn, repo, work, gate_fn, tasks, result, base_ref, remote, branch, decider, push=False, repo_name=None, observe=None):
    stale_retries = 0
    while tasks:
        _control_guard(conn, result.get('control_epoch'))
        base_sha = git(repo, 'rev-parse', base_ref).stdout.strip()
        tip, tasks, conflicts = build_tip(repo, work, base_sha, tasks)
        for tid in conflicts:
            result['dropped'][tid] = 'rebase conflict'
            ops.set_status(conn, tid, states.NEEDS_OWNER, actor='landing', reason='rebase conflict')
        if not tasks:
            result['outcome'] = 'conflict'
            return
        _control_guard(conn, result.get('control_epoch'))
        stopped, moved = threading.Event(), threading.Event()
        if observe:
            tracking = git(repo, 'rev-parse', '--verify', f'refs/remotes/{remote}/{branch}', check=False).stdout.strip()
            watcher = threading.Thread(target=observe, args=(repo, remote, branch, stopped, moved, tracking,
                                                            ORIGIN_OBSERVE_INTERVAL_S),
                                       daemon=True, name='landing-origin-observer')
            watcher.start()
        try:
            receipt, red = _gate(gate_fn, work, tip, tasks, result)
        finally:
            stopped.set()
            if observe:
                watcher.join(FETCH_TIMEOUT_S + 1)
        if red and observe:
            if moved.is_set():
                result['outcome'] = 'stale-base'
                return
            if synced := sync_main(conn, repo, remote, branch, repo_name, control_epoch=result.get('control_epoch')):
                result.update(synced)
                return
            if git(repo, 'rev-parse', base_ref).stdout.strip() != base_sha:
                result['outcome'] = 'stale-base'
                return
        if not red:
            plan = {'sha': tip, 'kind': 'full', 'platforms': result.get('required_platforms') or []}
            if ci.validate_receipt(receipt, plan):
                result.update(outcome='refused', error='Landing requires exact-SHA full CI on every required platform')
                return
            _land(conn, repo, work, tasks, tip, receipt, result, remote, branch, push, repo_name)
            if result['outcome'] == 'stale-base' and stale_retries < 2:
                stale_retries += 1
                result.pop('error', None)
                continue
            return
        from .ledger.waiting import repo_key
        key = repo_key(repo_name, repo)
        if base_status(conn, repo, work, gate_fn, base_sha, result, key, receipt) == 'red':
            return base_red(conn, result, base_sha, tasks, key)
        failures = [{'test': 'CI command', 'error': '\n'.join(receipt.get('log_tail') or []), 'file': None, 'in_diff': False}]
        culprit, how, asked = attribute(conn, tasks, failures, decider)
        result['decisions'] += asked
        if culprit is None:
            def red_at(k):
                sub_tip, sub, _ = build_tip(repo, work, base_sha, tasks[:k])
                return len(sub) == k and _gate(gate_fn, work, sub_tip, sub, result, True)[1]
            culprit = bisect(tasks, red_at)
            how = 'bisect'
        result['dropped'][culprit] = how
        requeue(conn, culprit, failures, how)
        tasks = [t for t in tasks if t['id'] != culprit]
    result['outcome'] = 'red'
def _land(conn, repo, work, tasks, tip, receipt, result, remote, branch, push=False, repo_name=None):
    result['tip'] = tip
    for t in tasks:
        now_head = branch_head(work, t['branch'])
        if now_head != t['head'] or not review_landed(conn, t['id'], now_head):
            result['outcome'] = 'refused'
            result['error'] = f'{t["id"]} has no review LAND for the gated commit {t["head"][:12]}'
            return
    epoch = _control_guard(conn, result.get('control_epoch'))
    sync_result = sync_main(conn, repo, remote, branch, repo_name, control_epoch=epoch)
    if sync_result:
        result.update(sync_result)
        return
    try:
        local_fast_forward(conn, repo, work, branch, tip, receipt, control_epoch=epoch,
                           required_platforms=result['required_platforms'])
    except origin.DirtyCheckout as e:
        result.update(_dirty_wait(conn, repo, repo_name, e))
        return
    except ci.CIError as e:
        result['outcome'] = 'stale-base' if 'rebase and regate' in str(e) else 'refused'
        result['error'] = str(e)
        return
    for t in tasks:
        from .ledger.ops_stages import land_stage
        land_stage(conn, t, tip, receipt)
        result['landed'].append(t['id'])
    result['outcome'] = 'landed'
    if push:
        from .launch import AdmissionHeld
        try:
            _control_guard(conn, epoch)
            origin.push_main(repo, remote, branch, guard=lambda: _control_guard(conn, epoch))
        except AdmissionHeld as e:
            result['alert'] = str(e) + ': local landing retained; push held'
            ops.event(conn, 'landing.push-held', actor='landing', data=dict(tip=tip, reason=str(e)))
        except origin.SyncError as e:
            result['alert'] = str(e)
            ops.ask_question(conn, 'epic', _epic(conn, 'Landing'), 'Push main failed', str(e),
                             options=[{'label': 'Acknowledged'}], actor='landing')
def land_sha(conn, task_id):
    return storage.land_sha(conn, task_id)
