"""Landing batch policy, formation, replay, attribution, and gate accounting."""
import json
import re
import statistics
import subprocess
import time
from pathlib import Path, PureWindowsPath

from . import config, ci, placement
from .ledger import ops, landing as storage

MIN_SIZE, MAX_SIZE = 1, 5


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
