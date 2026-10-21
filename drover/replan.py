"""Prepare and complete read-only planner runs with durable, independent request membership."""
from dataclasses import dataclass
from pathlib import Path

from . import brief, contract, placement, repos, task_access
from .pipeline import WorktreeRefused, git
from .ledger import states, scope
from .ledger.waiting import REPLAN

MAX_REPLAN_TASKS = 8


@dataclass
class PreparedBatch:
    run_id: int
    task: dict
    task_ids: tuple
    request_ids: tuple
    assignment: dict
    cwd: str
    read_roots: tuple
    read_checkouts: list
    brief: dict
    members: list


class Replanning:
    def __init__(self, pipeline):
        self.pipeline, self.port = pipeline, pipeline.port

    def _inputs(self, task) -> dict:
        """Inspect source and immutable context without creating a writable worktree."""
        import hashlib
        from . import task_access
        prog = self.pipeline.repo(task)
        if prog is None:
            raise WorktreeRefused('replan repo unavailable')
        configured = self.pipeline.all_repos()
        entries = task.get('read_access') or [repos.repo_name(prog)]
        reads = task_access.prepare(entries, configured=configured)
        checkout = task.get('worktree')
        branch = task.get('branch') or repos.default_branch(prog)
        if not checkout or not placement.reachable(checkout):
            own = task_access.prepare([f"{repos.repo_name(prog)}@{branch}"], configured=configured)
            checkout = own[0]['path']
            reads = own + [r for r in reads if r not in own]
        # prepare() has just verified each read checkout, so they are not verified a second time here.
        def inspect(*args):
            return git(checkout, '--no-optional-locks', *args, check=True, timeout=60).stdout
        # One call: the commit, then the full ref HEAD names ("HEAD" itself when detached).
        head, ref = inspect('rev-parse', 'HEAD', '--symbolic-full-name', 'HEAD').split()
        branch = None if ref == 'HEAD' else ref
        status = inspect('status', '--porcelain=v1', '-z', '--untracked-files=all')
        # ls-files lists both index entries and untracked paths, without following links.
        paths = inspect('ls-files', '-z', '--cached', '--others', '--exclude-standard').split('\0')
        digest = hashlib.sha256()
        digest.update(status.encode('utf-8'))
        digest.update(inspect('ls-files', '--stage', '-z').encode('utf-8'))
        root = Path(repos.native(checkout))
        for name in sorted(set(paths) - {''}):
            path = root / name
            digest.update(name.encode('utf-8'))
            if path.is_symlink():
                digest.update(b'link:' + str(path.readlink()).encode('utf-8'))
            elif path.is_file():
                digest.update(b'file:' + hashlib.sha256(path.read_bytes()).digest())
            else:
                digest.update(b'missing')
        return dict(repo=repos.repo_name(prog), branch=branch, head=head, dirty_digest=digest.hexdigest(),
                    checkout=str(checkout), read_checkouts=reads)

    def _defer(self, request, exc, now):
        self.port.waiting.delay(request['task_id'], 'planner', 'replan-inputs', now, minutes=1)
        self.port.append_event('replan-deferred', {'request': request['id'], 'reason': str(exc)[:300]}, request['task_id'])

    def prepare(self, assign, *, now, inspected=None, allow=None):
        """`inspected` is a per-tick cache of source inspections, keyed by task id."""
        p = self.port
        inspected = {} if inspected is None else inspected
        runs = p.waiting.replan_runs()
        requests = p.waiting.replans()
        candidates = []
        triggers = {e['id']: e['data'] for e in p.events('replan-request')}
        for request in requests:
            task = request['task']
            if allow is not None and not allow(task):
                continue                             # the owner limited dispatch to other repos
            if (request['state'] != contract.REPLAN_QUEUED or request['active_run'] or task['archived']
                    or task['status'] in (*states.ACTIVE, *states.TERMINAL)
                    or any(r['kind'] != REPLAN for r in request['pending']) or p.waiting.replan_busy(task['id'], runs)):
                continue
            if task['id'] in inspected and isinstance(inspected[task['id']], Exception):
                continue                             # already deferred this tick
            try:
                if task['id'] not in inspected:
                    inspected[task['id']] = self._inputs(task)
                inputs = inspected[task['id']]
                assignment = assign(task, inputs)
                if assignment is None:
                    continue
                identity = (str(inputs['repo']).strip().casefold(), *(assignment.get(k) for k in
                            ('backend', 'seat', 'model', 'effort', 'harness', 'placement')))
                item = dict(task=task, inputs=inputs, runs=p.list_runs(task['id']), notes=p.owner_notes(task['id']),
                            trigger=triggers.get(request['requesting_event']))
                candidates.append((request, assignment, identity, item))
            except (OSError, ValueError, WorktreeRefused) as exc:
                inspected[task['id']] = exc
                self._defer(request, exc, now)
        for seed in list(candidates):
            if seed not in candidates:
                continue
            request, assignment, identity, item = seed
            task = request['task']
            def preference(candidate):
                r, _, _, other = candidate
                same_epic = task['epic_id'] is not None and task['epic_id'] == r['task']['epic_id']
                connected = (task['id'] in r['prerequisites'] or r['task_id'] in request['prerequisites']
                             or scope.any_overlap(task['scope_files'], r['task']['scope_files']))
                return (0 if same_epic else 1 if connected else 2, r['order'])
            others = sorted((c for c in candidates if c is not seed and c[2] == identity), key=preference)
            selected = [seed, *others[:MAX_REPLAN_TASKS - 1]]
            while selected:
                ordered = sorted(selected, key=lambda c: c[0]['order'])
                try:
                    built = brief.assemble_replans([c[3] for c in ordered])
                    break
                except brief.BriefTooLarge as exc:
                    if len(selected) == 1:
                        self._defer(request, exc, now)
                    selected.pop()
            if not selected:
                continue
            started = p.waiting.start_replans([dict(request_id=c[0]['id'], inputs=c[3]['inputs'],
                                                   snapshot={**c[0]['inspection'], 'inputs': c[3]['inputs']}) for c in ordered], assignment, now=now)
            if started is None:
                # Enrollment may have moved exhausted members to their owner resolver.
                # Refresh candidate membership so that cause cannot strand eligible peers.
                queued = {r['id'] for r in p.waiting.replans() if r['state'] == contract.REPLAN_QUEUED
                          and not r['active_run'] and not r['task']['archived']
                          and all(w['kind'] == REPLAN for w in r['pending'])}
                candidates[:] = [c for c in candidates if c[0]['id'] in queued]
                continue
            fresh = self._fresh(started)
            if not p.waiting.verify_replans(started['run_id'], fresh, now=now):
                return None
            return self._prepared(started, assignment, built)
        return None

    def _fresh(self, batch):
        fresh = {}
        for member in batch['members']:
            try:
                task_access.verify(member['snapshot']['inputs']['read_checkouts'])
                fresh[member['request_id']] = self._inputs(self.port.get_task(member['task_id']))
            except (OSError, ValueError, WorktreeRefused):
                fresh[member['request_id']] = None
        return fresh

    def _prepared(self, batch, assignment, built):
        members = batch['members']
        checkouts, roots, seen = [], [], set()
        for member in members:
            inputs = member['snapshot']['inputs']
            for checkout in inputs['read_checkouts']:
                key = (checkout['path'], checkout['sha'])
                if key not in seen:
                    seen.add(key)
                    checkouts.append(checkout)
            roots.extend([inputs['checkout'], *[r['path'] for r in inputs['read_checkouts']]])
        anchor = self.port.get_task(batch['run']['task_id'])
        return PreparedBatch(batch['run_id'], anchor, tuple(m['task_id'] for m in members),
                             tuple(m['request_id'] for m in members), assignment,
                             members[0]['snapshot']['inputs']['checkout'], tuple(dict.fromkeys(roots)), checkouts, built, members)

    def restore(self, run_id):
        batch = self.port.waiting.replan_run(run_id)
        if batch is None:
            raise ValueError('unknown planner batch')
        return self._prepared(batch, batch['assignment'], {})

    def finish(self, run_id, terminal, *, now):
        batch = self.port.waiting.replan_run(run_id)
        if batch is None:
            raise ValueError('unknown planner batch')
        task_ids = [m['task_id'] for m in batch['members']]
        records = contract.parse_replan_records(terminal.get('text') or '', task_ids)
        status = terminal['status']
        if terminal.get('provider_error'):
            outcome = contract.PROVIDER_ERROR
        elif status in (contract.TIMEOUT, contract.ERROR, contract.LAUNCH_ERROR, contract.ORPHANED, contract.UNVERIFIED):
            outcome = status
        else:
            outcome = contract.DONE if records else contract.NO_RECORD
        usage = {k: v for k, v in (terminal.get('usage') or {}).items()
                 if k in ('tokens_in', 'tokens_out', 'tokens_cached', 'allowance_used')}
        return self.port.waiting.finish_replans(run_id, outcome, records, usage, fresh=self._fresh(batch), now=now)
