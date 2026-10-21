"""Third safety review: regression tests at cleanup and placement boundaries."""
from unittest.mock import patch

from tests.disk_fixture import DiskCase
from tests import test_disk_fix2 as fix2


class RepositorySafety(DiskCase):
    request = fix2.PathSafety.request
    execute = fix2.PathSafety.execute

    def test_git_syntax_and_uncertain_repository_trees_survive(self):
        configs = ('[core] bare=true\n', '\ufeff[core]\n bare = true\n',
                   '[CoRe] BaRe = "true" ; comment\n', '[core\n bare=true\n',
                   '[core]\n bare = "unterminated\n')
        for stage in (2, 3):
            for index, config in enumerate(configs):
                with self.subTest(stage=stage, config=config):
                    root = self.temp_root / f'git-{stage}-{index}'
                    self.file(root / 'config', config)
                    valuable = self.file(root / 'unpublished')
                    self.age(root, 40 * 86400)
                    self.cfg['disk_cleanup']['caches'] = [dict(path=str(root), placement=self.side)]
                    result = self.execute(self.request(stage))
                    self.assertTrue(valuable.exists(), result)
            for marker in ('HEAD', 'objects', 'refs'):
                with self.subTest(stage=stage, marker=marker):
                    root = self.temp_root / f'partial-{stage}-{marker}'
                    self.file(root / marker, 'incomplete repository')
                    valuable = self.file(root / 'unpublished')
                    self.age(root, 40 * 86400)
                    self.cfg['disk_cleanup']['caches'] = [dict(path=str(root), placement=self.side)]
                    result = self.execute(self.request(stage))
                    self.assertTrue(valuable.exists(), result)


class CancellationSafety(DiskCase):
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
                    self.approve_report()
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


from tests.test_disk_worktrees import WorktreeCase


class TargetDriveSafety(WorktreeCase):
    def test_each_target_needs_known_authorized_identity_at_selection_and_deletion(self):
        from pathlib import Path
        from types import SimpleNamespace
        from drover import placement
        from drover.disk_side import serve
        wt = self.worktree('output mounts')
        self.r.commit('pending', cwd=wt)
        for stage in (2, 3, 5):
            for changed in (False, True):
                for foreign in ('/simulated/other', None):
                    with self.subTest(stage=stage, changed=changed, foreign=foreign):
                        base = wt / 'generated' if stage == 5 else self.temp_root
                        target = self.file(base / f'target-{stage}-{changed}-{foreign is None}')
                        control = self.file(base / 'disposable-control')
                        request = fix2.PathSafety.request(self, stage)
                        request['repos'] = [dict(name='alpha', repo=str(self.r.repo),
                            worktrees=str(self.trees), remote='origin', default_branch='main')]
                        request['paths'] = dict(temp_roots=[str(base)], report_roots=[],
                            caches=[dict(path=str(base), placement=self.side)])
                        checks = []
                        def physical(path):
                            if Path(path) == target:
                                checks.append(str(path))
                                if not changed or len(checks) >= 2:
                                    return foreign
                            return '/simulated/primary'
                        with patch.object(placement, 'physical_disk_path', side_effect=physical), \
                                patch.object(placement, 'cleanup_free', return_value=80), \
                                patch.object(placement, 'cleanup_processes', return_value=SimpleNamespace(busy=lambda p: False)):
                            result = serve(request)
                        self.assertTrue(target.exists(), result)
                        self.assertTrue(any(i['path'] == str(target) and i['action'] == 'keep'
                                            and i.get('reason_code') for i in result['items']), result)
                        self.assertFalse(control.exists(), 'eligible control proves stage ran')
                        self.assertGreaterEqual(len(checks), 2 if changed else 1)
                        target.unlink()


class LiveOwnerSafety(WorktreeCase):
    def test_unregistered_live_owners_reset_grace_then_release(self):
        import json
        import shutil
        from drover import placement
        original = placement.worktree_metadata
        for kind in ('active', 'nonterminal', 'live', 'lease'):
            with self.subTest(kind=kind):
                path = self.trees / kind
                self.trees.mkdir(exist_ok=True)
                shutil.copytree(self.r.repo, path, ignore=shutil.ignore_patterns('.git'))
                live = False
                def metadata(worktree, name, *args):
                    if str(worktree) == str(path):
                        return json.dumps(dict(v=2, repo='alpha', owner='session:test',
                            created=self.now - 1, lease_until=self.now + 86400)) if live and kind == 'lease' else None
                    return original(worktree, name, *args)
                def owner(enabled):
                    nonlocal live
                    live = enabled
                    self.tasks = dict(at=self.now, active_worktrees=[str(path)] if enabled and kind == 'active' else [],
                        tasks=[dict(id='owner', worktree=str(path), terminal=kind == 'live', live=kind == 'live')]
                        if enabled and kind in ('nonterminal', 'live') else [])
                with patch.object(placement, 'worktree_metadata', side_effect=metadata):
                    owner(True)
                    self.cleanup()
                    self.cleanup(apply=True)
                    self.now += 3 * 86400
                    owner(True)
                    self.cleanup(apply=True)
                    self.assertTrue(path.exists(), kind)
                    owner(False)
                    self.cleanup(apply=True)
                    self.now += 2 * 86400
                    owner(False)
                    self.cleanup(apply=True)
                    self.assertTrue(path.exists(), 'live observations must reset quarantine')
                    self.now += 86400
                    owner(False)
                    self.cleanup(apply=True)
                    self.assertFalse(path.exists(), 'owner release starts a new full grace period')

    def test_owner_appearing_during_removal_keeps_unregistered_directory(self):
        import shutil
        from types import SimpleNamespace
        from drover import placement
        path = self.trees / 'late owner'
        self.trees.mkdir(exist_ok=True)
        shutil.copytree(self.r.repo, path, ignore=shutil.ignore_patterns('.git'))
        self.cleanup()
        self.cleanup(apply=True)
        self.now += 3 * 86400
        self.tasks = dict(at=self.now, active_worktrees=[], tasks=[])
        checks = []
        live = []
        metadata = placement.worktree_metadata
        def read_owner(worktree, name, *args):
            if str(worktree) == str(path) and live:
                import json
                return json.dumps(dict(v=2, repo='alpha', owner='session:late',
                    created=self.now, lease_until=self.now + 86400))
            return metadata(worktree, name, *args)
        def busy(candidate):
            if candidate == str(path):
                checks.append(candidate)
                if len(checks) == 2:
                    live.append(True)
            return False
        # run_cleanup installs the process adapter; busy is its declared test hook.
        self.busy = busy
        with patch.object(placement, 'worktree_metadata', side_effect=read_owner):
            self.cleanup(apply=True)
            self.assertGreaterEqual(len(checks), 2)
            self.assertTrue(path.exists(), 'live ownership must be checked again before removal')
            live.clear()
            self.busy = lambda candidate: False
            self.cleanup(apply=True)
            self.assertTrue(path.exists(), 'a newly observed owner cancels elapsed quarantine')
            self.now += 3 * 86400
            self.cleanup(apply=True)
            self.assertFalse(path.exists(), 'released owner gets a fresh full grace period')


class LinuxIdentitySafety(DiskCase):
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


class ProtectedTreeProperty(WorktreeCase):
    def test_randomized_protected_kinds_survive_every_stage(self):
        import random
        from pathlib import Path
        from types import SimpleNamespace
        from drover import placement
        from drover.disk_side import serve
        kinds = ('repo', 'bare', 'incomplete', 'venv', 'link', 'mount', 'live')
        all_protected, all_mounts, all_live = [], [], []
        for seed in range(3):
            rng = random.Random(seed)
            for stage in range(1, 7):
                with self.subTest(seed=seed, stage=stage):
                    base = self.temp_root / f'independent-{seed}-{stage}'
                    if stage == 5:
                        wt = self.worktree(f'outputs-{seed}')
                        self.r.commit('unmerged', cwd=wt)
                        base = wt / 'generated'
                    protected, mounts, live, controls, cache_roots = [], [], [], [], []
                    shuffled = rng.sample(kinds, len(kinds))
                    for kind in shuffled:
                        candidate = base / kind / 'protected'
                        control = base / kind / 'control'
                        if stage == 4:
                            # Both are otherwise eligible registered, merged, leased worktrees.
                            candidate = self.worktree(f'protected-{seed}-{kind}')
                            control = self.worktree(f'control-{seed}-{kind}')
                            folder = candidate / 'generated'
                            # Mount and live-owner protection applies to the worktree itself.
                            if kind == 'mount':
                                mounts.append(candidate)
                            elif kind == 'live':
                                live.append(str(candidate))
                        else:
                            folder = candidate
                        for depth in range(rng.randrange(3)):
                            folder /= f'depth-{depth}'
                        if kind == 'repo':
                            from tests.gitfix import sh
                            sh('git', 'init', '-q', folder)
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
                        elif kind == 'mount' and stage != 4:
                            mounts.append(folder)
                        elif kind == 'live' and stage != 4:
                            live.append(str(folder))
                        if kind != 'link':
                            protected.append(self.file(folder / 'valuable'))
                        if stage == 4:
                            controls.append(self.file(control / 'generated' / 'disposable'))
                        else:
                            controls.append(self.file(control / 'disposable'))
                        # A protected cache root must not suppress its independent control.
                        cache_roots.extend((dict(path=str(candidate), placement=self.side),
                                            dict(path=str(control), placement=self.side)))
                        self.age(candidate, 40 * 86400)
                        self.age(control, 40 * 86400)
                    all_protected.extend(protected)
                    all_mounts.extend(mounts)
                    all_live.extend(live)
                    request = fix2.PathSafety.request(self, stage)
                    request['repos'] = [dict(name='alpha', repo=str(self.r.repo),
                        worktrees=str(self.trees), remote='origin', default_branch='main')]
                    request['paths'] = dict(temp_roots=[str(base / kind) for kind in shuffled],
                        report_roots=[str(base)], caches=cache_roots)
                    request['tasks'] = dict(at=self.now, active_worktrees=all_live, tasks=[])
                    def physical(path):
                        p = Path(path)
                        return '/simulated/other' if any(p == m or m in p.parents for m in all_mounts) else '/simulated/primary'
                    with patch.object(placement, 'physical_disk_path', side_effect=physical), \
                            patch.object(placement, 'cleanup_free', return_value=80), \
                            patch.object(placement, 'cleanup_processes', return_value=SimpleNamespace(busy=lambda p: False)), \
                            patch.object(placement, 'trim_argv', return_value=None):
                        first = serve(request)
                        request['now'] += 4 * 86400
                        request['tasks']['at'] = request['now']
                        result = serve(request)
                    for kind, control in zip(shuffled, controls):
                        with self.subTest(kind=kind):
                            if stage in (2, 3, 4, 5):
                                self.assertFalse(control.exists(), 'each protection has an eligible control')
                            else:
                                self.assertTrue(control.exists(), 'trim and report stages do not remove paths')
                    for path in all_protected:
                        self.assertTrue(path.exists(), (seed, stage, str(path), first, result))
