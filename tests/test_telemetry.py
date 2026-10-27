"""Telemetry defaults reach workers and CI without starting processes."""
import os
import runpy
import unittest
from pathlib import Path
from unittest.mock import patch

from drover import placement
from drover.launch import RunnerLauncher


class TelemetryTests(unittest.TestCase):
    def test_worker_environment_preserves_owner_values_and_wsl_bridge(self):
        values = {**placement.TELEMETRY_OPTOUT, 'DOTNET_NOLOGO': 'owner'}
        with patch.dict(os.environ, {'DOTNET_NOLOGO': 'owner', 'WSLENV': 'OTHER/p:DOTNET_NOLOGO/w'}, clear=True):
            runpy.run_path(placement.__file__)
            self.assertEqual({k: os.environ[k] for k in values}, values)
            self.assertIn('OTHER/p', os.environ['WSLENV'].split(':'))
            self.assertIn('DOTNET_NOLOGO', os.environ['WSLENV'].split(':'))
        with patch.dict(os.environ, values):
            launcher = RunnerLauncher(runs_dir='runs', drover_dir='repo', windows=False)
            spec = dict(run_id='1', nonce='n', cwd='/tmp', placement='windows', backend='codex')
            _, env = launcher.command(spec, Path('/tmp'), ['codex'])
        for name, value in values.items():
            self.assertEqual(env[name], value)
            self.assertIn(name, [item.split('/')[0] for item in env['WSLENV'].split(':')])

    def test_wsl_ci_command_passes_all_defaults_and_owner_values(self):
        with patch.dict(os.environ, {'DOTNET_NOLOGO': 'owner'}, clear=True):
            cmd, env = placement.ci_runner_command(['--', 'pwsh'], r'C:\repo', 'Ubuntu', windows=True)
        self.assertIsNone(env)
        self.assertEqual(cmd[cmd.index('--exec') + 1], '/usr/bin/env')
        for name, value in {**placement.TELEMETRY_OPTOUT, 'DOTNET_NOLOGO': 'owner'}.items():
            self.assertIn(f'{name}={value}', cmd)
