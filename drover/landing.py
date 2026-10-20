"""Landing batches: form, gate the tip, attribute red, land fast-forward only, keep velocity. Software only.

python -m drover.landing --db LEDGER form|run|velocity ...

A batch is 1..policy `landing_batch_size` reviewed tasks whose diffs share no file. Their commits are replayed in
a scratch worktree on a fresh base and the full gate runs once on the tip. On red: one owning task is dropped and
the rest regated; ambiguous failures go to one `batch_culprit` decision; below tau (or on fallback) we bisect.
"""
import argparse
import json
import re
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PureWindowsPath

from . import config, ci, placement, origin
from .port import DirectPort
from .ledger import ops, landing as storage
from .ledger.db import connect, default_path

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
HOLD_KEY, HOLD_QUESTION_KEY = 'landing_hold', 'landing_hold_question'
RETRY, KEEP_HOLDING = 'Retry landing', 'Keep holding'
PRIORITY_ORDER = ('P0', 'P1', 'P2', 'P3', 'P4')

FETCH_TIMEOUT_S = 120


def git(repo, *args, check=True, timeout=None):
    try:
        r = placement.git(repo, *args, timeout=timeout)
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


def form_batch(conn, repo, base_ref, size=None, repo_name=None):
    """Up to `size` candidates whose changed files are disjoint: [{id, branch, files, title, head}]."""
    size = size or effective_batch_size(conn)
    chosen, used = [], set()
    for t, head in candidates(conn, repo, repo_name):
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
    identity = {'base': base_sha, 'tasks': [[t['id'], t.get('head') or branch_head(repo, t['branch'])] for t in tasks]}
    saved = ci.read_result(checkpoint)
    if saved and saved.get('identity') == identity and (worktree / '.git').exists():
        if ci.head(worktree) == saved['tip']:
            return saved['tip'], saved['applied'], saved['conflicts']
    if not (worktree / '.git').exists():
        from .pipeline import add_worktree
        add_worktree(repo, worktree, '-q', '--detach', str(worktree), base_sha, run=git, error=ci.CIError)
    else:
        pointer = (worktree / '.git').read_text(encoding='utf-8').strip().removeprefix('gitdir: ')
        if Path(pointer).is_absolute() or PureWindowsPath(pointer).is_absolute():
            git(repo, 'worktree', 'repair', '--relative-paths', str(worktree))
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

def local_fast_forward(conn, repo, worktree, branch, tip, receipt):
    """Advance local main and its checkout, with a receipt and an explicit ancestry check."""
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
            origin.fast_forward(repo, branch, sha, old, run=git)
        except origin.MainMoved as e:
            raise ci.CIError('local main moved; rebase and regate') from e
        except origin.SyncError as e:
            raise ci.CIError(str(e)) from e
    finally:
        storage.drop_lock(conn, holder)


def fast_forward(conn, worktree, remote, branch, tip, receipt):
    """Compatibility entry point: sync, land locally, then push without force."""
    origin.sync(worktree, remote, branch)
    local_fast_forward(conn, worktree, worktree, branch, tip, receipt)
    origin.push_main(worktree, remote, branch)


# ---- base red ------------------------------------------------------------------------------------------------

def _epic(conn, title):
    for e in ops.list_epics(conn):
        if e['title'] == title:
            return e['id']
    return ops.create_epic(conn, 'drover', title, plan_md='Landing alerts.')['id']


def held(conn, base_sha):
    """True while landing is held for a red base. The hold ends when the base moves or the owner answers Retry."""
    sha = ops.get_setting(conn, HOLD_KEY, '')
    if not sha:
        return False
    if sha != base_sha:
        ops.set_setting(conn, HOLD_KEY, '')
        return False
    qid = ops.get_setting(conn, HOLD_QUESTION_KEY, '')
    if qid:
        q = ops.get_question(conn, int(qid))
        if q['status'] == 'answered' and q['answer_choice'] == RETRY:
            storage.forget_base(conn, base_sha)
            ops.set_setting(conn, HOLD_KEY, '')
            return False
    return True


def base_status(conn, repo, work, gate_fn, base_sha, result):
    """'green' or 'red' for the base commit itself, gated at most once per base SHA."""
    known = storage.base_status(conn, base_sha)
    if known:
        return known
    tip, _, _ = build_tip(repo, work, base_sha, [])
    _, red = _gate(gate_fn, work, tip, [], result)
    status = 'red' if red else 'green'
    storage.record_base(conn, base_sha, status)
    return status


def base_red(conn, result, base_sha, tasks):
    """Blame no task: hold landing and ask the owner."""
    result['outcome'] = 'base-red'
    result['base_sha'] = base_sha
    ops.set_setting(conn, HOLD_KEY, base_sha)
    qid = ops.ask_question(
        conn, 'epic', _epic(conn, 'Landing'), 'The base branch is red: landing is held',
        f'The full gate fails on the base commit {base_sha[:12]} itself, so no task was blamed and '
        f'{len(tasks)} reviewed task(s) are waiting. Fix the base, then choose Retry landing.',
        options=[{'label': RETRY, 'description': 'gate the base again and resume landing', 'recommended': True},
                 {'label': KEEP_HOLDING, 'description': 'stay held'}], actor='landing')
    ops.set_setting(conn, HOLD_QUESTION_KEY, qid)
    ops.event(conn, 'base-red', 'landing', {'sha': base_sha, 'question': qid, 'tasks': [t['id'] for t in tasks]})


def sync_main(conn, repo, remote, branch, repo_name=None):
    """Sync or return the repo hold, recording an owner alert on fetch failure."""
    name = repo_name or str(Path(repo).resolve())
    port = DirectPort(conn, actor='landing')
    if origin.paused(port, name):
        return {'outcome': 'paused', 'reason': 'origin sync paused'}
    try:
        origin.sync(repo, remote, branch, run=git, timeout=FETCH_TIMEOUT_S)
    except origin.FetchFailed as e:
        origin.pause(port, name, 'epic', _epic(conn, 'Landing'), str(e))
        return {'outcome': 'paused', 'reason': str(e)}
    except origin.SyncError as e:
        reason = str(e)
        for task in reviewed_tasks(conn, repo, repo_name):
            ops.set_status(conn, task['id'], 'needs-owner', actor='landing', reason=reason)
        return {'outcome': 'needs-owner', 'reason': reason}
    return None


def run_batch(conn, repo, gate_fn, *, remote='origin', branch='main', size=None, repo_name=None, workdir=None,
              decider=None, actor='landing', push=False):
    """Form, run full CI, attribute, land one batch. Returns a result dict; every task is landed, requeued or untouched.

    CIInterrupted preserves the checkout and checkpoint for CI.resume before the next batch.
    push=False lands on the local branch only: the base is the local branch (so one batch builds on the last) and
    nothing is pushed. Fetch failure pauses the repo; divergent main needs owner intervention."""
    repo = Path(repo)
    result = {'outcome': 'empty', 'landed': [], 'dropped': {}, 'tip': None, 'gates': 0, 'red': 0, 'bisect_gates': 0,
              'decisions': 0, 'gate_s': [], 'batch': []}
    base_ref = branch
    sync_result = sync_main(conn, repo, remote, branch, repo_name)
    if sync_result:
        result.update(sync_result)
        return result
    if held(conn, git(repo, 'rev-parse', base_ref).stdout.strip()):
        return {'outcome': 'held', 'landed': [], 'dropped': {}, 'tip': None, 'gates': 0, 'red': 0, 'bisect_gates': 0,
                'decisions': 0, 'gate_s': [], 'batch': []}
    tasks = form_batch(conn, repo, base_ref, size, repo_name)
    result['batch'] = [t['id'] for t in tasks]
    if not tasks:
        return result
    own_tmp = workdir is None
    interrupted = False
    work = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix='drover-landing-')) / 'wt'
    try:
        _drive(conn, repo, work, gate_fn, tasks, result, base_ref, remote, branch, decider, push, repo_name)
    except BaseException as e:
        interrupted = isinstance(e, ci.CIInterrupted)
        result['outcome'], result['error'] = 'error', repr(e)
        storage.record_batch(conn, result)
        raise
    finally:
        if not interrupted:
            git(repo, 'worktree', 'remove', '--force', str(work), check=False)
            (work.parent / ('.' + work.name + '.merge-group.json')).unlink(missing_ok=True)
            if own_tmp:
                try:
                    work.parent.rmdir()
                except OSError:
                    pass
    storage.record_batch(conn, result)
    return result


def _gate(gate_fn, work, tip, tasks, result, bisecting=False):
    t0 = time.monotonic()
    receipt = gate_fn(work, tip, tasks)
    result['gate_s'].append(round(time.monotonic() - t0, 3))
    result['gates'] += 1
    result['bisect_gates'] += bisecting
    red = receipt['status'] == 'failed'
    result['red'] += red
    return receipt, red


def _failures(receipt, tasks):
    return [{'test': 'CI command', 'error': '\n'.join(receipt.get('log_tail') or []),
             'file': None, 'in_diff': False}]


def _drive(conn, repo, work, gate_fn, tasks, result, base_ref, remote, branch, decider, push=False, repo_name=None):
    stale_retries = 0
    while tasks:
        base_sha = git(repo, 'rev-parse', base_ref).stdout.strip()
        tip, tasks, conflicts = build_tip(repo, work, base_sha, tasks)
        for tid in conflicts:
            result['dropped'][tid] = 'rebase conflict'
            ops.set_status(conn, tid, 'needs-owner', actor='landing', reason='rebase conflict')
        if not tasks:
            result['outcome'] = 'conflict'
            return
        receipt, red = _gate(gate_fn, work, tip, tasks, result)
        if not red:
            _land(conn, repo, work, tasks, tip, receipt, result, remote, branch, push, repo_name)
            if result['outcome'] == 'stale-base' and stale_retries < 2:
                stale_retries += 1
                result.pop('error', None)
                continue
            return
        if base_status(conn, repo, work, gate_fn, base_sha, result) == 'red':
            return base_red(conn, result, base_sha, tasks)
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
    sync_result = sync_main(conn, repo, remote, branch, repo_name)
    if sync_result:
        result.update(sync_result)
        return
    try:
        local_fast_forward(conn, repo, work, branch, tip, receipt)
    except ci.CIError as e:
        result['outcome'] = 'stale-base' if 'rebase and regate' in str(e) else 'refused'
        result['error'] = str(e)
        return
    for t in tasks:
        ops.set_status(conn, t['id'], 'landed', actor='system', reason='fast-forwarded',
                       data={'land_sha': t['last_sha'], 'tip': tip, 'receipt': receipt.get('log')})
        storage.record_merged(conn, t, tip, receipt.get("log"))
        result['landed'].append(t['id'])
    result['outcome'] = 'landed'
    if push:
        try:
            origin.push_main(repo, remote, branch)
        except origin.SyncError as e:
            result['alert'] = str(e)
            ops.ask_question(conn, 'epic', _epic(conn, 'Landing'), 'Push main failed', str(e),
                             options=[{'label': 'Acknowledged'}], actor='landing')


def land_sha(conn, task_id):
    return storage.land_sha(conn, task_id)


# ---- CLI -----------------------------------------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(prog='python -m drover.landing')
    p.add_argument('--db', help='ledger path (default: the hub default)')
    sub = p.add_subparsers(dest='action', required=True)
    for name in ('form', 'run'):
        s = sub.add_parser(name)
        s.add_argument('--repo', required=True)
        s.add_argument('--remote', default='origin')
        s.add_argument('--branch', default='main')
        s.add_argument('--repo-name')
        s.add_argument('--size', type=int)
        s.add_argument('--json', action='store_true')
        if name == 'run':
            s.add_argument('--out', required=True, help='directory for receipts')
    v = sub.add_parser('velocity')
    v.add_argument('--json', action='store_true')
    a = p.parse_args(argv)
    conn = connect(a.db or default_path())
    try:
        if a.action == 'velocity':
            out = velocity(conn)
            print(json.dumps(out) if a.json else '\n'.join(f'{k}: {v}' for k, v in out.items()))
            return 0
        if a.action == 'form':
            origin.sync(a.repo, a.remote, a.branch)
            batch = form_batch(conn, a.repo, a.branch, a.size, a.repo_name)
            print(json.dumps(batch) if a.json else '\n'.join(f"{t['id']}  {t['branch']}  {len(t['files'])} file(s)" for t in batch))
            return 0
        from . import repos
        prog = repos.load({'name': a.repo_name or Path(a.repo).name, 'repo': a.repo})
        service = ci.CI(a.out)
        fn = lambda worktree, commit, tasks: service.run(prog, worktree, commit, base=a.branch, conn=conn)
        r = run_batch(conn, a.repo, fn, remote=a.remote, branch=a.branch, size=a.size, repo_name=a.repo_name)
        print(json.dumps(r) if a.json else f"{r['outcome']}: landed {r['landed']} dropped {r['dropped']} gates {r['gates']}")
        return 0 if r['outcome'] in ('landed', 'empty') else 1
    except (ci.CIError, origin.SyncError) as e:
        print(f'drover.landing: {e}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
