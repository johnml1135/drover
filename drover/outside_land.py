"""Detect landings made outside Drover: git proves them, so nobody has to record them.

Only commits the ledger ties to the task can prove a landing, and the task's change must still be on the base: a
branch name can be reused, and history cannot see a revert. No failure or unknown state is a proof, and git never
runs inside a ledger transaction. The scan is synchronous, so its time is budgeted and a stuck repo stops early.

What git answers but does not prove, with commits on the base that overlap the task, goes to the decider and then to
the owner in one Inbox question. A model's answer counts only for a commit git then shows on the base with its change
still there and its files among the task's recorded ones, and the owner's answer is checked against the base too. An
answer is consumed once, with what it causes, and is void if the task has changed since it was asked about. When git
cannot answer, nothing is asked and the next interval retries.
"""
import json
import logging
import re
import subprocess
import time

from . import ci, config, decide as decide_module, disk, landing, placement, repos
from .ledger import disk_notices, ops, ops_bypass, scope, states, tx

INTERVAL_S = 600
GIT_TIMEOUT_S = 30
SCAN_BUDGET_S = 60
RESUME_KEY = 'outside_land_resume'
SHA = re.compile('[0-9a-f]{40}')
QUESTION, MAX_CANDIDATES, NOT_LANDED = 'landed_outside', 20, 'not_landed'
SUBJECT_CHARS = 72
DECIDER_FLOOR_S = 5      # less time than this, or than the configured timeout if lower, is not worth a call
KIND, EPIC, ACTOR = 'landing.notice', 'Outside landings', 'detector'
LANDED_AT = re.compile(r'landed at\s+(\S+)', re.IGNORECASE)
log = logging.getLogger(__name__)


class GitUnknown(subprocess.SubprocessError):
    """Git failed rather than answered, so nothing can be concluded and nobody should be asked."""


class _OutOfTime(Exception):
    """A decider call would pass the scan's budget."""


def interval_s(cfg):
    return cfg.get('watch', {}).get('interval_s', INTERVAL_S)


def _out(repo, *args, **kw):
    """Git's stripped stdout, or None when it answers no by exiting 1. Any other exit raises GitUnknown, and a timeout
    raises TimeoutExpired."""
    result = landing.timed_git(repo, *args, timeout=GIT_TIMEOUT_S, **kw)
    if result.returncode not in (0, 1):
        raise GitUnknown(f'git {args[0]} exited {result.returncode}: {result.stderr.strip()[:200]}')
    return None if result.returncode else result.stdout.strip()


def _commit(repo, name):
    """The full SHA `name` resolves to, or None when the repo has no such commit."""
    return _out(repo, 'rev-parse', '--verify', '-q', f'{name}^{{commit}}')


def _count(repo, base, sha, kind):
    return _out(repo, 'rev-list', '--count', kind, f'{base}..{sha}')


def _replayed(repo, base, sha):
    """No commit past the base is a merge, which `git cherry` skips, and the base holds the patch of each.
    The caller has shown `sha` is not on the base, so there is at least one."""
    if _count(repo, base, sha, '--merges') != '0':
        return False
    cherry = _out(repo, 'cherry', base, sha)
    return cherry is not None and not any(line.startswith('+') for line in cherry.splitlines())


def _adds_nothing(repo, base, sha):
    """`sha` changed something, and merging it into the base changes nothing."""
    fork = _out(repo, 'merge-base', base, sha)
    merged = fork and _out(repo, 'merge-tree', '--write-tree', base, sha)    # with no fork, git refuses the merge
    if not merged:
        return False
    fork_tree, sha_tree, base_tree = (_out(repo, 'rev-parse', '--verify', f'{commit}^{{tree}}') for commit in (fork, sha, base))
    return bool(base_tree) and fork_tree != sha_tree and merged == base_tree


def _survives(repo, base, start, sha):
    """The task changed something, and merging `sha` into the base changes nothing from any point the task passed
    through, so all of it is still there. One span cannot show this: a run that only restores what `start[0]`, the first
    run's base, held is in no diff from it. Each point merges to `sha`, not the next commit, because a run may remove
    what a later run restores. This compares trees: a conflict or a later edit of the same lines is a missed proof,
    never a false one."""
    first, *later = [start] if isinstance(start, str) else start or [None]
    if not first or not all(later):
        return False
    first_tree, sha_tree, base_tree = (_out(repo, 'rev-parse', '--verify', f'{commit}^{{tree}}') for commit in (first, sha, base))
    if not base_tree or first_tree == sha_tree:
        return False
    return all(_out(repo, 'merge-tree', '--write-tree', f'--merge-base={point}', base, sha) == base_tree
               for point in dict.fromkeys(point for point in (first, *later) if point == first or point != sha))


def _points(start):
    """`start` as a list: the one base a caller named, or what `candidates` gives."""
    return [start] if isinstance(start, str) else list(start or ())


def _proof(repo, branch, base_ref, commits, start):
    if not commits or not _commit(repo, commits[-1]):
        return None           # a pruned commit is a real no, though the checks below would fail on it
    recorded = commits[-1]    # an earlier attempt on the base says nothing about the task's final output
    start = [point and _commit(repo, point) for point in _points(start)]    # a pruned point leaves None: no proof
    tip = _out(repo, 'rev-parse', '--verify', '-q', f'refs/heads/{branch}^{{commit}}')
    base = _out(repo, 'rev-parse', '--verify', '-q', f'{base_ref}^{{commit}}')
    if not tip or not base:
        return None
    own = _count(repo, base, tip, '--no-merges')
    if own is None or (own != '0' and tip != recorded):
        return None           # the branch holds work the ledger never saw
    if _out(repo, 'merge-base', '--is-ancestor', recorded, base) is not None:
        proof = {'sha': recorded, 'proof': 'ancestor'}
    elif _replayed(repo, base, recorded):
        proof = {'sha': base, 'proof': 'patch-equivalent'}
    elif _adds_nothing(repo, base, recorded):
        proof = {'sha': base, 'proof': 'adds-nothing'}
    else:
        return None
    return proof if _survives(repo, base, start, recorded) else None


def git_proof(repo, branch, base_ref, commits=(), start=None):
    """{'sha', 'proof'} when git proves the task's newest recorded commit landed on `base_ref` and is still there.

    `commits` are the commits the ledger recorded for the task, and `start` the base of its first worker run, or
    the list `candidates` gives: that base, then the task's earlier commits and the base of the run that made the newest.
    `sha` is always on the base: the recorded commit for 'ancestor', the base itself for the other proofs, whose
    recorded commit never reached it. A timeout or a git failure is unknown, so it gives None too; `_proof` raises."""
    try:
        return _proof(repo, branch, base_ref, commits, start)
    except (OSError, ValueError, subprocess.SubprocessError, ci.CIError):
        return None


def _evidence(conn, task_id):
    done = [e['data'] for e in ops.events(conn, task_id=task_id, kind='worker-done')
            if SHA.fullmatch(e['data'].get('sha') or '')]
    bases = {e['data'].get('run'): (e['data'].get('output_snapshot') or {}).get('base')
             for e in ops.events(conn, task_id=task_id, kind='run-launching')}
    first = next((e['data'].get('run') for e in ops.events(conn, task_id=task_id, kind='run-started')
                  if e['data'].get('role') == 'worker'), None)
    start, commits = bases.get(first), [d['sha'] for d in done]
    points = [start, *commits[:-1], bases.get(done[-1].get('run'))] if done else [start]
    return {'commits': commits, 'start': points if SHA.fullmatch(start or '') else None}


def candidates(conn):
    """Write tasks with a branch that await an answer and have no live run, with their recorded commits and `start`."""
    found = []
    for status in (states.YANKED, states.NEEDS_OWNER):
        for task in ops.list_tasks(conn, status=status):
            if task['write'] and task['branch'] and not task.get('internal') and not any(
                    run['ended'] is None for run in ops.list_runs(conn, task['id'])):
                found.append({**task, **_evidence(conn, task['id'])})
    return found


def _log(repo, *args):
    """(sha, subject, changed files) of each commit `git log` selects, newest first."""
    found = []
    for entry in (_out(repo, 'log', '--format=%x00%H %s', '--name-only', *args) or '').split('\0')[1:]:
        head, *files = entry.strip().split('\n')    # only newlines: a subject can hold other line separators
        sha, _, subject = head.partition(' ')
        found.append((sha, subject, [name for name in files if name]))
    return found


def _fork(repo, task, base_ref):
    """Where the task left the base: from its first run's start, or else from its branch or recorded commits, since a
    branch that reached the base has moved its own merge base past the task's work."""
    first = next(iter(_points(task.get('start'))), None)
    for name in filter(None, (first, f"refs/heads/{task['branch']}", *reversed(task.get('commits') or ()))):
        if _commit(repo, name):
            fork = _out(repo, 'merge-base', base_ref, name)
            if fork:
                return fork
    return None


def _words(text):
    return {word for word in re.findall('[a-z0-9]+', text.lower()) if len(word) > 3}


def _subject_overlaps(task, subject):
    """The subject names the task or its branch, or shares two of the title's words, or all of a shorter title."""
    if any(re.search(rf'(?<![\w/-]){re.escape(name)}(?![\w/-])', subject) for name in (task['id'], task['branch'])):
        return True
    title = _words(task['title'])
    return bool(title) and len(title & _words(subject)) >= min(2, len(title))


def ambiguous(repo, task, base_ref):
    """Up to MAX_CANDIDATES commits on the base since the task left it, newest first, whose subject or changed files
    overlap the task. Empty means nothing to ask."""
    fork = _fork(repo, task, base_ref)
    if not fork:
        return []
    return [sha for sha, subject, files in _log(repo, f'{fork}..{base_ref}')
            if scope.any_overlap(task['scope_files'] or [], files) or _subject_overlaps(task, subject)][:MAX_CANDIDATES]


def _described(repo, shas):
    """Each commit's subject and changed files."""
    return {sha: (subject, files) for sha, subject, files in (_log(repo, '--no-walk=unsorted', *shas) if shas else [])}


def _line(commit):
    subject, files = commit
    return f"{subject}; files: {', '.join(files[:6])}"


def _plain(subject):
    """A commit subject as one short line of plain text: it is someone else's text, shown to the owner to decide on."""
    text = ' '.join(''.join(c if c.isprintable() else ' ' for c in subject).split())
    return text if len(text) <= SUBJECT_CHARS else text[:SUBJECT_CHARS - 3] + '...'


def _refusal(repo, sha, ref, name):
    """Why `sha` cannot be taken as a commit on the base `ref`, or None when git shows it there."""
    if not SHA.fullmatch(sha):
        return f'{sha} is not a full commit SHA'
    if not _commit(repo, sha):
        return f'{sha} is not a commit in this repository'
    if _out(repo, 'merge-base', '--is-ancestor', sha, ref) is None:
        return f'{sha} is not on {name}'
    return None


def _changed(repo, old, new):
    return set(filter(None, (_out(repo, 'diff', '--name-only', '-z', '--no-renames', old, new) or '').split('\0')))


def _recorded_files(repo, task):
    """The files the task's newest recorded commit changed since the latest point before it that the repo still has,
    the base of the run that made it if that is known. None recorded means none known."""
    recorded = task['commits'][-1] if task['commits'] else None
    if not recorded or not _commit(repo, recorded):
        return set()
    for point in (*reversed(_points(task['start'])), f'{recorded}^'):
        start = point and _commit(repo, point)
        if start:
            return _changed(repo, start, recorded)
    return set()


def _holds(repo, task, sha, ref, name):
    """A cited commit is on the base, its own change is still there, so one reverted since cannot land a task, and it
    touches a file the task's recorded work changed, so a persuasive subject alone cannot land one either."""
    if _refusal(repo, sha, ref, name):
        return False
    parent = _commit(repo, f'{sha}^')
    return _survives(repo, ref, parent, sha) and bool(_changed(repo, parent, sha) & _recorded_files(repo, task))


def _real_decide(*args, **kw):
    return decide_module.decide(*args, recorder=lambda result: None, **kw)    # _ask_decider records into the ledger


def _ask_decider(conn, task, commits, decide, wait):
    """The commit a live answer cites, if it is one of the candidates. No transaction is open across the call."""
    lines = {sha: _line(commit) for sha, commit in commits.items()}
    state = json.dumps({'task': {key: task.get(key) for key in ('title', 'goal', 'acceptance', 'scope_files', 'branch')},
                        'recorded_commits': task['commits'], 'candidate_commits': lines}, indent=1)
    criteria = {**{sha: f"This commit carries all of the task's accepted work: {line}" for sha, line in lines.items()},
                NOT_LANDED: "No one commit listed carries all of the task's accepted work, or it cannot be told."}
    result = decide(QUESTION, state, rule_answer=NOT_LANDED, dynamic_criteria=criteria, timeout=wait)
    landing._recorder(conn)(result)
    return result.get('answer') if result.get('acted') and result.get('answer') in commits else None


def _ask_owner(conn, ident, task, commits, name, mark, why=''):
    """One Inbox question, tied to the task's state by `mark`. It sits on the epic: an answer on the task itself would
    requeue a needs-owner task. The Inbox shows only the body and the labels, so the body names the commits."""
    options = [*(dict(label=f'Landed at {sha}', description=_line(commit)) for sha, commit in commits.items()),
               dict(label='Not landed', description='It stays where it is; a yanked task is abandoned.')]
    listed = '; '.join(f'{sha[:12]} {_plain(subject)}' for sha, (subject, _) in commits.items())
    body = f"{task['title']}\n\nGit cannot prove this task landed on {name}. Free text may say \"Landed at <full SHA>\"."
    body += f'\n\nCommits that overlap it: {listed}' if listed else ''
    disk_notices.publish(conn, dict(id=ident, task=task['id'], mark=mark, title=f"Did {task['id']} land outside Drover?",
                                    body=f'{why}\n\n{body}' if why else body, options=options, allow_text=True),
                         kind=KIND, epic_title=EPIC, actor=ACTOR)


def _take_answer(conn, task, repo, name, ref, notice):
    """Act once on the owner's answer, if there is one. `take_answer` commits with the action, and only then does the
    action run, so an answer is never read twice and never outlives the state it was given about."""
    tid, question, mark = task['id'], notice['question'], notice['notice']['mark']
    asked = ops.get_question(conn, question)
    if asked['status'] == 'open' or ops.answer_taken(conn, tid, question):
        return False
    reply = (asked['answer_choice'] or asked['answer_text'] or '').strip()
    landed = LANDED_AT.fullmatch(reply)
    moved = ops.status_mark(conn, tid) != mark
    if moved or not landed:
        yanked = task['status'] == states.YANKED
        commits = _described(repo, ambiguous(repo, task, ref)) if moved and yanked else {}
        with tx(conn):
            not_landed = reply.lower() == 'not landed'
            if ops.take_answer(conn, tid, question, mark, 'not-landed' if not_landed else 'no-action'):
                if not_landed and yanked:
                    ops_bypass.abandon(conn, tid, actor='owner')
            elif commits:         # one new question per void answer: its id holds it to one across ticks
                _ask_owner(conn, f'{tid}#{question}', task, commits, name, ops.status_mark(conn, tid),
                           why='An earlier answer was not used: the task changed after this was asked.')
        return False
    sha = landed[1].lower()
    why = _refusal(repo, sha, ref, name)
    if why:
        commits = _described(repo, ambiguous(repo, task, ref))
        with tx(conn):
            if ops.take_answer(conn, tid, question, mark, 'refused'):
                _ask_owner(conn, f'{tid}#{question}', task, commits, name, mark,
                           why=f'That answer was not recorded: {why}.')
        return False
    done = ops.record_answered_landing(conn, tid, sha, name, actor='owner',
                                       guard=lambda: ops.take_answer(conn, tid, question, mark, 'landed'))
    return done['status'] == states.LANDED


def _unproved(conn, task, prog, decide, room):
    """Git answered, with no proof. True if the task lands all the same. One question per task, for ever, unless the
    owner's answer is refused."""
    name, ref, repo = *_base(prog), repos.native(repos.repo_path(prog))
    if not _commit(repo, ref):
        raise GitUnknown(f'{ref} does not resolve')
    notices = [e['data'] for e in ops.events(conn, kind=KIND) if e['data']['notice']['task'] == task['id']]
    if notices:
        return _take_answer(conn, task, repo, name, ref, notices[-1])
    shas = ambiguous(repo, task, ref)
    if not shas:
        return False
    commits, mark, wait = _described(repo, shas), ops.status_mark(conn, task['id']), room()
    if not wait:
        raise _OutOfTime
    cited = _ask_decider(conn, task, commits, decide, wait)
    if ops.status_mark(conn, task['id']) != mark:
        return False          # the task moved while the model thought
    if cited and _holds(repo, task, cited, ref, name):
        ops.record_detected_landing(conn, task['id'], cited, name, 'decider')
        return True
    _ask_owner(conn, task['id'], task, commits, name, mark)
    return False


def _close_settled(conn):
    """Close the open questions of tasks that have landed or been cancelled, however that came about, and of tasks that
    have moved since they were asked about, whose answer would be void."""
    for data in (e['data'] for e in ops.events(conn, kind=KIND)):
        notice = data['notice']
        if ops.get_question(conn, data['question'])['status'] == 'open':
            status = ops.get_task(conn, notice['task'])['status']
            if status in states.TERMINAL or ops.status_mark(conn, notice['task']) != notice['mark']:
                disk_notices.retract(conn, notice['id'], kind=KIND, actor=ACTOR, choice=None,
                                     text=f'Closed by Drover: the task is {status} now.')


def _base(prog):
    """The base as the ledger names it and the full ref git reads, as record_external_landing resolves them."""
    branch = repos.default_branch(prog)
    if prog['landing']['push']:
        name = f'{repos.remote_name(prog)}/{branch}'
        return name, f'refs/remotes/{name}'
    return branch, f'refs/heads/{branch}'


def _wsl_up():
    """Whether WSL is running; asking starts nothing, and a stopped WSL is never booted for a scan."""
    try:
        return placement.wsl_running(disk.tracker_distro(config.get()))
    except (OSError, subprocess.SubprocessError):
        return False


def _in_turn(conn, tasks):
    """The candidates, starting with the one the last scan ran out of time before."""
    ids = [task['id'] for task in tasks]
    resume = ops.get_setting(conn, RESUME_KEY, '')
    return tasks[ids.index(resume):] + tasks[:ids.index(resume)] if resume in ids else tasks


def _detect(conn, task, prog):
    name, ref = _base(prog)
    proof = _proof(repos.native(repos.repo_path(prog)), task['branch'], ref, task['commits'], task['start'])
    if proof:
        ops.record_detected_landing(conn, task['id'], proof['sha'], name, proof['proof'])
    return bool(proof)


def tick(conn, cfg, *, decide=None, now=time.monotonic):
    """Scan the candidates once. Proofs are found with no transaction open; each landing is one short transaction.

    A repo stops at its first timeout. The whole scan, decider calls included, stops at SCAN_BUDGET_S, and a decider
    call gets only the time left, or waits if too little is. Either way the scan resumes there at the next interval."""
    tasks, landed, stuck, up, scanned, started = _in_turn(conn, candidates(conn)), [], set(), None, 0, now()
    programs, resume, decide = repos.load_all() if tasks else {}, '', decide or _real_decide
    margin = cfg.get('decider', {}).get('timeout_s', decide_module.TIMEOUT)
    def room():
        wait = min(margin, SCAN_BUDGET_S - (now() - started))
        return wait if wait >= min(margin, DECIDER_FLOOR_S) else 0
    for task in tasks:
        if now() - started >= SCAN_BUDGET_S:
            resume = task['id']
            log.warning('outside landing scan ran out of time; resuming at %s', resume)
            break
        key = task['repo'].lower()
        prog = programs.get(key)
        if prog is None or key in stuck:
            continue
        try:
            if placement.routes_through_wsl(repos.repo_path(prog)):
                up = _wsl_up() if up is None else up
                if not up:
                    continue
            scanned += 1
            if _detect(conn, task, prog) or _unproved(conn, task, prog, decide, room):
                landed.append(task['id'])
        except _OutOfTime:
            resume = task['id']
            log.warning('outside landing scan stops before the decider; resuming at %s', resume)
            break
        except subprocess.TimeoutExpired:
            stuck.add(key)
            log.warning('outside landing scan skips %s: git timed out', key)
        except (OSError, LookupError, ValueError, PermissionError, subprocess.SubprocessError, ci.CIError) as error:
            log.warning('landing of %s not detected: %s', task['id'], error)     # IllegalTransition is a ValueError
    if ops.get_setting(conn, RESUME_KEY, '') != resume:
        ops.set_setting(conn, RESUME_KEY, resume)
    _close_settled(conn)
    return {'scanned': scanned, 'landed': landed}
