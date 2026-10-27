"""scripts/test.py counts a module as skipped only when it skips itself at import."""
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

from tests.tiers import integration

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('drover_test_runner', ROOT / 'scripts' / 'test.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


@integration
class ModuleOutcome(unittest.TestCase):
    def outcome(self, source, tier='default'):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'probe_mod.py').write_text(source)
            env = {**os.environ, 'PYTHONPATH': tmp}
            env['DROVER_TESTS'] = tier
            return runner.run_module('probe_mod', env, 0, 60)

    def test_empty_tier_is_successful(self):
        r = self.outcome('import unittest\nclass T(unittest.TestCase):\n def test_x(self): self.fail("excluded")\n',
                         tier='integration')
        self.assertEqual((r['code'], r['ran']), (0, 0))

    def test_import_time_skip_is_a_skip(self):
        r = self.outcome('import unittest\nraise unittest.SkipTest("deps missing")\n')
        self.assertEqual((r['code'], r['ran'], r['skipped']), (0, 0, 1))

    def test_crash_that_mentions_skiptest_still_fails(self):
        r = self.outcome('import unittest\nprint("unittest.case.SkipTest: earlier")\nraise RuntimeError("boom")\n')
        self.assertNotEqual(r['code'], 0)

    def test_failing_test_fails(self):
        r = self.outcome('import unittest\nclass T(unittest.TestCase):\n def test_x(self): self.fail("no")\n')
        self.assertNotEqual(r['code'], 0)
        self.assertEqual(r['ran'], 1)


if __name__ == '__main__':
    unittest.main()
