"""Executable discovery uses the machine PATH without running installed tools."""
import os
from contextlib import closing
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from drover.tool_discovery import DISCOVERY_SCRIPT, INVALID_PATH_REASON, parse_discovery_result


@unittest.skipUnless(os.name == "posix", "requires Bash and POSIX filesystem paths")
class ToolDiscovery(unittest.TestCase):
    def test_executable_entry_points_are_reported_without_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for tool in ('claude', 'codex', 'pwsh'):
                path = root / tool
                path.write_text('#!/bin/sh\nexit 99\n')
                path.chmod(0o755)
            result = subprocess.run([shutil.which('bash'), '-c', DISCOVERY_SCRIPT],
                                    env={'PATH': tmp, 'DROVER_WORKER_PATH': '/usr/bin:/bin'}, capture_output=True, text=True, check=True)
            found, errors = parse_discovery_result(result.stdout)
            self.assertEqual(found, {tool: str(root / tool) for tool in ('claude', 'codex', 'pwsh')})
            self.assertEqual(errors, {})

    def test_discovered_shim_runs_with_worker_path_and_login_only_interpreter_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good = root / 'claude'
            good.write_text('#!/usr/bin/env sh\nprintf worker-ok\n')
            good.chmod(0o755)
            interpreter = root / 'login-interpreter'
            interpreter.write_text('#!/bin/sh\nprintf login-only\n')
            interpreter.chmod(0o755)
            bad = root / 'codex'
            bad.write_text('#!/usr/bin/env login-interpreter\n')
            bad.chmod(0o755)
            native = root / 'pwsh'
            shutil.copyfile('/bin/true', native)
            native.chmod(0o755)
            worker_env = {'PATH': '/usr/bin:/bin'}
            result = subprocess.run(['/bin/bash', '-c', DISCOVERY_SCRIPT],
                                    env={'PATH': tmp, 'DROVER_WORKER_PATH': worker_env['PATH']},
                                    capture_output=True, text=True, check=True)
            found, _ = parse_discovery_result(result.stdout)
            self.assertNotIn('codex', found)
            self.assertEqual(set(found), {'claude', 'pwsh'})
            result = subprocess.run([found['claude']], env=worker_env, capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout, 'worker-ok')
            subprocess.run([found['pwsh']], env=worker_env, check=True)

    def test_missing_or_non_executable_entries_are_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'claude').write_text('not executable')
            result = subprocess.run([shutil.which('bash'), '-c', DISCOVERY_SCRIPT],
                                    env={'PATH': tmp, 'DROVER_WORKER_PATH': '/usr/bin:/bin'}, capture_output=True, text=True, check=True)
            self.assertEqual(parse_discovery_result(result.stdout), ({}, {}))

    def test_probe_output_validation_ignores_noise_and_foreign_paths(self):
        found, errors = parse_discovery_result('noise\nDROVER_TOOL|codex|/opt/bin/codex\n'
                                              'DROVER_TOOL|claude|/mnt/c/cli/claude\n'
                                              'DROVER_TOOL|other|/opt/bin/other\n')
        self.assertEqual(found, {'codex': '/opt/bin/codex'})
        self.assertEqual(errors, {'claude': INVALID_PATH_REASON})


class DiscoverySettings(unittest.TestCase):
    def test_discovery_preserves_explicit_machine_tool_path(self):
        from hub.services import Services
        from hub.settings import Settings
        from hub.store import Store
        with tempfile.TemporaryDirectory() as tmp, closing(Store(Path(tmp) / 'ledger.db')) as store:
            store.update_settings({'wsl_bin_codex': '/opt/native/codex'})
            service = Services(store, Settings(runs_dir=tmp, distro='Ubuntu'),
                               wsl=lambda *args: (0, 'DROVER_TOOL|codex|/usr/bin/codex\n'
                                                    'DROVER_TOOL|claude|/usr/bin/claude\n'))
            service.discover_tools()
            self.assertEqual(store.get_settings()['wsl_bin_codex'], '/opt/native/codex')
            self.assertEqual(store.get_settings()['wsl_bin_claude'], '/usr/bin/claude')
