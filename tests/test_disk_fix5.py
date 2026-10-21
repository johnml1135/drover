"""Fifth cleanup review: startup, user visibility and bounded inventories."""
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from drover import config, disk_cleanup, disk_state, placement
from tests.disk_fixture import DiskCase
from tests.test_disk_worktrees import WorktreeCase


class Startup(DiskCase):
    @unittest.skipIf(os.name == 'nt', 'POSIX session semantics')
    def test_composed_worker_already_session_leader(self):
        execute = subprocess.run
        def session(argv, **kw):
            if 'cleanup_worker' in ' '.join(map(str, argv)):
                kw['start_new_session'] = True  # wsl.exe already does this
                kw['env'] = dict(kw['env'], WSL_DISTRO_NAME='')
            return execute(argv, **kw)
        pidfile = self.root / 'descendant.pid'
        command = ('import subprocess,sys;from pathlib import Path;'
                   'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"],'
                   'stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);'
                   'Path(sys.argv[1]).write_text(str(p.pid))')
        request = dict(stage=3, apply=True, now=self.now, state_dir=str(self.state),
                       repos=[], tasks={}, drive=dict(path=str(self.root)), paths=dict(caches=[dict(
                           argv=[sys.executable, '-c', command, str(pidfile)],
                           cwd=str(self.temp_root), placement='wsl')]))
        with patch.object(subprocess, 'run', side_effect=session):
            result = placement.cleanup_call('wsl', request)
        self.assertFalse(result['errors'], result)
        self.assertTrue(pidfile.exists(), 'the configured command really executed')
        pid = int(pidfile.read_text())
        self.assertFalse(any(p == pid and argv for p, argv in placement.process_cmdlines()),
                         'the existing session group must be drained')

    @unittest.skipIf(os.name == 'nt', 'POSIX session semantics')
    def test_startup_failures_are_reaped_and_release_lock_with_report(self):
        execute = subprocess.run
        rt = self.runtime()
        for mode in ('import', 'setsid'):
            with self.subTest(mode=mode):
                def fail(argv, **kw):
                    if 'cleanup_worker' in ' '.join(map(str, argv)):
                        argv = list(argv)
                        i = argv.index('-c') + 1
                        prefix = ('raise ImportError("startup fixture")\n' if mode == 'import' else
                                  'import os,errno\nos.setsid=lambda: (_ for _ in ()).throw(PermissionError(errno.EPERM,"fixture"))\n')
                        argv[i] = prefix + argv[i]
                    return execute(argv, **kw)
                with patch.object(subprocess, 'run', side_effect=fail), \
                        patch.dict(os.environ, {'WSL_DISTRO_NAME': ''}):
                    result = disk_cleanup.run(self.cfg, runtime=disk_cleanup.CleanupRuntime(
                        rt.now, rt.read, placement.cleanup_call, rt.toast))
                self.assertFalse((self.state / 'disk-cleanup.lock').exists())
                errors = [e for e in result['errors'] if e.get('reason_code') == 'worker_error']
                self.assertTrue(errors, result)
                self.assertTrue(all(e['termination_confirmed'] for e in errors))
                self.assertTrue(all(e['work_started'] is False for e in errors))
                self.assertIn('worker_error', (self.state / 'disk' / 'last-report.json').read_text())

    @unittest.skipIf(os.name == 'nt', 'executes the composed WSL proof locally')
    def test_remote_startup_executes_its_composed_absence_proof(self):
        self.cfg['paths']['wsl_distro'] = 'Fixture'
        execute = subprocess.run
        observed = []
        def locally(argv, **kw):
            observed.append(argv)
            # Only transport is replaced; execute the actual composed Python code.
            i = argv.index('python3')
            argv = [sys.executable, *argv[i + 1:]]
            if 'cleanup_worker' in ' '.join(argv):
                j = argv.index('-c') + 1
                argv[j] = 'raise ImportError("startup fixture")\n' + argv[j]
            return execute(argv, **kw)
        request = dict(stage=1, apply=False, now=self.now, state_dir=str(self.state),
                       repos=[], tasks={}, drive=dict(path=str(self.root)), paths={})
        with patch.object(placement, 'IS_WINDOWS', True), \
                patch.object(subprocess, 'run', side_effect=locally), \
                disk_state.CleanupLock(self.state):
            result = placement.cleanup_call('wsl', request)
        self.assertEqual(len(observed), 2)
        self.assertTrue(result['errors'][0]['termination_confirmed'])
        self.assertFalse((self.state / 'disk-cleanup.lock').exists())

    @unittest.skipIf(os.name == 'nt', 'reads /proc')
    def test_startup_proof_skips_unreadable_environ_but_keeps_argv_token_and_unknown_errors(self):
        token = 'tok-' + os.urandom(4).hex()
        real = Path.read_bytes

        def proof(environ_error):
            def read_bytes(path):
                if path.name == 'environ':
                    raise environ_error
                return real(path)
            sys.argv = ['proof', token]
            with patch.object(Path, 'read_bytes', read_bytes):
                try:
                    exec(placement._CLEANUP_STARTUP_PROOF, {'__name__': 'proof'})
                except SystemExit as stop:
                    return stop.code
            return 0

        saved = sys.argv
        # Unreadable environ without the token in argv (systemd, sd-pam): not Drover's own.
        self.assertEqual(proof(PermissionError(errno.EACCES, 'denied')), 0)
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)', token])
        try:
            self.assertEqual(proof(PermissionError(errno.EACCES, 'denied')), 1)  # argv token still wins
        finally:
            child.kill()
            child.wait()
            sys.argv = saved
        other = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            self.assertEqual(proof(OSError(errno.EIO, 'unknown')), 2)  # non-EACCES evidence fails closed
        finally:
            other.kill()
            other.wait()
            sys.argv = saved

    @unittest.skipIf(os.name == 'nt', 'reads /proc')
    def test_startup_proof_walks_ancestry_when_environ_is_unreadable(self):
        token = 'tok-' + os.urandom(4).hex()
        real_bytes, real_text, real_iterdir = Path.read_bytes, Path.read_text, Path.iterdir

        def proof(only_pid, stat_error_pid=None):
            def read_bytes(path):
                if path.name == 'environ':
                    raise PermissionError(errno.EACCES, 'denied')
                return real_bytes(path)
            def read_text(path, *a, **k):
                if path.name == 'stat' and path.parent.name == str(stat_error_pid):
                    raise OSError(errno.EIO, 'unknown')
                return real_text(path, *a, **k)
            def iterdir(path):  # scan only the process under test, so only the ancestry walk can reach its parent
                return iter([Path('/proc') / str(only_pid)]) if str(path) == '/proc' else real_iterdir(path)
            saved = sys.argv
            sys.argv = ['proof', token]
            try:
                with patch.object(Path, 'read_bytes', read_bytes), patch.object(Path, 'read_text', read_text), \
                        patch.object(Path, 'iterdir', iterdir):
                    exec(placement._CLEANUP_STARTUP_PROOF, {'__name__': 'proof'})
            except SystemExit as stop:
                return stop.code
            finally:
                sys.argv = saved
            return 0

        # The parent carries the token in argv; its child (no token, unreadable environ) was started by Drover.
        def family(*extra):
            parent = subprocess.Popen([sys.executable, '-c', 'import subprocess, sys, time\n'
                                       'c = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])\n'
                                       'print(c.pid, flush=True); time.sleep(30)', *extra], stdout=subprocess.PIPE, text=True)
            return parent, int(parent.stdout.readline())
        parent, child_pid = family(token)
        try:
            self.assertEqual(proof(child_pid), 1)
        finally:
            parent.kill()
            parent.wait()
        # A token-less parent whose stat cannot be read leaves the chain unknown: fail closed.
        parent, child_pid = family()
        try:
            self.assertEqual(proof(child_pid), 0)
            self.assertEqual(proof(child_pid, stat_error_pid=parent.pid), 2)
        finally:
            parent.kill()
            parent.wait()
        # No Drover ancestor anywhere: skipped.
        lone = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            self.assertEqual(proof(lone.pid), 0)
        finally:
            lone.kill()
            lone.wait()

    def test_remote_startup_requires_wsl_process_absence_before_unlock(self):
        self.cfg['paths']['wsl_distro'] = 'Fixture'
        for absent in (False, True):
            with self.subTest(absent=absent):
                scans = []
                def execute(argv, **kw):
                    if 'cleanup_worker' in ' '.join(argv):
                        return subprocess.CompletedProcess(argv, 1, '', 'startup fixture')
                    scans.append(argv)
                    return subprocess.CompletedProcess(argv, 0 if absent else 1, '', '')
                request = dict(stage=1, apply=False, now=self.now, state_dir=str(self.state),
                               repos=[], tasks={}, drive=dict(path=str(self.root)), paths={})
                with patch.object(placement, 'IS_WINDOWS', True), \
                        patch.object(subprocess, 'run', side_effect=execute):
                    if absent:
                        with disk_state.CleanupLock(self.state):
                            result = placement.cleanup_call('wsl', request)
                        self.assertEqual(result['errors'][0]['reason_code'], 'worker_error')
                        self.assertFalse((self.state / 'disk-cleanup.lock').exists())
                    else:
                        with self.assertRaises(placement.CleanupWorkerError):
                            with disk_state.CleanupLock(self.state):
                                placement.cleanup_call('wsl', request)
                        self.assertTrue((self.state / 'disk-cleanup.lock').exists())
                        # This simulated lock belongs solely to the temp fixture.
                        import shutil
                        shutil.rmtree(self.state / 'disk-cleanup.lock')
                self.assertTrue(scans, 'wsl.exe exit alone is not remote termination proof')


class UserScope(unittest.TestCase):
    @unittest.skipIf(os.name == 'nt', 'WSL process ownership')
    def test_wsl_status_uid_scopes_unreadable_cwd_but_keeps_named_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            entry = proc / '123456789'
            entry.mkdir()
            (entry / 'cmdline').write_bytes(b'python\0/candidate/worker.py\0')
            for same_user in (False, True):
                with self.subTest(same_user=same_user):
                    (entry / 'status').write_text(f'Name:\tfixture\nUid:\t{os.getuid() if same_user else os.getuid()+1}\t0\t0\t0\n')
                    proof = placement.cleanup_processes(proc_root=proc, windows=False)
                    self.assertTrue(proof.busy('/candidate'))
                    self.assertEqual(proof.busy('/unrelated'), same_user)
                    self.assertEqual(proof.out_of_scope, 0 if same_user else 1)
                    if same_user:
                        self.assertEqual(proof.unreadable[0]['pid'], 123456789)
            # exe alone is evidence even if argv is unreadable/empty.
            (entry / 'cmdline').write_bytes(b'')
            (entry / 'status').write_text(f'Uid:\t{os.getuid()+1}\n')
            target = proc / 'named' / 'tool'
            target.parent.mkdir()
            target.touch()
            (entry / 'exe').symlink_to(target)
            proof = placement.cleanup_processes(proc_root=proc, windows=False)
            self.assertTrue(proof.busy(str(target.parent)))
            self.assertFalse(proof.busy('/unrelated'))

    def test_windows_cim_owner_sid_scopes_cwd_and_preserves_exe_evidence(self):
        rows = [dict(ProcessId=123456789, Name='fixture', ExecutablePath=r'H:\candidate\tool.exe',
                     CommandLine=None, OwnerSid='other')]
        done = subprocess.CompletedProcess([], 0, json.dumps(dict(sid='invoker', processes=rows)), '')
        for same_user in (False, True):
            rows[0]['OwnerSid'] = 'invoker' if same_user else 'other'
            done.stdout = json.dumps(dict(sid='invoker', processes=rows))
            with self.subTest(same_user=same_user), \
                    patch.object(subprocess, 'run', return_value=done) as query, \
                    patch.object(placement, '_windows_cwd', return_value=None):
                proof = placement.cleanup_processes(windows=True)
            self.assertIn('OpenProcessToken', query.call_args.args[0][-1])  # one batched pass, owners from tokens
            self.assertTrue(proof.busy(r'H:\candidate'))
            self.assertEqual(proof.busy(r'H:\unrelated'), same_user)
            self.assertEqual(proof.out_of_scope, 0 if same_user else 1)


class BoundedReport(WorktreeCase):
    def test_large_tree_is_summarized_and_inventoried_once_across_stages(self):
        wt = self.worktree('large')
        folder = wt / 'generated'
        folder.mkdir()
        # Real synthetic tree, with long paths to expose per-file report growth.
        for i in range(6000):
            (folder / (f'{i:06}-' + 'x' * 110)).write_bytes(b'x' * (i % 100 + 1))
        scans = []
        iterdir = Path.iterdir
        def counted(path):
            if path == folder:
                scans.append(str(path))
            return iterdir(path)
        with patch.object(Path, 'iterdir', counted):
            result = self.cleanup()
        rows = [r for r in result['items'] if r.get('reason_code') == 'untracked_or_ignored' and r['path'] == str(wt)]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['files'], 6000)
        self.assertEqual(rows[0]['bytes'], sum(i % 100 + 1 for i in range(6000)))
        self.assertLessEqual(len(rows[0]['largest']), 20)
        self.assertEqual(len(scans), 1, 'one inventory pass; Git ignored-directory expansion must not rescan')
        self.assertLess((self.state / 'disk' / 'last-report.json').stat().st_size, 5_000_000)
        # Machine scale: 356 worktrees / 829k leaves, bounded output per root.
        self.assertLess(len(json.dumps(rows[0])) * 356, 5_000_000)

    def test_machine_scale_last_report_caps_samples_without_losing_totals(self):
        count = 829_000
        def worker(side, request):
            rows = []
            if side == self.side and request['stage'] == 6:
                for index in range(356):
                    root = str(self.root / ('tree-' + str(index)))
                    largest = [dict(path=root + '/' + 'long-component/' * 25 + str(n), bytes=n) for n in range(20)]
                    for reason in ('worktree_inventory', 'untracked_or_ignored'):
                        rows.append(dict(path=root, action='keep', reason_code=reason,
                                         files=count // 356, bytes=424 * 1024**3 // 356,
                                         largest=largest, owner=None, age_s=0))
            return dict(items=rows, errors=[], reclaimed_bytes=0)
        result = self.run_cleanup(handler=worker)
        self.assertLess((self.state / 'disk' / 'last-report.json').stat().st_size, 5_000_000)
        self.assertEqual(sum(r['files'] for r in result['items'] if r['reason_code'] == 'untracked_or_ignored'), count // 356 * 356)
        self.assertTrue(all(len(r['largest']) <= 20 for r in result['items']))

    def test_retained_reasons_have_counts_and_bounded_samples(self):
        for i in range(60):
            self.file(self.temp_root / f'retained-{i}')
        self.busy_paths.add(str(self.temp_root))
        result = self.cleanup()
        rows = [r for r in result['items'] if r.get('reason_code') == 'process_busy']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['count'], 60)
        self.assertLessEqual(len(rows[0]['sample']), 20)

    def test_process_scope_diagnostic_is_reported_once_per_side(self):
        class Proof:
            out_of_scope = 172
            unreadable = [dict(pid=77, name='fixture')]
            unknown = True
            reason = 'process visibility unavailable'
            def busy(self, path):
                return True
        rt = self.runtime(real=True)
        with patch.object(placement, 'cleanup_processes', return_value=Proof()), \
                patch.object(placement, 'physical_disk_path', return_value=str(self.root)):
            result = disk_cleanup.run(self.cfg, runtime=rt)
        for reason in ('process_out_of_scope', 'process_visibility_unavailable'):
            rows = [r for r in result['items'] if r.get('reason_code') == reason]
            self.assertEqual(len(rows), 1, result)
            self.assertEqual(rows[0]['count'], 172 if reason == 'process_out_of_scope' else 1)

    def test_excluded_repo_names_exact_owner_action(self):
        (self.repos_dir / 'alpha.toml').write_text(config.dumps(dict(
            name='alpha', repo=str(self.r.repo), worktrees=str(self.trees),
            scripts=dict(test='.drover/scripts/test.ps1'))))
        # Dirty repo edits cannot repair machine policy rejected against HEAD.
        entry = self.r.repo / '.drover' / 'config.toml'
        entry.write_text(entry.read_text() + '\n# uncommitted migration preparation\n')
        result = self.cleanup()
        row = next(e for e in result['errors'] if e.get('reason_code') == 'excluded_repo')
        self.assertEqual(row['owner_action'], 'commit .drover/config.toml, then drover config migrate-machine alpha')


class Timeout(DiskCase):
    def test_configured_cap_is_per_worker(self):
        self.assertEqual(config.validate({})['disk_cleanup']['worker_timeout_s'], 1800)
        for value in (0, -1, True, float('inf'), '10'):
            with self.assertRaises(config.ConfigError):
                config.validate(dict(disk_cleanup=dict(worker_timeout_s=value)))
        self.assertEqual(config.validate(dict(disk_cleanup=dict(worker_timeout_s=2400)))['disk_cleanup']['worker_timeout_s'], 2400)
        calls = []
        def execute(argv, **kw):
            request = json.loads(kw['input'])
            disk_state.save_file(self.state, 'worker-' + request['_worker_id'] + '.json',
                                 dict(pid=123456789, run_id=request['_worker_id']))
            calls.append(kw['timeout'])
            return subprocess.CompletedProcess(argv, 0, '{"items":[],"errors":[],"reclaimed_bytes":0}', '')
        request = dict(stage=1, apply=False, state_dir=str(self.state), repos=[], paths=dict(worker_timeout_s=2345))
        with patch.object(subprocess, 'run', side_effect=execute), \
                patch.object(placement, 'recover_ci_children', return_value=True):
            placement.cleanup_call('wsl', request)
            placement.cleanup_call('wsl', request)
        self.assertEqual(calls, [2345, 2345])


class VisibilityReport(DiskCase):
    def test_foreign_process_count_is_once_per_run_across_drives_and_workers(self):
        self.cfg['trackers'].append(dict(self.cfg['trackers'][0], name='second-drive'))
        def worker(side, request):
            return dict(items=[dict(action='report', reason_code='process_out_of_scope',
                                    count=172 if side == 'windows' else 10)],
                        errors=[], reclaimed_bytes=0)
        result = self.run_cleanup(handler=worker)
        rows = [i for i in result['items'] if i['reason_code'] == 'process_out_of_scope']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['count'], 182)
        self.assertEqual(rows[0]['placements'], dict(windows=172, wsl=10))

    def test_partial_failure_report_also_counts_foreign_processes_once(self):
        def worker(side, request):
            if request['stage'] == 4:
                raise placement.CleanupWorkerError('unconfirmed fixture worker')
            return dict(items=[dict(action='report', reason_code='process_out_of_scope', count=10)],
                        errors=[], reclaimed_bytes=0)
        with self.assertRaises(placement.CleanupWorkerError):
            self.run_cleanup(handler=worker)
        saved = json.loads((self.state / 'disk' / 'last-report.json').read_text())
        rows = [i for i in saved['items'] if i.get('reason_code') == 'process_out_of_scope']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['count'], 20)
        self.assertTrue((self.state / 'disk-cleanup.lock').exists())
