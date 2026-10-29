"""Immutable exports and the separate candidate/recovery trust boundaries."""
import hashlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import stat
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from functools import lru_cache
from hub.runtime import ReleaseError, prepare_release, check_eligibility, check_recovery, child_command
from hub.runtime import _blob_digests
from tests.gitfix import sh, _remove_tree


from tests.test_hub_runtime import INSTANCE, digest, git, setUpModule, _RuntimeBase


class RuntimePathTests(_RuntimeBase):
    def test_interpreter_and_wsl_reachability_required(self):
        release = self.baseline()
        self.python.write_bytes(b'changed interpreter')
        self.assertEqual(check_eligibility(self.enrolled, self.a, self.a, 4)['reasons'], ['interpreter-change'])
        self.assertEqual(check_recovery(self.enrolled, release, self.receipt(release))['reasons'], ['interpreter-change'])
        self.python.write_bytes(b'existing interpreter')
        for probe in (None, lambda _: False, lambda _: True):
            expected = probe is not None and probe(Path(release['release_path']))
            self.assertEqual(check_recovery(self.enrolled, release, self.receipt(release),
                                            wsl_probe=probe, wsl_live_run_ids=[612])['eligible'], expected)
            self.assertEqual(check_eligibility(self.enrolled, self.a, self.a, 4,
                                               wsl_probe=probe, wsl_live_run_ids=[612])['eligible'], expected)


    def test_new_candidate_checks_existing_parent_then_exported_wsl_path(self):
        self.baseline()
        self.write('hub/app.py', 'candidate\n')
        b = self.commit()
        destination = self.release_root / b
        probes = []

        def reachable(path):
            probes.append(path)
            return path.is_dir()

        result = check_eligibility(self.enrolled, self.a, b, 4,
                                   wsl_probe=reachable, wsl_live_run_ids=[612])
        self.assertEqual(result, {'eligible': True, 'reasons': []})
        self.assertEqual(probes, [self.release_root])
        self.assertFalse(destination.exists())
        probes.clear()
        release = prepare_release(self.source, b, self.release_root, self.enrolled,
                                  wsl_probe=reachable, wsl_live_run_ids=[612])
        self.assertEqual(probes, [self.release_root, destination])
        self.assertTrue((destination / 'manifest.json').is_file())
        probes.clear()
        self.assertTrue(check_eligibility(self.enrolled, self.a, b, 4,
                                         wsl_probe=reachable, wsl_live_run_ids=[612])['eligible'])
        self.assertEqual(probes, [destination])
        probes.clear()
        self.assertEqual(prepare_release(self.source, b, self.release_root, self.enrolled,
                                         wsl_probe=reachable, wsl_live_run_ids=[612]), release)
        self.assertEqual(probes, [destination, destination])


    def test_first_enrollment_checks_existing_ancestor_then_exported_wsl_path(self):
        probes = []

        def reachable(path):
            probes.append(path)
            return path.is_dir()

        self.assertFalse(self.release_root.exists())
        release = prepare_release(
            self.source, self.a, self.release_root, self.enrolled,
            first_enrollment=True,
            baseline_probe=lambda *_: {'ledger_schema': 4, 'required_schema': 4},
            wsl_probe=reachable, wsl_live_run_ids=[612])
        destination = Path(release['release_path'])
        self.assertEqual(probes, [self.root, destination])
        self.assertTrue((destination / 'manifest.json').is_file())


    def test_unreachable_wsl_parent_refuses_before_export(self):
        self.baseline()
        self.write('hub/app.py', 'candidate\n')
        b = self.commit()
        probes = []

        def unreachable(path):
            probes.append(path)
            return False

        self.assertEqual(check_eligibility(self.enrolled, self.a, b, 4,
                                          wsl_probe=unreachable, wsl_live_run_ids=[612])['reasons'],
                         ['wsl-release-unreachable'])
        with self.assertRaisesRegex(ReleaseError, 'wsl-release-unreachable'):
            prepare_release(self.source, b, self.release_root, self.enrolled,
                            git=lambda *_: self.fail('unreachable parent must refuse before Git export'),
                            wsl_probe=unreachable, wsl_live_run_ids=[612])
        self.assertEqual(probes, [self.release_root, self.release_root])
        self.assertEqual([path.name for path in self.release_root.iterdir()], [self.a])


    def test_reachable_parent_cannot_authorize_unreachable_exported_wsl_path(self):
        self.baseline()
        self.write('hub/app.py', 'candidate\n')
        b = self.commit()
        destination = self.release_root / b
        probes = []

        def parent_only(path):
            self.assertTrue(path.is_dir())
            probes.append(path)
            return path == self.release_root

        for attempt in ('new export', 'retained reuse'):
            with self.subTest(attempt=attempt):
                probes.clear()
                with self.assertRaisesRegex(ReleaseError, 'wsl-release-unreachable'):
                    prepare_release(self.source, b, self.release_root, self.enrolled,
                                    wsl_probe=parent_only, wsl_live_run_ids=[612])
                self.assertEqual(probes, [self.release_root, destination] if attempt == 'new export'
                                 else [destination])
                self.assertTrue((destination / 'manifest.json').is_file())
        self.assertEqual(check_eligibility(self.enrolled, self.a, b, 4,
                                          wsl_probe=parent_only, wsl_live_run_ids=[612])['reasons'],
                         ['wsl-release-unreachable'])


    def test_recovery_compares_both_paths_with_native_case_rules(self):
        release = self.baseline()
        # Exercise Windows normcase behavior on every platform.
        with patch('hub.runtime.os.path.normcase', side_effect=str.casefold):
            self.assertTrue(check_recovery(self.enrolled, release, self.receipt(release))['eligible'])


    def test_child_command_pins_imports_and_paths_with_spaces(self):
        release = self.baseline()
        argv, env, cwd = child_command(self.enrolled, release, 12, INSTANCE, env={
            'PYTHONPATH': str(self.source), 'PythonPath': 'other source',
            'PYTHONHOME': 'foreign python', 'DROVER_RELOAD_LEDGER': 'foreign ledger',
            'drover_overlay': 'foreign overlay', 'drover_home': 'foreign home',
            'drover_repo_configs_dir': 'foreign configs', 'pythonsafepath': '0',
            'OWNER_SETTING': 'preserved'})
        self.assertEqual(argv, [str(self.python), '-P', '-m', 'hub'])
        self.assertEqual(cwd, release['release_path'])
        self.assertEqual(env['PYTHONPATH'], cwd)
        self.assertNotIn('PythonPath', env)
        self.assertNotIn('PYTHONHOME', env)
        for key in ('drover_overlay', 'drover_home', 'drover_repo_configs_dir', 'pythonsafepath'):
            self.assertNotIn(key, env)
        self.assertEqual(env['PYTHONSAFEPATH'], '1')
        self.assertEqual(env['PYTHONDONTWRITEBYTECODE'], '1')
        self.assertEqual(env['OWNER_SETTING'], 'preserved')
        self.assertNotIn('DROVER_OVERLAY', env)
        self.assertNotIn('DROVER_RELOAD_OVERLAY', env)
        self.assertEqual(env['DROVER_HOME'], self.enrolled['paths']['config_home'])
        self.assertEqual(env['DROVER_REPO_CONFIGS_DIR'], self.enrolled['paths']['repo_config'])
        for name, value in self.enrolled['paths'].items():
            if name == 'overlay':
                continue
            self.assertEqual(env['DROVER_RELOAD_' + name.upper()], value)
        self.assertEqual(env['DROVER_RELOAD_REQUIRED_SCHEMA'], '4')


    def test_expanded_size_limit_refused_before_extraction(self):
        self.baseline()
        self.write('hub/app.py', 'B')
        b = self.commit()
        payload = bytearray(git(self.source, 'archive', '--format=zip', b))
        central = payload.index(b'PK\x01\x02')
        # Advertise a large expansion without allocating or extracting it.
        struct.pack_into('<I', payload, central + 24, 128 * 1024 * 1024 + 1)
        def adapter(source, *args):
            return bytes(payload) if args[0] == 'archive' else git(source, *args)
        with self.assertRaisesRegex(ReleaseError, 'archive-too-large'):
            prepare_release(self.source, b, self.release_root, self.enrolled, git=adapter)
        self.assertFalse((self.release_root / b).exists())


    def test_tracked_link_and_windows_case_collision_refused(self):
        self.baseline()
        # Inject Git tree records so this runs on Windows without symlink rights
        # or a case-sensitive volume, as well as on POSIX.
        original = git(self.source, 'ls-tree', '-r', '-z', '--full-tree', self.a)
        blob = git(self.source, 'rev-parse', self.a + ':hub/__main__.py').strip()
        for extra, reason in (
            (b'120000 blob ' + blob + b'\thub/link\0', 'archive-type'),
            (b'160000 commit ' + self.a.encode() + b'\tsubmodule\0', 'archive-type'),
            (b'100644 blob ' + blob + b'\tHub/other.py\0', 'archive-path-collision'),
        ):
            def adapter(source, *args):
                return original + extra if args[0] == 'ls-tree' else git(source, *args)
            with self.subTest(extra=extra), self.assertRaisesRegex(ReleaseError, reason):
                prepare_release(self.source, self.a, self.release_root, self.enrolled, git=adapter)


    def test_child_package_and_runner_import_roots_are_retained(self):
        self.write('drover/__init__.py', '')
        self.write('drover/launch.py', 'from pathlib import Path\nROOT=Path(__file__).resolve().parent.parent\n')
        self.write('hub/__main__.py',
                   'import json, os\nfrom pathlib import Path\nimport drover.launch\n'
                   'print(json.dumps({"hub":str(Path(__file__).resolve()),'
                   '"runner_root":str(drover.launch.ROOT),"overlay":"DROVER_OVERLAY" in os.environ,"reload_overlay":"DROVER_RELOAD_OVERLAY" in os.environ}))\n')
        self.a = self.commit()
        release = self.baseline()
        self.write('hub/__main__.py', 'raise RuntimeError("mutable source imported")\n')
        self.write('drover/launch.py', 'raise RuntimeError("mutable runner imported")\n')
        argv, env, cwd = child_command(self.enrolled, release, 12, INSTANCE,
                                       env={**os.environ, 'PYTHONPATH': str(self.source), 'DROVER_OVERLAY': 'obsolete', 'DROVER_RELOAD_OVERLAY': 'obsolete'})
        # The installed-interpreter argv is asserted above. Use the preinstalled
        # test Python to execute the same import flags against this fixture.
        process = subprocess.run([sys.executable, *argv[1:]], env=env, cwd=cwd,
                                 capture_output=True, text=True, check=True)
        report = json.loads(process.stdout)
        self.assertEqual(report['hub'], str(Path(cwd) / 'hub/__main__.py'))
        self.assertEqual(report['runner_root'], cwd)
        self.assertFalse(report['overlay'])
        self.assertFalse(report['reload_overlay'])


    def test_frozen_module_supports_package_and_sibling_imports_without_application_or_sqlite(self):
        bootstrap = self.root / 'frozen bootstrap'
        bootstrap.mkdir()
        repo = Path(__file__).resolve().parent.parent
        for name in ('runtime.py', 'reload_protocol.py'):
            shutil.copyfile(repo / 'hub' / name, bootstrap / name)
        guard = '''
import sys
class Guard:
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in ('drover', 'sqlite3', 'fastapi', 'uvicorn'):
            raise AssertionError('forbidden import: ' + fullname)
sys.meta_path.insert(0, Guard())
sys.path.insert(0, sys.argv[1])
'''
        for directory, module in ((repo, 'hub.runtime'), (bootstrap, 'runtime')):
            script = guard + '\nimport importlib\nm = importlib.import_module(sys.argv[2])\nassert callable(m.check_recovery)\n'
            result = subprocess.run([sys.executable, '-I', '-B', '-c', script, str(directory), module],
                                    cwd=bootstrap, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
