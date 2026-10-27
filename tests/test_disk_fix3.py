"""Third safety review: regression tests at cleanup and placement boundaries."""
from unittest.mock import patch
from contextlib import contextmanager
from pathlib import Path

from tests.disk_fixture import DiskCase, MemoryCleanupCase, MemoryCleanupEvidence, MemoryCleanupState
from tests import test_disk_fix2 as fix2


@contextmanager
def output_facts(path, side, registrations=None):
    """A source-free checkout: fake Git observations, real path protection probes."""
    from drover import disk_paths, disk_worktrees
    entries = [dict(worktree=str(p), HEAD='head', branch='refs/heads/test', locked=side)
               for p in (registrations if registrations is not None else [path])]
    def status(root, **kwargs):
        generated = Path(root) / 'generated'
        return [], [str(p) for p in generated.rglob('*') if p.is_file() or p.is_symlink()]
    with patch.object(disk_worktrees, 'entries', return_value=entries), \
         patch.object(disk_worktrees, 'status', side_effect=status) as observed_status, \
         patch.object(disk_worktrees, 'git', return_value=''), \
         patch.object(disk_paths, 'output_evidence', side_effect=lambda request, machine, entry, **kw:
                      (Path(entry['worktree']), 'head', None, ['generated'])):
        yield observed_status


class ProcessFreeDiskCase(DiskCase):
    def setUp(self):
        super().setUp()
        guard = patch('subprocess.Popen', side_effect=AssertionError('fast cleanup started a process'))
        guard.start()
        self.addCleanup(guard.stop)


class CancellationSafety(ProcessFreeDiskCase):
    def test_every_exception_confirms_termination_before_releasing_lock(self):
        import json
        import subprocess
        from types import SimpleNamespace
        from drover import disk_cleanup, disk_state, placement
        for failure in (KeyboardInterrupt, SystemExit,
                        lambda: UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'bad worker output')):
            for confirmed in (True, False):
                error = failure()
                with self.subTest(error=type(error).__name__, confirmed=confirmed):
                    disk_state.update(self.state, lambda state: state.update(
                        report_scope=disk_cleanup.scope_key(self.cfg)))
                    checks = []
                    previous = set((self.state / 'disk').glob('worker-error-*.json'))
                    def execute(argv, **kwargs):
                        request = json.loads(kwargs['input'])
                        disk_state.save_file(self.state, 'worker-' + request['_worker_id'] + '.json',
                                             dict(pid=123456789, run_id=request['_worker_id']))
                        raise error
                    def confirm(*args):
                        self.assertTrue((self.state / 'disk-cleanup.lock').exists())
                        checks.append(True)
                        return confirmed
                    rt = self.runtime()
                    with patch.object(subprocess, 'run', side_effect=execute), \
                            patch.object(placement, 'physical_disk_path', return_value=str(self.root)), \
                            patch.object(placement, 'recover_ci_children', side_effect=confirm), \
                            patch.object(placement, '_CleanupJob', return_value=SimpleNamespace(
                                terminate=confirm, close=lambda: None)):
                        try:
                            disk_cleanup.run(self.cfg, apply=True, runtime=disk_cleanup.CleanupRuntime(
                                rt.now, rt.read, placement.cleanup_call, rt.toast))
                        except BaseException as caught:
                            self.assertIsInstance(caught, type(error) if confirmed else placement.CleanupWorkerError)
                        else:
                            self.assertIsInstance(error, UnicodeDecodeError)
                            self.assertTrue(confirmed) # coordinator reports confirmed decode failures
                    self.assertTrue(checks, 'cancellation must confirm termination')
                    lock = self.state / 'disk-cleanup.lock'
                    self.assertEqual(lock.exists(), not confirmed)
                    if not confirmed:
                        records = list(set((self.state / 'disk').glob('worker-error-*.json')) - previous)
                        self.assertTrue(records)
                        self.assertFalse(json.loads(records[-1].read_text())['termination_confirmed'])
                        # The test owns this simulated lock; never touches live locks.
                        import shutil
                        shutil.rmtree(lock)

    def test_windows_job_is_drained_even_without_worker_identity(self):
        import subprocess
        from types import SimpleNamespace
        from drover import placement
        drained = []
        def terminate():
            drained.append(True)
            return True
        request = fix2.PathSafety.request(self, 2)
        with patch.object(placement, 'IS_WINDOWS', True), \
                patch.object(subprocess, 'run', side_effect=KeyboardInterrupt), \
                patch.object(placement, '_CleanupJob', return_value=SimpleNamespace(
                    terminate=terminate, close=lambda: None)):
            try:
                placement.cleanup_call('windows', request)
            except BaseException as error:
                self.assertIsInstance(error, placement.CleanupWorkerError)
            else:
                self.fail('missing worker identity must retain the lock')
        self.assertEqual(drained, [True])


class TargetDriveSafety(ProcessFreeDiskCase):
    def test_each_target_needs_known_authorized_identity_at_selection_and_deletion(self):
        from pathlib import Path
        from types import SimpleNamespace
        from drover import placement
        from drover.disk_side import serve
        wt = self.root / 'checkout'
        wt.mkdir()
        for stage in (2, 3, 5):
            for changed in (False, True):
                for foreign in ('/simulated/other', None):
                    with self.subTest(stage=stage, changed=changed, foreign=foreign):
                        base = wt / 'generated' if stage == 5 else self.temp_root
                        target = self.file(base / f'target-{stage}-{changed}-{foreign is None}')
                        control = self.file(base / 'disposable-control')
                        request = fix2.PathSafety.request(self, stage)
                        request['repos'] = [dict(name='alpha', repo=str(wt),
                            worktrees=str(self.root / 'alpha.worktrees'), remote='origin', default_branch='main')]
                        request['paths'] = dict(temp_roots=[str(base)], report_roots=[],
                            caches=[dict(path=str(base), placement=self.side)])
                        checks = []
                        def physical(path):
                            if Path(path) == target:
                                checks.append(str(path))
                                if not changed or len(checks) >= 2:
                                    return foreign
                            return '/simulated/primary'
                        with output_facts(wt, self.side), \
                            patch.object(placement, 'physical_disk_path', side_effect=physical), \
                                patch.object(placement, 'cleanup_free', return_value=80), \
                                patch.object(placement, 'cleanup_processes', return_value=SimpleNamespace(busy=lambda p: False)):
                            result = serve(request)
                        self.assertTrue(target.exists(), result)
                        self.assertTrue(any(i['path'] == str(target) and i['action'] == 'keep'
                                            and i.get('reason_code') for i in result['items']), result)
                        self.assertFalse(control.exists(), 'eligible control proves stage ran')
                        self.assertGreaterEqual(len(checks), 2 if changed else 1)
                        target.unlink()


class LiveOwnerSafety(MemoryCleanupCase):
    def test_unregistered_live_owners_reset_grace_then_release(self):
        for kind in ('active', 'nonterminal', 'live', 'lease'):
            with self.subTest(kind=kind):
                observed = self.evidence.add(kind)
                observed['row'].update(unlisted=True, tag=None)
                def owner(enabled):
                    observed['row']['tag'] = (dict(v=2, repo='alpha', owner='session:test',
                        lease_until=self.now + 86400) if enabled and kind == 'lease' else None)
                    self.tasks = dict(at=self.now,
                        active_worktrees=[kind] if enabled and kind == 'active' else [],
                        tasks=[dict(id='owner', worktree=kind, terminal=kind == 'live', live=kind == 'live')]
                        if enabled and kind in ('nonterminal', 'live') else [])
                owner(True)
                self.cleanup()
                self.cleanup(apply=True)
                self.now += 3 * 86400
                owner(True)
                self.cleanup(apply=True)
                self.assertTrue(self.kept(kind))
                owner(False)
                self.cleanup(apply=True)
                self.now += 2 * 86400
                owner(False)
                self.cleanup(apply=True)
                self.assertTrue(self.kept(kind), 'live observations reset quarantine')
                self.now += 86400
                owner(False)
                self.cleanup(apply=True)
                self.assertFalse(self.kept(kind), 'release starts a new full grace period')

    def test_owner_appearing_during_removal_keeps_unregistered_directory(self):
        observed = self.evidence.add('late owner')
        observed['row'].update(unlisted=True, tag=None)
        self.cleanup()
        self.cleanup(apply=True)
        self.now += 3 * 86400
        self.tasks['at'] = self.now
        def late_owner(path):
            observed['row']['tag'] = dict(v=2, repo='alpha', owner='session:late', lease_until=self.now + 86400)
        self.evidence.before_apply = late_owner
        self.cleanup(apply=True)
        self.assertTrue(self.kept('late owner'))
        observed['row']['tag'] = None
        self.evidence.before_apply = None
        self.cleanup(apply=True)
        self.assertTrue(self.kept('late owner'), 'new owner cancels elapsed quarantine')
        self.now += 3 * 86400
        self.cleanup(apply=True)
        self.assertFalse(self.kept('late owner'))

    def test_unknown_process_and_identity_change_never_use_cached_authority(self):
        for change in ('unknown', 'head', 'lock', 'dirty', 'activity'):
            with self.subTest(change=change):
                observed = self.evidence.add(change)
                if change == 'activity':
                    observed['row']['tag'] = None
                    self.cleanup(apply=True)
                    self.now += 3 * 86400
                def race(path):
                    if change == 'unknown':
                        observed['checks'].update(busy=True, process_reason='process_visibility_unavailable')
                    elif change == 'dirty':
                        observed['row']['dirty'] = ['owner data']
                    elif change == 'head':
                        observed['row']['head'] = 'new-head'
                    elif change == 'lock':
                        observed['row']['side_lock'] = 'foreign'
                        observed['checks']['other_side'] = True
                    else:
                        observed['identity'] = 'new activity'
                self.evidence.before_apply = race
                self.cleanup(apply=True)
                self.assertTrue(self.kept(change))
                self.evidence.before_apply = None



class LinuxIdentitySafety(ProcessFreeDiskCase):
    def test_linux_mount_identity_is_case_sensitive_with_windows_path_flavour(self):
        import os
        from pathlib import PureWindowsPath
        from types import SimpleNamespace
        from drover import placement
        mountinfo = ('1 0 8:1 / / rw - ext4 /dev/root rw\n'
                     '2 1 8:2 / /data rw - ext4 /dev/a rw\n'
                     '3 1 8:3 / /Data rw - ext4 /dev/b rw\n')
        def windows_path(value):
            if str(value) == '/proc/self/mountinfo':
                return SimpleNamespace(read_text=lambda: mountinfo)
            return PureWindowsPath(value)
        request = fix2.PathSafety.request(self, 2)
        request['drive']['path'] = '/data'
        with patch.object(placement, 'IS_WINDOWS', False), \
                patch.dict(os.environ, {'WSL_DISTRO_NAME': ''}), \
                patch.object(placement, 'Path', side_effect=windows_path):
            self.assertTrue(placement.cleanup_contains(request, '/data/candidate'))
            self.assertFalse(placement.cleanup_contains(request, '/Data/candidate'))


class ProtectedTreeProperty(ProcessFreeDiskCase):
    def test_randomized_protected_kinds_survive_every_stage(self):
        import random
        import subprocess
        from pathlib import Path
        from types import SimpleNamespace
        from drover import placement, disk_paths
        from drover.disk_side import serve
        from drover.disk_cleanup import CleanupRuntime, evaluate
        kinds = ('repo', 'bare', 'incomplete', 'venv', 'link', 'mount', 'live')
        all_protected, all_mounts, all_live = [], [], []
        registrations, protected_worktrees, stage4_candidates = [], [], []
        store = MemoryCleanupState()
        evidence = MemoryCleanupEvidence(store)
        for seed in range(3):
            rng = random.Random(seed)
            for stage in range(1, 7):
                with self.subTest(seed=seed, stage=stage):
                    base = self.temp_root / f'independent-{seed}-{stage}'
                    wt = self.root / f'checkout-{seed}'
                    if stage == 5:
                        base = wt / 'generated'
                        registrations.append(wt)
                        protected_worktrees.append(str(wt))
                        evidence.add(str(wt))['settlement']['merged'] = False
                    protected, mounts, live, controls, cache_roots = [], [], [], [], []
                    shuffled = rng.sample(kinds, len(kinds))
                    for kind in shuffled:
                        candidate = base / kind / 'protected'
                        control = base / kind / 'control'
                        folder = candidate / 'generated' if stage == 4 else candidate
                        if kind == 'mount':
                            mounts.append(candidate if stage == 4 else folder)
                        if kind == 'live':
                            live.append(str(candidate if stage == 4 else folder))
                        for depth in range(rng.randrange(3)):
                            folder /= f'depth-{depth}'
                        if kind == 'repo':
                            self.file(folder / '.git' / 'HEAD', 'ref: refs/heads/main')
                        elif kind == 'bare':
                            self.file(folder / 'config', rng.choice(('[core] bare=true\n', '\ufeff[core]\n bare=true\n')))
                        elif kind == 'incomplete':
                            self.file(folder / '.git', 'gitdir: absent')
                        elif kind == 'venv':
                            folder /= '.venv'
                        elif kind == 'link':
                            outside = self.root / f'link-target-{seed}-{stage}'
                            sentinel = self.file(outside / 'valuable')
                            folder.parent.mkdir(parents=True, exist_ok=True)
                            folder.symlink_to(outside, target_is_directory=True)
                            protected.extend((folder, sentinel))
                        if kind != 'link':
                            protected.append(self.file(folder / 'valuable'))
                        controls.append(self.file(control / 'generated' / 'disposable' if stage == 4 else control / 'disposable'))
                        cache_roots.extend((dict(path=str(candidate), placement=self.side),
                                            dict(path=str(control), placement=self.side)))
                        self.age(candidate, 40 * 86400)
                        self.age(control, 40 * 86400)
                        if stage == 4:
                            registrations.extend((candidate, control))
                            protected_worktrees.append(str(candidate))
                            stage4_candidates.append(str(candidate))
                            observation = evidence.add(str(candidate))
                            observation['row']['tag']['lease_until'] = self.now + 86400
                            # Real path detectors produce facts at the adapter boundary.
                            # Unknown config parsing fails closed in-process; native grammar
                            # is checked by the real deletion contract.
                            with patch.object(placement, 'git', return_value=subprocess.CompletedProcess([], 128, '', 'unavailable')):
                                excluded = disk_paths.repository_tree(candidate / 'generated', boundary=candidate)
                            observation['excluded'] = [str(folder)] if excluded else []
                            observation['checks'].update(other_drive=kind == 'mount', busy=kind == 'live')
                            evidence.add(str(control))
                    all_protected.extend(protected)
                    all_mounts.extend(mounts)
                    all_live.extend(live)
                    request = fix2.PathSafety.request(self, stage)
                    request['repos'] = [dict(name='alpha', repo=str(wt),
                        worktrees=str(self.root / 'alpha.worktrees'), remote='origin', default_branch='main')]
                    request['paths'] = dict(temp_roots=[str(base / kind) for kind in shuffled],
                        report_roots=[str(base)], caches=cache_roots)
                    request['tasks'] = dict(at=self.now, active_worktrees=all_live, tasks=[])
                    def physical(path):
                        p = Path(path)
                        return '/simulated/other' if any(p == m or m in p.parents for m in all_mounts) else '/simulated/primary'
                    if stage == 4:
                        rt = CleanupRuntime(lambda: self.now, None, None, None, evidence=evidence, state=store)
                        first = evaluate(request, rt)
                        request['now'] += 4 * 86400
                        request['tasks']['at'] = request['now']
                        result = evaluate(request, rt)
                        stage4_request = request
                        self.assertTrue(set(protected_worktrees) <= {row['path'] for row in result['items']})
                        for path in protected_worktrees:
                            self.assertFalse(evidence.paths[path]['removed'], path)
                        registrations[:] = [p for p in registrations
                                            if str(p) not in evidence.paths or not evidence.paths[str(p)]['removed']]
                        for kind in shuffled:
                            self.assertTrue(evidence.paths[str(base / kind / 'control')]['removed'], kind)
                            self.assertFalse(evidence.paths[str(base / kind / 'protected')]['removed'], kind)
                    else:
                        with output_facts(wt, self.side, registrations) as observed_status, \
                                patch.object(placement, 'physical_disk_path', side_effect=physical), \
                                patch.object(placement, 'cleanup_free', return_value=80), \
                                patch.object(placement, 'cleanup_processes', return_value=SimpleNamespace(busy=lambda p: False)), \
                                patch.object(placement, 'trim_argv', return_value=None), \
                                patch.object(placement, 'git', return_value=subprocess.CompletedProcess([], 128, '', 'unavailable')):
                            first = serve(request)
                            request['now'] += 4 * 86400
                            request['tasks']['at'] = request['now']
                            result = serve(request)
                        if stage == 5:
                            self.assertEqual({Path(call.args[0]) for call in observed_status.call_args_list},
                                             set(registrations))
                        for kind, control in zip(shuffled, controls):
                            with self.subTest(kind=kind):
                                self.assertEqual(control.exists(), stage not in (2, 3, 5),
                                                 'each protection has an independent eligible control')
                    for path in all_protected:
                        self.assertTrue(path.exists(), (seed, stage, str(path), first, result))

        # Revisit all seeds after lease expiry and a full quarantine grace period.
        # Protected content must survive even when its lease no longer protects it.
        stage4_request['now'] += 4 * 86400
        stage4_request['tasks']['at'] = stage4_request['now']
        result = evaluate(stage4_request, rt)
        self.assertTrue(set(protected_worktrees) <= {row['path'] for row in result['items']})
        for path in stage4_candidates:
            with self.subTest(path=path, lease='expired'):
                observed = evidence.paths[path]
                self.assertLessEqual(observed['row']['tag']['lease_until'], stage4_request['now'])
                self.assertFalse(observed['removed'], result)
        for path in all_protected:
            self.assertTrue(path.exists(), ('expired stage-4 lease', str(path), result))


class RepositorySafety(ProcessFreeDiskCase):
    request = fix2.PathSafety.request
    execute = fix2.PathSafety.execute

    def test_partial_or_unreadable_repository_evidence_survives(self):
        import subprocess
        from drover import placement
        for stage in (2, 3):
            for marker in ('HEAD', 'objects', 'refs', 'config', '.git'):
                with self.subTest(stage=stage, marker=marker):
                    root = self.temp_root / f'partial-{stage}-{marker}'
                    self.file(root / marker, 'incomplete repository')
                    valuable = self.file(root / 'unpublished')
                    self.age(root, 40 * 86400)
                    self.cfg['disk_cleanup']['caches'] = [dict(path=str(root), placement=self.side)]
                    with patch.object(placement, 'git', return_value=subprocess.CompletedProcess([], 128, '', 'unavailable')):
                        result = self.execute(self.request(stage))
                    self.assertTrue(valuable.exists(), result)
