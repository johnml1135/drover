"""Second review regressions, through cleanup/config/placement interfaces only."""
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from drover import config, placement
from drover.disk_side import serve
from tests.tiers import integration
from tests.disk_fixture import DiskCase
from tests.disk_memory import MemoryWorktreeCase as WorktreeCase, ProcessFreeDiskCase


class PathSafety(ProcessFreeDiskCase):
    def request(self, stage):
        return dict(stage=stage, mode='triggered', apply=True, now=self.now,
                    state_dir=str(self.state), repos=[], tasks={},
                    drive=dict(path=str(self.root), level='hold', warn_gb=150),
                    paths=self.cfg['disk_cleanup'])

    def execute(self, request):
        with patch.object(placement, 'physical_disk_path', return_value=str(self.root)), \
                patch.object(placement, 'cleanup_free', return_value=80), \
                patch.object(placement, 'cleanup_processes', return_value=SimpleNamespace(busy=lambda p: False)):
            return serve(request)

    def repository(self, root, kind):
        if kind == 'bare-layout':
            self.file(root / 'HEAD', 'ref: refs/heads/unpublished')
            (root / 'objects').mkdir()
            self.file(root / 'refs/heads/unpublished')
        elif kind == 'bare-config':
            self.file(root / 'config', '[core]\n bare = true\n')
        elif kind == 'git-dir':
            (root / '.git').mkdir(parents=True)
        else:
            self.file(root / '.git', 'gitdir: missing')
        return self.file(root / 'source/precious')

    def test_bare_repositories_survive_temp_and_cache(self):
        for stage in (2, 3):
            for kind in ('bare-layout', 'bare-config'):
                with self.subTest(stage=stage, kind=kind):
                    root = self.temp_root / f'{stage}-{kind}'
                    valuable = self.repository(root, kind)
                    self.age(root, 40 * 86400)
                    self.cfg['disk_cleanup']['caches'] = [dict(path=str(root.parent), placement=self.side)]
                    result = self.execute(self.request(stage))
                    self.assertTrue(valuable.exists(), result)
                    self.assertTrue(any(i['action'] == 'keep' for i in result['items']))

    def test_bare_config_with_section_comment_protects_cache(self):
        root = self.root / 'commented bare cache'
        self.file(root / 'config', '[core] # git permits section comments\n bare = true\n')
        valuable = self.file(root / 'source/precious')
        self.cfg['disk_cleanup']['caches'] = [dict(path=str(root), placement=self.side)]
        result = self.execute(self.request(3))
        self.assertTrue(valuable.exists(), result)

    def test_incomplete_repositories_protect_cache_descendants(self):
        for kind in ('git-dir', 'git-file'):
            with self.subTest(kind=kind):
                root = self.root / kind
                valuable = self.repository(root / 'broken', kind)
                self.cfg['disk_cleanup']['caches'] = [dict(path=str(root), placement=self.side)]
                result = self.execute(self.request(3))
                self.assertTrue(valuable.exists(), result)
                self.assertTrue(any(i['action'] == 'keep' for i in result['items']))

    def test_validation_rejects_incomplete_repository_ancestors_and_descendants(self):
        for kind in ('git-dir', 'git-file', 'bare-layout', 'bare-config'):
            root = self.root / kind
            self.repository(root / 'repo', kind)
            for folder in (root, root / 'repo/source'):
                with self.subTest(kind=kind, folder=folder):
                    self.cfg['disk_cleanup']['caches'] = [dict(path=str(folder), placement=self.side)]
                    with self.assertRaisesRegex(config.ConfigError, 'protected|overlap'):
                        config.validate(self.cfg)

    def test_repository_appearing_at_final_protection_pass_is_kept(self):
        for stage in (2, 3):
            for kind in ('bare-layout', 'bare-config', 'git-dir'):
                with self.subTest(stage=stage, kind=kind):
                    root = self.temp_root / f'race-{stage}-{kind}'
                    valuable = self.file(root / 'payload')
                    self.age(root, 40 * 86400)
                    self.cfg['disk_cleanup']['caches'] = [dict(path=str(root), placement=self.side)]
                    def busy(path):
                        self.repository(root, kind)
                        self.age(root, 40 * 86400)
                        return False
                    with patch.object(placement, 'physical_disk_path', return_value=str(self.root)), \
                            patch.object(placement, 'cleanup_free', return_value=80), \
                            patch.object(placement, 'cleanup_processes', return_value=SimpleNamespace(busy=busy)):
                        result = serve(self.request(stage))
                    self.assertTrue(valuable.exists(), result)

    def test_validation_normalizes_parent_components_without_resolving_links(self):
        spare = self.root / 'spare'
        spare.mkdir()
        self.cfg['disk_cleanup']['caches'] = [dict(
            path=str(spare / '..' / self.state.name / 'private'), placement=self.side)]
        with self.assertRaisesRegex(config.ConfigError, 'protected|overlap'):
            config.validate(self.cfg)

    def test_unknown_mount_keeps_and_reports_candidates(self):
        valuable = self.file(self.temp_root / 'precious')
        with patch.object(placement, 'physical_disk_path', return_value=None), \
                patch.object(placement, 'cleanup_processes', return_value=SimpleNamespace(busy=lambda p: False)):
            result = serve(self.request(2))
        self.assertTrue(valuable.exists(), result)
        self.assertTrue(result['errors'] or result['items'], result)

    def test_case_distinct_linux_mounts_are_not_authorized(self):
        mountinfo = ('1 0 8:1 / / rw - ext4 /dev/root rw\n'
                     '2 1 8:2 / /data rw - ext4 /dev/a rw\n'
                     '3 1 8:3 / /Data rw - ext4 /dev/b rw\n')
        request = self.request(2)
        request['drive']['path'] = '/data'
        with patch.object(placement, 'IS_WINDOWS', False) as _placement_patch_1, patch('drover.placement_exec.IS_WINDOWS', _placement_patch_1), patch('drover.placement_git.IS_WINDOWS', _placement_patch_1), patch('drover.placement_host.IS_WINDOWS', _placement_patch_1), patch('drover.placement_paths.IS_WINDOWS', _placement_patch_1), patch('drover.placement_process.IS_WINDOWS', _placement_patch_1), patch('drover.placement_proof.IS_WINDOWS', _placement_patch_1), patch('drover.placement_tree.IS_WINDOWS', _placement_patch_1), patch('drover.placement_worker.IS_WINDOWS', _placement_patch_1), \
                patch.dict(os.environ, {'WSL_DISTRO_NAME': ''}), \
                patch('pathlib.Path.read_text', return_value=mountinfo):
            self.assertFalse(placement.cleanup_contains(request, '/Data/candidate'))


class OutputSafety(WorktreeCase):
    def test_registration_crossing_link_keeps_administrative_path(self):
        wt = self.worktree('linked')
        self.r.commit('pending', cwd=wt)
        valuable = self.file(wt / 'generated/result')
        moved = self.root / 'outside'
        wt.rename(moved)
        wt.symlink_to(moved, target_is_directory=True)
        self.cleanup()
        result = self.cleanup(apply=True)
        self.assertTrue((moved / valuable.relative_to(wt)).exists(), result)
        self.assertTrue(wt.is_symlink())
        self.assertTrue(any(i['path'] == str(wt) and i['action'] == 'keep' for i in result['items']))

    def test_dangling_registered_link_is_kept_without_deletion_intent(self):
        import shutil
        wt = self.worktree('dangling')
        shutil.rmtree(wt)
        wt.symlink_to(self.root / 'absent', target_is_directory=True)
        self.cleanup()
        result = self.cleanup(apply=True)
        self.assertTrue(wt.is_symlink(), result)
        self.assertIn(str(wt), self.registrations())
        self.assertFalse((self.state / 'disk-cleanup-manifest.jsonl').exists(), result)

    def test_registration_with_linked_ancestor_keeps_outputs(self):
        wt = self.worktree('linked ancestor')
        self.r.commit('pending', cwd=wt)
        self.file(wt / 'generated/result')
        moved = self.root / 'outside parent'
        self.trees.rename(moved)
        self.trees.symlink_to(moved, target_is_directory=True)
        self.cleanup()
        result = self.cleanup(apply=True)
        self.assertTrue((moved / wt.name / 'generated/result').exists(), result)
        self.assertTrue(any(i['path'] == str(wt) and i['action'] == 'keep' for i in result['items']))

    def test_nested_repositories_survive_declared_output_cleanup(self):
        wt = self.worktree('nested repositories')
        self.r.commit('pending', cwd=wt)
        paths = []
        for kind in ('bare-layout', 'bare-config', 'git-dir', 'git-file'):
            paths.append(PathSafety.repository(self, wt / 'generated' / kind, kind))
        self.cleanup()
        result = self.cleanup(apply=True)
        for path in paths:
            self.assertTrue(path.exists(), result)


class WorkerSafety(ProcessFreeDiskCase):
    request = PathSafety.request

    def test_successful_worker_requires_termination_confirmation_under_lock(self):
        from drover import disk_cleanup, disk_state
        self.approve_report()
        def execute(argv, **kwargs):
            request = json.loads(kwargs['input'])
            disk_state.save_file(self.state, 'worker-' + request['_worker_id'] + '.json', dict(
                pid=123456789, runner_pid=123456789, run_id=request['_worker_id']))
            return subprocess.CompletedProcess(argv, 0, json.dumps(dict(items=[], errors=[], reclaimed_bytes=0)), '')
        runtime = self.runtime()
        with patch.object(subprocess, 'run', side_effect=execute), \
                patch.object(placement, 'physical_disk_path', return_value=str(self.root)), \
                patch.object(placement, 'recover_ci_children', return_value=False), \
                patch.object(placement, '_CleanupJob', return_value=SimpleNamespace(terminate=lambda: False, close=lambda: None)):
            with self.assertRaises(placement.CleanupWorkerError):
                disk_cleanup.run(self.cfg, apply=True, runtime=disk_cleanup.CleanupRuntime(
                    runtime.now, runtime.read, placement.cleanup_call, runtime.toast))
        self.assertTrue((self.state / 'disk-cleanup.lock').exists())

    @integration
    def test_composed_worker_reaps_descendants_after_success_failure_and_inner_timeout(self):
        # Child inherits the worker's group/job; its stdio does not delay parent exit.
        # The finalizer is only a hermetic test safety net, never the asserted cleanup.
        execute = subprocess.run
        for mode in ('success', 'failure', 'worker-failure', 'timeout'):
            observations = ('without-readiness', 'after-readiness') if mode == 'timeout' else ('default',)
            for observation in observations:
                with self.subTest(mode=mode, observation=observation):
                    tag = mode if observation == 'default' else mode + '-' + observation
                    pidfile = self.root / (tag + '.pid')
                    readyfile = self.root / (tag + '.ready')
                    script = self.root / (mode + '.py')
                    script.write_text('import subprocess,sys,time\nfrom pathlib import Path\n' +
                        ('time.sleep(.8)\n' if mode == 'timeout' else '') +
                        'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"], '
                        'stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n'
                        'Path(sys.argv[1]).write_text(str(p.pid))\n'
                        'Path(sys.argv[2]).write_text("ready")\n' +
                        ('time.sleep(60)\n' if mode == 'timeout' else 'sys.exit(' + ('7' if mode == 'failure' else '0') + ')\n'))
                    request = self.request(3)
                    request['paths'] = dict(temp_roots=[], report_roots=[], caches=[dict(
                        argv=[sys.executable, str(script), str(pidfile), str(readyfile)],
                        cwd=str(self.root), placement=self.side)])

                    def bounded(argv, **kwargs):
                        # Shorten only the cache's timeout in the otherwise real worker.
                        argv = list(argv)
                        if mode == 'worker-failure' and 'cleanup_worker' in ' '.join(map(str, argv)):
                            i = argv.index('-c') + 1
                            crash = ('from drover import disk_side\n_serve = disk_side.serve\n'
                                     'def crash(request):\n _serve(request)\n raise RuntimeError("worker crash")\n'
                                     'disk_side.serve = crash\n')
                            argv[i] = argv[i].replace('from drover.placement import cleanup_worker;',
                                                      'exec(' + repr(crash) + ');from drover.placement import cleanup_worker;')
                        if mode == 'timeout' and 'cleanup_worker' in ' '.join(map(str, argv)):
                            i = argv.index('-c') + 1
                            if observation == 'without-readiness':
                                # The controlled 0.8s startup delay exceeds the old 0.4s window.
                                argv[i] = ('import subprocess; _run=subprocess.run; '
                                    'subprocess.run=lambda *a,**k: _run(*a,**{**k,"timeout":.4 if k.get("timeout")==120 else k.get("timeout")}); ' + argv[i])
                            else:
                                ready_code = f'''import subprocess,time
from pathlib import Path
_run = subprocess.run
_ready = Path({str(readyfile)!r})
def _bounded_cache_run(*args, **kwargs):
    if kwargs.get("timeout") != 120:
        return _run(*args, **kwargs)
    kwargs.pop("timeout")
    kwargs.pop("capture_output", None)
    kwargs["stdout"] = kwargs["stderr"] = subprocess.DEVNULL
    child = subprocess.Popen(*args, **kwargs)
    try:
        deadline = time.monotonic() + 5
        while not _ready.exists():
            if child.poll() is not None:
                return subprocess.CompletedProcess(args[0], child.returncode, "", "")
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(args[0], 5)
            time.sleep(.01)
        returncode = child.wait(timeout=.4)
        return subprocess.CompletedProcess(args[0], returncode, "", "")
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
subprocess.run = _bounded_cache_run
'''
                                argv[i] = 'exec(' + repr(ready_code) + ');' + argv[i]
                        return execute(argv, **kwargs)

                    pid = None
                    try:
                        with patch.object(subprocess, 'run', side_effect=bounded), \
                                patch.dict(os.environ, {'WSL_DISTRO_NAME': ''}):
                            result = placement.cleanup_call(self.side, request)
                        if observation == 'without-readiness':
                            self.assertFalse(pidfile.exists(), result)
                            self.assertTrue(result['errors'], result)
                            continue
                        self.assertTrue(pidfile.exists(), result)
                        pid = int(pidfile.read_text())
                        if mode == 'timeout':
                            self.assertTrue(result['errors'], result)
                        if self.side == 'windows':
                            alive = any(p == pid for p, *_ in placement.process_cmdlines())
                        else:
                            stat = Path('/proc') / str(pid) / 'stat'
                            alive = stat.exists() and stat.read_text().rsplit(')', 1)[1].split()[0] != 'Z'
                        self.assertFalse(alive, (mode, pid, result))
                    finally:
                        if pidfile.exists():
                            pid = int(pidfile.read_text())
                            if self.side == 'windows':
                                execute(['taskkill.exe', '/PID', str(pid), '/T', '/F'], capture_output=True)
                            else:
                                try:
                                    os.kill(pid, 9)
                                except ProcessLookupError:
                                    pass


class RepositoryConfigContract(DiskCase):
    @integration
    def test_git_config_grammar_protects_repository_evidence(self):
        from drover.disk_paths import repository_root
        for index, text in enumerate(('[core]\n bare = true\n',
                '[core] # section comment\n bare = true\n',
                '\ufeff[CoRe]\n BaRe = true\n', '[include]\n path = elsewhere\n',
                '[core]\n bare = "unterminated')):
            with self.subTest(config=text):
                root = self.root / str(index)
                self.file(root / 'config', text)
                self.assertTrue(repository_root(root))
