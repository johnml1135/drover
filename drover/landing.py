"""Landing batches: form, gate the tip, attribute red, land fast-forward only, keep velocity. Software only.

A batch is 1..policy `landing_batch_size` reviewed tasks whose diffs share no file. Their commits are replayed in
a scratch worktree on a fresh base and the full gate runs once on the tip. On red: one owning task is dropped and
the rest regated; ambiguous failures go to one `batch_culprit` decision; below tau (or on fallback) we bisect.
"""
import hashlib
import inspect
import json
import re
import statistics
import subprocess
import time
from pathlib import Path, PureWindowsPath

from . import config, ci, placement, origin
from .ledger import ops, states, landing as storage
from .ledger.db import tx

MIN_SIZE, MAX_SIZE = 1, 5


def __getattr__(name):   # DEFAULT_SIZE stays importable; it follows the active config
    if name == 'DEFAULT_SIZE':
        return default_size()
    raise AttributeError(name)


def default_size():
    """The starting landing batch size (config policy.landing_batch_size, 3); the tuner changes it from there."""
    return config.get()['policy']['landing_batch_size']
BATCHING_OFF_MEDIAN_S = 600          # plan section 12: batching turns off once the median gate is 10 minutes or less
MIN_GATES_FOR_OFF = 5
VERDICT = re.compile(r'^LAND@([0-9a-fA-F]{7,40})$')
RETRY, KEEP_HOLDING = 'Retry landing', 'Keep holding'
PRIORITY_ORDER = ('P0', 'P1', 'P2', 'P3', 'P4')

FETCH_TIMEOUT_S = 120


def git(repo, *args, check=True, timeout=None, **kw):
    try:
        r = placement.git(repo, *args, timeout=timeout, **kw)
    except subprocess.TimeoutExpired:
        r = subprocess.CompletedProcess(args, 124, '', f'git {args[0]} timed out after {timeout}s')
    if check and r.returncode:
        raise ci.CIError(f'git {" ".join(args)}: {r.stderr.strip()}')
    return r


# ---- policy and velocity ---------------------------------------------------------------------------------

def policy_batch_size(conn):
    """landing_batch_size from the current policy version, clamped to 1..5 (default 3)."""
    version = ops.current_policy(conn)
    try:
        n = int(storage.policy_data(conn, version).get('landing_batch_size', default_size()))
    except (ValueError, TypeError):
        n = default_size()
    return max(MIN_SIZE, min(MAX_SIZE, n))


def velocity(conn):
    """Summary of landed batches: gate durations, red rate, bisects, decisions, and whether batching is still worth it."""
    rows = storage.batches(conn)
    gate_s = [s for r in rows for s in json.loads(r['gate_s'])]
    gates = sum(r['gates'] for r in rows)
    red = sum(r['red'] for r in rows)
    landed = [json.loads(r['tasks']) for r in rows if r['outcome'] == 'landed']
    median = statistics.median(gate_s) if gate_s else None
    off = bool(median is not None and len(gate_s) >= MIN_GATES_FOR_OFF and median <= BATCHING_OFF_MEDIAN_S)
    return {'batches': len(rows), 'landed_batches': len(landed), 'tasks_landed': sum(len(t) for t in landed),
            'gates': gates, 'red_gates': red, 'red_rate': (red / gates) if gates else None,
            'median_gate_s': median, 'bisect_gates': sum(r['bisect_gates'] for r in rows),
            'decisions': sum(r['decisions'] for r in rows),
            'mean_batch_size': statistics.mean(len(t) for t in landed) if landed else None,
            'batching_off': off}


def effective_batch_size(conn):
    return MIN_SIZE if velocity(conn)['batching_off'] else policy_batch_size(conn)


# ---- formation ---------------------------------------------------------------------------------------------

def branch_head(repo, branch):
    r = git(repo, 'rev-parse', '--verify', '--quiet', f'refs/heads/{branch}^{{commit}}', check=False)
    return r.stdout.strip() if r.returncode == 0 else None


def review_landed(conn, task_id, head):
    """True when the newest review verdict is `LAND@<sha>` and that sha is `head`, the branch tip being landed.

    A bare LAND, or a LAND for another commit, is not a licence to land: the review must name the exact SHA it read.
    """
    for run in reversed(ops.list_runs(conn, task_id)):
        if run['review_verdict'] is not None:
            m = VERDICT.match(str(run['review_verdict']).strip())
            return bool(m and head and head.lower().startswith(m.group(1).lower()))
    return False


def task_files(repo, base_ref, branch):
    r = git(repo, 'diff', '--name-only', '--no-renames', f'{base_ref}...{branch}', check=False)
    return [p for p in r.stdout.splitlines() if p] if r.returncode == 0 else []


def reviewed_tasks(conn, repo, repo_name=None):
    """Limit unnamed CLI calls to tasks whose recorded checkout belongs to this git repo."""
    tasks = ops.list_tasks(conn, status='reviewed', repo=repo_name)
    if repo_name is not None:
        return tasks

    def common(path):
        r = git(config.native(path), 'rev-parse', '--path-format=absolute', '--git-common-dir', check=False)
        return Path(config.native(r.stdout.strip())).resolve() if r.returncode == 0 else None

    wanted = common(repo)
    return [t for t in tasks if wanted is not None and any(
        common(path) == wanted for path in (t.get('repo_path'), t.get('worktree')) if path)]


def candidates(conn, repo, repo_name=None):
    """Reviewed tasks whose branch tip carries a LAND@sha verdict, best first (priority, then rank): [(task, head)]."""
    found = []
    for t in reviewed_tasks(conn, repo, repo_name):
        head = branch_head(repo, t['branch']) if t['branch'] else None
        if head and review_landed(conn, t['id'], head):
            found.append((t, head))
    return sorted(found, key=lambda th: (PRIORITY_ORDER.index(th[0]['priority']), th[0]['rank'], th[0]['id']))


def form_batch(conn, repo, base_ref, size=None, repo_name=None, *, task_id=None):
    """Up to `size` candidates whose changed files are disjoint: [{id, branch, files, title, head}]."""
    size = size or effective_batch_size(conn)
    chosen, used = [], set()
    for t, head in candidates(conn, repo, repo_name):
        if task_id is not None and t['id'] != task_id:
            continue
        files = task_files(repo, base_ref, t['branch'])
        if not files or used & set(files):
            continue
        chosen.append({'id': t['id'], 'branch': t['branch'], 'files': files, 'title': t['title'], 'head': head})
        used |= set(files)
        if len(chosen) == size:
            break
    return chosen


# ---- the batch tip ----------------------------------------------------------------------------------------

def build_tip(repo, worktree, base_sha, tasks):
    """Replay each task's commits onto base_sha in `worktree`. -> (tip sha, applied tasks with last_sha, conflicted ids)."""
    worktree = Path(worktree)
    checkpoint = worktree.parent / ('.' + worktree.name + '.merge-group.json')
    identity = {'base': base_sha, 'tasks': [[t['id'], t.get('head') or branch_head(repo, t['branch'])] for t in tasks],
                'base_repair': any(t.get('base_repair') for t in tasks)}
    saved = ci.read_result(checkpoint)
    def renew_owner():
        from . import ownership
        try:
            ownership.tag(str(worktree), Path(placement.native(repo)).name,
                          'session:merge-group:' + worktree.name, time.time() + 86400)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            raise ci.CIError(f'created {worktree} but could not record ownership: {error}') from error
    if saved and saved.get('identity') == identity and (worktree / '.git').exists():
        if ci.head(worktree) == saved['tip']:
            renew_owner()
            return saved['tip'], saved['applied'], saved['conflicts']
    if not (worktree / '.git').exists():
        placement.add_worktree(repo, worktree, '-q', '--detach', str(worktree), base_sha, run=git, error=ci.CIError)
    else:
        pointer = (worktree / '.git').read_text(encoding='utf-8').strip().removeprefix('gitdir: ')
        if Path(pointer).is_absolute() or PureWindowsPath(pointer).is_absolute():
            git(repo, 'worktree', 'repair', '--relative-paths', str(worktree))
    renew_owner()
    git(worktree, 'reset', '-q', '--hard')
    git(worktree, 'checkout', '-q', '--detach', base_sha)
    git(worktree, 'clean', '-fdq')
    applied, conflicts = [], []
    for t in tasks:
        before = git(worktree, 'rev-parse', 'HEAD').stdout.strip()
        tip_ref = t.get('head') or t['branch']      # the recorded, reviewed SHA: a branch that moved since is not replayed
        fork = git(repo, 'merge-base', base_sha, tip_ref).stdout.strip()
        commits = git(repo, 'rev-list', '--reverse', '--no-merges', f'{fork}..{tip_ref}').stdout.split()
        ok = True
        for c in commits:
            if git(worktree, 'cherry-pick', '--allow-empty', c, check=False).returncode:
                git(worktree, 'cherry-pick', '--abort', check=False)
                git(worktree, 'reset', '-q', '--hard', before)
                ok = False
                break
        if ok:
            applied.append({**t, 'last_sha': git(worktree, 'rev-parse', 'HEAD').stdout.strip()})
        else:
            conflicts.append(t['id'])
    tip = git(worktree, 'rev-parse', 'HEAD').stdout.strip()
    from .runner import write_json_atomic
    write_json_atomic(checkpoint, dict(identity=identity, tip=tip, applied=applied, conflicts=conflicts))
    return tip, applied, conflicts


# ---- red attribution ---------------------------------------------------------------------------------------

def _recorder(conn):
    def record(r):
        answer = r.get('answer') if r.get('answer') is not None else r.get('shadow_answer')
        ops.record_decision(conn, r['name'], str(r.get('criteria_version')), r.get('model') or 'none',
                            r.get('state_hash') or '', answer, r.get('probabilities'), r.get('confidence'),
                            shadow=not r.get('acted'), rule_answer=r.get('rule_answer'))
    return record


def _path_matches(file, changed):
    file, changed = str(file).replace('\\', '/'), str(changed).replace('\\', '/')
    changed = changed[2:] if changed.startswith('./') else changed
    return file == changed or ((file.startswith('/') or placement.drive(file)) and file.endswith('/' + changed))


def owners_of(failures, tasks):
    """Ids of the tasks whose changed files hold a failing file. Matching is path-component aware, and when an
    absolute path ends with several changed paths only the longest (most specific) one counts."""
    owners = set()
    for f in failures:
        if not f.get('file'):
            continue
        hits = [(len(str(p).replace('\\', '/')), t['id']) for t in tasks for p in t['files'] if _path_matches(f['file'], p)]
        if hits:
            top = max(n for n, _ in hits)
            owners |= {tid for n, tid in hits if n == top}
    return sorted(owners)


def attribute(conn, tasks, failures, decider=None):
    """(culprit id or None, how, decisions asked). how: single | owner | decide | bisect."""
    if len(tasks) == 1:
        return tasks[0]['id'], 'single', 0
    owners = owners_of(failures, tasks)
    if len(owners) == 1:
        return owners[0], 'owner', 0
    if decider is None:
        from . import decide
        decider = decide.decide
    criteria = {t['id']: f"{t['title']}; files: {', '.join(t['files'][:6])}" for t in tasks}
    state = json.dumps({'tasks': [{'id': t['id'], 'title': t['title'], 'files': t['files']} for t in tasks],
                        'failing_owners': owners, 'failures': failures[:10]}, indent=1)
    try:
        r = decider('batch_culprit', state, rule_answer=None, dynamic_criteria=criteria, recorder=_recorder(conn))
    except Exception:
        r = {}
    if r.get('acted') and r.get('answer') in criteria:
        return r['answer'], 'decide', 1
    return None, 'bisect', 1


def bisect(tasks, red_at):
    """The first task whose addition turns the prefix red; red_at(k) gates tasks[:k]. The full batch is known red."""
    lo, hi = 1, len(tasks)
    while lo < hi:
        mid = (lo + hi) // 2
        if red_at(mid):
            hi = mid
        else:
            lo = mid + 1
    return tasks[lo - 1]['id']


def requeue(conn, task_id, failures, why):
    lines = [f"{f['test']}: {f['error']}"[:160] for f in failures[:6]] or ['gate red, output not parsed']
    note = (f'batch gate red ({why}): ' + ' | '.join(lines))[:1400]
    ops.update_task(conn, task_id, actor='system', handoff=note)
    ops.set_status(conn, task_id, 'ready', actor='system', reason=f'batch gate red ({why})',
                   data={'failures': failures[:10]})


# ---- landing ------------------------------------------------------------------------------------------------

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


def _infra_failure(conn, keys, tasks, result, detail):
    from .ledger.waiting import Waiting, OWNER_ANSWER, cause_key
    with tx(conn):
        prior = [json.loads(ops.get_setting(conn, key, '{}')) for key in keys]
        saved = max(prior, key=lambda r: r.get('failures', 0))
        count = saved.get('failures', 0) + 1
        saved.update(failures=count, next_after=time.time() + INFRA_BACKOFF_S, detail=detail)
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


def run_batch(conn, repo, gate_fn, *, workdir, remote='origin', branch='main', size=None, repo_name=None,
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
    base_ref = branch
    sync_result = sync_main(conn, repo, remote, branch, repo_name, control_epoch=result['control_epoch'])
    if sync_result:
        result.update(sync_result)
        return result
    from .ledger.waiting import Waiting, repo_key, cause_key, BASE
    waiting = Waiting(conn, actor)
    name = repo_key(repo_name, repo)
    sha = git(repo, 'rev-parse', branch, check=True).stdout.strip()
    base_cause = cause_key('base', name, sha)
    repair = _base_repair(conn, name, sha)
    if repair and repair.get('blocked'):
        return _block_repair(conn, name, repair, result, repair['blocked'])
    if not repair and waiting.state(base_cause) in ('open', 'attempting'):
        if waiting.state(base_cause) == 'attempting':
            waiting.consume_retry(base_cause)  # abandoned attempt reopens, without granting I/O
        waiting.wait(('merge_queue', name), BASE, cause_key(name, sha), cause=base_cause,
                     data={'repo': name, 'sha': sha, 'mode': 'blocked'})
        return {**result, 'outcome': 'held'}
    pending = waiting.pending(('merge_queue', name))
    if any(not repair or row['cause'] != base_cause for row in pending):
        if repair:
            return _block_repair(conn, name, repair, result, 'Repair is waiting on another queue prerequisite')
        return {**result, 'outcome': 'held'}
    tasks = (form_batch(conn, repo, base_ref, 1, repo_name, task_id=repair['task']) if repair
             else form_batch(conn, repo, base_ref, size, repo_name))
    if repair:
        if len(tasks) != 1 or tasks[0]['head'] != repair['head']:
            return _block_repair(conn, name, repair, result, 'Repair needs a fresh exact-head review')
        tasks = [dict(tasks[0], base_repair=True)]
        result['required_platforms'] = required_platforms or []
    result['batch'] = [t['id'] for t in tasks]
    if not tasks:
        return result
    recovery_keys = [_recovery_key(name, sha, task) for task in tasks]
    recovery = max((json.loads(ops.get_setting(conn, key, '{}')) for key in recovery_keys),
                   key=lambda r: r.get('failures', 0))
    if recovery.get('failures', 0) >= 2:
        return {**result, 'outcome': 'infra-held', 'detail': recovery['detail']}
    if recovery:
        from . import resources
        if time.time() < recovery['next_after'] or resources.check(new_workers=1)['verdict'] != 'go':
            return {**result, 'outcome': 'queued', 'detail': recovery['detail']}
    if waiting.state(base_cause) in ('authorized', 'attempting'):
        if not waiting.consume_retry(base_cause):
            return {**result, 'outcome': 'held'}
        storage.forget_base(conn, name, sha)
    interrupted = False
    work = Path(workdir)
    try:
        _drive(conn, repo, work, gate_fn, tasks, result, base_ref, remote, branch, decider, push, repo_name)
    except ci.CIDeferred as error:
        interrupted = True
        result.update(outcome='queued', reason=str(error))
    except ci.CIInfrastructure as error:
        _infra_failure(conn, recovery_keys, tasks, result, _failure_detail(error.detail))
    except AdmissionHeld as error:
        result.update(outcome='held', reason=str(error))
    except BaseException as e:
        interrupted = isinstance(e, ci.CIInterrupted)
        result['outcome'], result['error'] = 'error', repr(e)
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
    storage.record_batch(conn, result)
    return result


def _failure_detail(receipt):
    excerpt = receipt.get('log_excerpt') or '\n'.join(receipt.get('log_tail') or [])
    return {'reason': receipt.get('reason') or ('CI gate ' + receipt.get('status', 'failed')),
            'run_id': receipt.get('run_id'), 'platform': receipt.get('platform'),
            'log_excerpt': excerpt[-4096:]}


def _gate(gate_fn, work, tip, tasks, result, bisecting=False):
    t0 = time.monotonic()
    red, attempted = True, True
    try:
        receipt = gate_fn(work, tip, tasks)
        if receipt.get('infra') or receipt['status'] == 'infra':
            raise ci.CIInfrastructure(receipt)
        red = receipt['status'] == 'failed'
        if red:
            result['detail'] = _failure_detail(receipt)
        return receipt, red
    except BaseException as error:
        if isinstance(error, ci.CIDeferred):
            red, attempted = False, False
        result['detail'] = _failure_detail(getattr(error, 'detail', {'reason': str(error)}))
        raise
    finally:
        if attempted:
            result['gate_s'].append(round(time.monotonic() - t0, 3))
            result['gates'] += 1
            result['bisect_gates'] += bisecting
            result['red'] += red


def _failures(receipt, tasks):
    return [{'test': 'CI command', 'error': '\n'.join(receipt.get('log_tail') or []),
             'file': None, 'in_diff': False}]


def _drive(conn, repo, work, gate_fn, tasks, result, base_ref, remote, branch, decider, push=False, repo_name=None):
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
        receipt, red = _gate(gate_fn, work, tip, tasks, result)
        if not red:
            if result.get('required_platforms') is not None:
                certified = {r.get('platform') for r in receipt.get('results', [receipt])
                             if not ci.status_problems(r) and r.get('sha') == tip and r.get('kind') == 'full'}
                if not result['required_platforms'] or not set(result['required_platforms']) <= certified:
                    result.update(outcome='refused', error='Repair requires fresh exact-SHA full CI on every platform')
                    return
            _land(conn, repo, work, tasks, tip, receipt, result, remote, branch, push, repo_name)
            if result['outcome'] == 'stale-base' and stale_retries < 2:
                stale_retries += 1
                result.pop('error', None)
                continue
            return
        from .ledger.waiting import repo_key
        if base_status(conn, repo, work, gate_fn, base_sha, result, repo_key(repo_name, repo), receipt) == 'red':
            return base_red(conn, result, base_sha, tasks, repo_key(repo_name, repo))
        failures = _failures(receipt, tasks)
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
        local_fast_forward(conn, repo, work, branch, tip, receipt, control_epoch=epoch)
    except origin.DirtyCheckout as e:
        result.update(_dirty_wait(conn, repo, repo_name, e))
        return
    except ci.CIError as e:
        result['outcome'] = 'stale-base' if 'rebase and regate' in str(e) else 'refused'
        result['error'] = str(e)
        return
    for t in tasks:
        ops.set_status(conn, t['id'], states.LANDED, actor='system', reason='fast-forwarded',
                       data={'land_sha': t['last_sha'], 'tip': tip, 'receipt': receipt.get('log')})
        storage.record_merged(conn, t, tip, receipt.get("log"))
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
