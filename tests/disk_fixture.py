"""Hermetic public-interface fixtures for the disk contract."""
import copy
import io
import os
import tempfile
import time
import unittest
from dataclasses import asdict
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from drover import config, git_worktrees, placement
from drover.disk_stage import StageClock, StageRequest, StageRun, read_rows
from drover.git_worktrees import Listing, Proof
from tests.gitfix import clean_parent

DAY = 86400
GIB = 1024 ** 3


def listing_answers(answer):
    """Patch `git_worktrees.listing`: `answer` is the rows, or a function of the repo that gives them or raises."""
    rows = answer if callable(answer) else lambda repo: answer
    return patch.object(git_worktrees, 'listing', side_effect=lambda repo, **_: Listing(str(repo), tuple(rows(repo))))


def agreeing_proof():
    """Patch `git_worktrees.prove` for a test with no admin files: nothing refuses, and `reach` is the listing's."""
    return patch.object(git_worktrees, 'prove', side_effect=lambda listings, *_, **__: Proof((), listings.reach()))


def stage_request(request):
    """(the dict a stage's helpers read, the stage's evidence), as `stage_run(request)` makes them, for a test that
    calls a removal itself: `remove(view, selected, clock=..., evidence=evidence)`."""
    run = stage_run(request)
    return run.request.decide_view(), run.evidence


def stage_run(request, *, clock=time.monotonic, listings=None):
    """A `StageRun` for today's request dict `request`, as `disk_side.serve` builds one, for a test that calls a stage
    itself: its StageClock reads `clock` and is exempt for an approved removal, and the evidence asks
    `placement.cleanup_processes` when its proof is taken. Rows cross whole, in the order they are added."""
    from drover import disk_evidence
    wire = dict(dict(stage=4, now=0, apply=False, drive=dict(path='/'), repos=[], paths={}, tasks={}), **request)
    stage = StageRequest.from_wire(wire)
    clock = StageClock(stage.policy.get('worker_timeout_s', 1800), clock=clock, exempt=bool(stage.approved_path))
    run = StageRun(stage, clock, out=io.BytesIO(), root_of=lambda row: None, limit=10 ** 6)
    run.evidence = disk_evidence.StageEvidence(stage, clock, processes=lambda **o: placement.cleanup_processes(**o),
                                               **({'listings': listings} if listings else {}))
    return run


def answer(run):
    """`run` finished, and its lines read back as `cleanup_call` reads them."""
    run.finish()
    return asdict(read_rows(run._out.getvalue()))


def staged(stage, request, *, clock=time.monotonic, listings=None, **options):
    """`stage(run, **options)` on `stage_run(request)`, and its answer."""
    run = stage_run(request, clock=clock, listings=listings)
    stage(run, **options)
    return answer(run)


def placement_processes():
    """Patch `disk_side.StageEvidence` so a stage's first process proof is asked of `placement.cleanup_processes` when
    it is taken, as its fresh proofs are: a test patching that one name then reaches both. The default binds at
    definition."""
    from drover import disk_evidence, disk_side

    def processes(**options):
        return placement.cleanup_processes(**options)
    return patch.object(disk_side, 'StageEvidence', partial(disk_evidence.StageEvidence, processes=processes))


def served(wire, **options):
    """`disk_side.serve` run in this process, its lines read back as `cleanup_call` reads them, so a test sees the
    answer the coordinator gets, with `placement_processes`."""
    from drover import disk_side
    out = io.BytesIO()
    with placement_processes():
        disk_side.serve(wire, out=out, **options)
    return asdict(read_rows(out.getvalue()))


def empty_result():
    return {'items': [], 'errors': [], 'reclaimed_bytes': 0}


class DiskCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='disk-contract-', dir=clean_parent())
        self.addCleanup(self.discard_tree)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.repos_dir = self.state / 'repos'
        self.temp_root = self.root / 'declared temp'
        self.report_root = self.root / 'reports'
        for path in (self.state, self.repos_dir, self.temp_root, self.report_root):
            path.mkdir(parents=True, exist_ok=True)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg['paths'].update(state_dir=str(self.state), repos_root=str(self.root / 'repos'),
                                 worktrees=str(self.root / 'worktrees'), wsl_distro='Disk Test Distro')
        self.cfg['repos_dir'] = str(self.repos_dir)
        self.cfg['trackers'] = [dict(kind='disk', name='test-drive', enabled=True,
                                    path=str(self.root), warn_gb=150., queue_gb=100.,
                                    hold_gb=50., critical_gb=25.)]
        self.cfg['disk_cleanup'] = dict(temp_roots=[str(self.temp_root)],
                                        report_roots=[str(self.report_root)], caches=[])
        self.now = 2_000_000_000.
        self.free = 80.
        self.calls, self.toasts, self.busy_paths = [], [], set()
        self.tasks = {}
        self.stage_clock = None         # a StageClock factory for each served stage; None is the real clock
        self.side = placement.cleanup_side()
        self.other_side = 'wsl' if self.side == 'windows' else 'windows'
        for context in (patch.dict(os.environ, {'DROVER_HOME': str(self.state)}),
                        patch.object(config, 'get', return_value=self.cfg)):
            context.start()
            self.addCleanup(context.stop)

    def discard_tree(self):
        self.tmp.cleanup()

    def busy(self, path):
        path = Path(path)
        return any(path == Path(p) or path in Path(p).parents or Path(p) in path.parents
                   for p in self.busy_paths)

    def age(self, path, seconds):
        path = Path(path)
        if path.is_dir() and not path.is_symlink():
            for child in path.rglob('*'):
                if not child.is_symlink():
                    os.utime(child, (self.now - seconds, self.now - seconds))
        if not path.is_symlink():
            os.utime(path, (self.now - seconds, self.now - seconds))
        return path

    def file(self, path, text='sentinel', age=8 * DAY):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        self.age(path, age)
        return path

    def runtime(self, real=False, handler=None):
        from drover.disk_cleanup import CleanupRuntime

        def call(side, request):
            # The one place a StageRequest becomes today's dict, a deep copy, which handlers and `calls` read.
            request = request.to_wire()
            self.calls.append((side, copy.deepcopy(request)))
            if handler is not None:
                return handler(side, request)
            if not real or side != self.side or request['stage'] == 1:
                return empty_result()
            # Tests declare every temp root explicitly, never inspect host temp.
            request = {**request, 'tasks': self.tasks,
                       'paths': {**request['paths'], 'temp_roots': self.cfg['disk_cleanup']['temp_roots'],
                                 'report_roots': self.cfg['disk_cleanup']['report_roots'],
                                 'caches': self.cfg['disk_cleanup']['caches']}}
            return served(request, **({'clock_factory': self.stage_clock} if self.stage_clock else {}))

        return CleanupRuntime(lambda: self.now, lambda path: self.free, call, self.toasts.append)

    def run_cleanup(self, *, apply=False, mode='on-demand', real=True, handler=None):
        from drover import disk_cleanup, placement
        proof = SimpleNamespace(busy=self.busy)
        with patch.object(placement, 'cleanup_processes', return_value=proof), \
             patch.object(placement, 'physical_disk_path', return_value=str(self.root)), \
             patch('tempfile.gettempdir', return_value=str(self.temp_root)), \
             patch.dict(os.environ, {name: str(self.temp_root) for name in ('TMPDIR', 'TMP', 'TEMP')}):
            return disk_cleanup.run(self.cfg, mode=mode, apply=apply,
                                    runtime=self.runtime(real=real, handler=handler))

    def approve_report(self):
        """Obtain matching dry receipt via the same public interface as an owner."""
        return self.run_cleanup(apply=False)


class Fixture(DiskCase):
    def test_hermetic_roots(self):
        self.assertEqual(config.state_dir(self.cfg), self.state)
        self.assertEqual(config.repos_dir(self.cfg), self.repos_dir)
        self.assertEqual(Path(os.environ['DROVER_HOME']), self.state)
        for path in (self.state, self.repos_dir, self.temp_root, self.report_root,
                     Path(self.cfg['trackers'][0]['path'])):
            self.assertTrue(path.is_relative_to(self.root))
        self.assertEqual(self.cfg['disk_cleanup']['caches'], [])
        self.assertEqual(self.calls, [])


class MemoryCleanupState:
    """Transactional state/journal fake; no disk, clocks or child processes."""
    def __init__(self):
        self.state, self.journal = {}, []
        self.fail_journal = False

    def update(self, transform):
        state = copy.deepcopy(self.state)
        answer = transform(state)
        self.state = state
        return answer

    def append(self, record):
        if self.fail_journal:
            raise OSError('journal unavailable')
        self.journal.append(copy.deepcopy(record))


def bare_evidence(request):
    """A StageEvidence holding only `request`'s task snapshot and time, on a fresh 1800 s clock, for an adapter or a
    test that lists no repo."""
    from drover.disk_evidence import StageEvidence
    stage = StageRequest(stage=4, side=placement.cleanup_side(), mode=None, apply=bool(request.get('apply')),
                         now=request['now'], drive=dict(path='/'), repos=(), policy={}, state_dir=None,
                         tasks=request.get('tasks') or {})
    return StageEvidence(stage, StageClock(1800), processes=None)


class MemoryCleanupEvidence:
    """Semantic observations, never a fake Git CLI. All policy lives in decide."""
    def __init__(self, store):
        self.store = store
        self.paths = {}
        self.before_apply = None
        self.operations = []

    def add(self, path, **facts):
        row = dict(path=path, repo='repo', name='alpha', head='head', branch='refs/heads/' + path,
                   side_lock='native', gitdir=path + '/gitdir', action='keep', reason_code='unsettled',
                   dirty=[], ignored=[], tag=dict(v=2, repo='alpha', owner='session:' + path,
                       lease_until=9_000_000_000), owner='session:' + path, files=1, bytes=10, age_s=40 * DAY)
        observation = dict(row=row, checks={}, settlement=dict(live=False, terminal=False, merged=True),
                           identity='original', excluded=[], nested=False, removed=False)
        observation.update(facts)
        self.paths[path] = observation
        return observation

    def observe(self, request):
        evidence = bare_evidence(request)
        observations = []
        for path, facts in self.paths.items():
            if facts['removed']:
                continue
            observed = copy.deepcopy(facts)
            row = observed['row']
            live, terminal = evidence.task_facts(path, row.get('tag'))
            observed['settlement'].update(live=live, terminal=terminal)
            if row.get('unlisted'):
                tag = row.get('tag')
                lease = bool(tag and tag['v'] == 2 and tag['lease_until'] > request['now'])
                observed['settlement']['live'] |= lease
            observed['quarantine'] = dict(key=path, identity=repr((facts['identity'], row.get('tag'), row['head'],
                                                               row['branch'], row['side_lock'])),
                                          excluded=facts['excluded'],
                                          live=bool(row.get('unlisted') and observed['settlement']['live']))
            # Classification and grace run together in the production decision function.
            observations.append(observed)
        return observations

    def apply(self, request, decision, expected_identity):
        from drover.disk_authority import decide
        from drover.disk_evidence import registration_identity
        if self.before_apply:
            self.before_apply(decision['path'])
        observed = next(o for o in self.observe(request) if o['row']['path'] == decision['path'])
        facts = self.paths[decision['path']]
        current = decide(request, [observed], self.store.state, request['now'])['decisions'][0]
        identity = registration_identity(current)
        keys = ('head', 'branch', 'side_lock', 'gitdir', 'tag')
        if current['action'] != 'candidate' or any(identity.get(k) != expected_identity.get(k) for k in keys):
            self.store.update(lambda state: state.setdefault('quarantine', {}).pop(decision['path'], None))
            return dict(current, action='keep', reason_code='revalidation_changed')
        validation = dict(nested=facts['nested'], excluded=facts['excluded'])
        current = decide(dict(request, approved_path=None), [dict(row=current, validation=validation)], {}, request['now'])['decisions'][0]
        if current['action'] != 'candidate':
            return current
        try:
            self.store.append(dict(path=decision['path'], phase='planned'))
        except OSError as error:
            return dict(current, action='keep', error=str(error))
        self.operations.append(('remove', decision['path']))
        facts['removed'] = True
        self.store.append(dict(path=decision['path'], phase='completed'))
        return dict(decision, action='removed')


class MemoryCleanupCase(unittest.TestCase):
    def setUp(self):
        from drover.disk_cleanup import CleanupRuntime
        self.now = 2_000_000_000.
        self.tasks = dict(at=self.now, active_worktrees=[], tasks=[])
        self.store = MemoryCleanupState()
        self.evidence = MemoryCleanupEvidence(self.store)
        self.runtime = CleanupRuntime(lambda: self.now, None, None, None,
                                      evidence=self.evidence, state=self.store)
        # Any accidental process use in these policy tests fails immediately.
        guard = patch('subprocess.Popen', side_effect=AssertionError('memory cleanup started a process'))
        guard.start()
        self.addCleanup(guard.stop)

    def cleanup(self, *, apply=False, approved_path=None):
        from drover.disk_cleanup import evaluate
        return evaluate(dict(apply=apply, now=self.now, tasks=self.tasks, approved_path=approved_path), self.runtime)

    def kept(self, path):
        return not self.evidence.paths[path]['removed']
