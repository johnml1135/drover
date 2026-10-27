"""Hermetic public-interface fixtures for the disk contract.

No new product module is imported until a test actually exercises its interface.
"""
import copy
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from drover import config, placement
from tests.gitfix import clean_parent

DAY = 86400
GIB = 1024 ** 3


def empty_result():
    return {'items': [], 'errors': [], 'reclaimed_bytes': 0}


class DiskCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='disk-contract-', dir=clean_parent())
        self.addCleanup(self.tmp.cleanup)
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
        self.side = placement.cleanup_side()
        self.other_side = 'wsl' if self.side == 'windows' else 'windows'
        for context in (patch.dict(os.environ, {'DROVER_HOME': str(self.state)}),
                        patch.object(config, 'get', return_value=self.cfg)):
            context.start()
            self.addCleanup(context.stop)

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
            self.calls.append((side, copy.deepcopy(request)))
            if handler is not None:
                return handler(side, request)
            if not real or side != self.side or request['stage'] == 1:
                return empty_result()
            from drover.disk_side import serve
            # Tests declare every temp root explicitly, never inspect host temp.
            request = {**request, 'tasks': self.tasks,
                       'paths': {**request['paths'], 'temp_roots': self.cfg['disk_cleanup']['temp_roots'],
                                 'report_roots': self.cfg['disk_cleanup']['report_roots'],
                                 'caches': self.cfg['disk_cleanup']['caches']}}
            return serve(request)

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
        from drover.disk_worktrees import task_facts
        observations = []
        for path, facts in self.paths.items():
            if facts['removed']:
                continue
            observed = copy.deepcopy(facts)
            row = observed['row']
            live, terminal = task_facts(request, path, row.get('tag'))
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
        from drover.disk_cleanup import decide
        from drover.disk_worktrees import registration_identity
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
