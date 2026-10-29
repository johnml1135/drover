"""Guards that only ever tighten: file size, real-process test count, default-tier purity.

Each ceiling is pinned at today's value. Lower it (delete the entry / reduce the number) when the code improves;
the tests fail when a pinned value is now loose, so the pins cannot rot, and nothing here lets a new offender in.
"""
import os
import subprocess
import time
import unittest
from unittest import mock
from pathlib import Path

import tests
from tests.tiers import MARKER, enabled

ROOT = Path(__file__).resolve().parents[1]
MAX_LINES = 500
INTEGRATION_TARGET = 20
INTEGRATION_CEILING = 93  # pinned today; owner target is INTEGRATION_TARGET

# Files over MAX_LINES, pinned at their current line counts (wc -l). Shrink-only: never add a file.
OVER = {
    "hub/reload_protocol.py": 571,
    "hub/runtime.py": 612,
    "tests/test_decide.py": 638,
    "tests/test_hub_api.py": 799,
    "tests/test_hub_ops.py": 559,
    "tests/test_hub_runtime.py": 766,
    "tests/test_importer.py": 507,
    "tests/test_landing.py": 754,
    "tests/test_launch_runner.py": 580,
    "tests/test_ledger.py": 754,
    "tests/test_notification_api.py": 637,
    "tests/test_runner.py": 848,
    "tests/test_waiting.py": 688,
    "tests/test_waiting_consumers.py": 657,
    "tests/test_wizard.py": 583,
}

IMPURE_CEILING = 56  # len(tests.IMPURE): default-tier modules still allowed to start processes or sleep


def sources():
    """.py/.ps1 files under the source folders, skipping dot dirs, caches, node_modules and virtualenvs."""
    for top in ('drover', 'hub', 'scripts', 'tests'):
        for folder, dirs, names in os.walk(ROOT / top):
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('__pycache__', 'node_modules')
                       and not (Path(folder, d, 'pyvenv.cfg')).exists()]
            for name in names:
                if name.endswith(('.py', '.ps1')):
                    yield Path(folder, name)


def integration_tests(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from integration_tests(test)
        elif getattr(type(test), MARKER, False) or getattr(getattr(test, test._testMethodName), MARKER, False):
            yield test


class FileSize(unittest.TestCase):
    def test_python_and_powershell_files_stay_small(self):
        lines = {p.relative_to(ROOT).as_posix(): p.read_bytes().count(b'\n') for p in sources()}
        print(f'\nfiles over 300 lines: {sum(n > 300 for n in lines.values())}')
        grown = {f: n for f, n in lines.items() if n > OVER.get(f, MAX_LINES)}
        self.assertFalse(grown, f'files over their limit ({MAX_LINES}, or the pinned count in OVER): {grown}')
        stale = [f for f, pinned in OVER.items() if lines.get(f, 0) <= MAX_LINES or lines[f] < pinned]
        self.assertFalse(stale, 'shrink-only: delete the OVER entry (now <= 500 lines) or lower its pinned count: '
                         + ', '.join(f'{f} is {lines.get(f, 0)}' for f in stale))


class RealProcessBudget(unittest.TestCase):
    def test_integration_tests_only_go_down(self):
        suite = unittest.TestLoader().discover(str(ROOT / 'tests'), top_level_dir=str(ROOT))
        count = len(list(integration_tests(suite)))
        print(f'\nintegration-tier tests: {count} (target {INTEGRATION_TARGET})')
        self.assertLessEqual(count, INTEGRATION_CEILING,
                             f'{count} integration tests exceed the ceiling {INTEGRATION_CEILING}; '
                             f'fake the process instead (target {INTEGRATION_TARGET})')
        self.assertGreaterEqual(count, INTEGRATION_CEILING,
                                f'shrink-only: lower INTEGRATION_CEILING to {count} (target {INTEGRATION_TARGET})')


@unittest.skipIf(enabled(), 'the purity guard applies to the default tier only')
class DefaultTierPurity(unittest.TestCase):
    def test_guard_blocks_processes_and_real_sleeps_outside_the_list(self):
        with mock.patch.object(tests, 'IMPURE', set()):
            with self.assertRaises(AssertionError):
                subprocess.run(['true'])
            with self.assertRaises(AssertionError):
                os.system('true')
            with self.assertRaises(AssertionError):
                time.sleep(0.1)
            time.sleep(0.001)  # a yield is not a real sleep
        del tests.VIOLATIONS[:]  # these were deliberate

    def test_impure_list_only_shrinks(self):
        n = len(tests.IMPURE)
        self.assertLessEqual(n, IMPURE_CEILING, 'do not add to tests.IMPURE: fake the process or sleep')
        self.assertGreaterEqual(n, IMPURE_CEILING, f'shrink-only: lower IMPURE_CEILING to {n}')
