"""Executable discovery uses the machine PATH without running installed tools."""
import os
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
                                    env={'PATH': tmp}, capture_output=True, text=True, check=True)
            found, errors = parse_discovery_result(result.stdout)
            self.assertEqual(found, {tool: str(root / tool) for tool in ('claude', 'codex', 'pwsh')})
            self.assertEqual(errors, {})

    def test_missing_or_non_executable_entries_are_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'claude').write_text('not executable')
            result = subprocess.run([shutil.which('bash'), '-c', DISCOVERY_SCRIPT],
                                    env={'PATH': tmp}, capture_output=True, text=True, check=True)
            self.assertEqual(parse_discovery_result(result.stdout), ({}, {}))

    def test_probe_output_validation_ignores_noise_and_foreign_paths(self):
        found, errors = parse_discovery_result('noise\nDROVER_TOOL|codex|/opt/bin/codex\n'
                                              'DROVER_TOOL|claude|/mnt/c/cli/claude\n'
                                              'DROVER_TOOL|other|/opt/bin/other\n')
        self.assertEqual(found, {'codex': '/opt/bin/codex'})
        self.assertEqual(errors, {'claude': INVALID_PATH_REASON})
