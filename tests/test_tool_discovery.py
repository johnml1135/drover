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
    def test_discovered_pwsh_runs_composed_ci_with_non_login_path(self):
        import json
        import sys
        from drover import ci, pipeline, placement, repos
        from hub.services import Services
        from hub.settings import Settings
        from hub.store import Store
        from tests.gitfix import Repo

        fix = Repo()
        self.addCleanup(fix.cleanup)
        tools = fix.root / 'login-tools'
        tools.mkdir()
        worker_tools = fix.root / 'worker-tools'
        worker_tools.mkdir()
        (worker_tools / 'git').symlink_to(shutil.which('git'))
        (worker_tools / 'python3').symlink_to(sys.executable)
        pwsh = tools / 'pwsh'
        pwsh.write_text('#!/bin/sh\n'
                        '[ "$1" = -NoLogo ] && [ "$2" = -NoProfile ] && '
                        '[ "$3" = -NonInteractive ] && [ "$4" = -File ] || exit 91\n'
                        'exec /bin/sh "$5"\n')
        pwsh.chmod(0o755)
        fix.commit('scripts/check action.ps1', 'printf "discovered-script-ran\\n"\n')
        sha = fix.commit('drover.toml', '[scripts]\ntest="scripts/check action.ps1"\n'
                         '[gate]\ndocs_only=false\n')
        with closing(Store(fix.root / 'ledger.db')) as store:
            def probe(argv, timeout):
                result = subprocess.run(argv[argv.index('--exec') + 1:],
                                        env={'PATH': str(tools), 'DROVER_WORKER_PATH': '/usr/bin:/bin'},
                                        capture_output=True, text=True, check=True, timeout=timeout,
                                        executable='/bin/bash')
                return result.returncode, result.stdout

            service = Services(store, Settings(runs_dir=fix.root / 'runs'), wsl=probe)
            self.assertEqual(service.discover_tools()['pwsh'], str(pwsh))
            self.assertEqual(store.get_settings()['wsl_bin_pwsh'], str(pwsh))
            previous = placement.bind_settings(store.get_settings)
            self.addCleanup(placement.bind_settings, previous)
            prog = repos.load({'name': 'example', 'repo': str(fix.repo)})
            pipe = pipeline.Pipeline(None, repos_map={'example': prog}, runs_dir=fix.root / 'runs')
            plan = pipe.ci.prepare(prog, fix.repo, sha, 'discovered', platform='wsl')
            argv, receipt = plan['spec']['argv'], Path(plan['receipt'])
            result = subprocess.run([sys.executable, '-m', 'drover.runner', '--run-id', 'discovered',
                                     '--runs-dir', str(fix.root / 'runs'), '--cwd', str(fix.repo), '--', *argv],
                                    env={**os.environ, 'PATH': str(worker_tools)},
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            data = json.loads(receipt.read_text())
            self.assertTrue(data['passed'])
            self.assertEqual(data['steps'][0]['command'][0], str(pwsh))
            self.assertIn('discovered-script-ran', '\n'.join(data['log_tail']))

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
