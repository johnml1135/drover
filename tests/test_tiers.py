"""Tier selection partitions test IDs before execution and sharding."""
import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import tests.tiers as tiers

from tests.tiers import integration, select


class Selection(unittest.TestCase):
    def test_tiers_partition_mixed_methods_and_classes_without_losing_skips(self):
        class Mixed(unittest.TestCase):
            def test_fast(self):
                pass

            @integration
            def test_real(self):
                pass

            @unittest.skip('platform')
            def test_platform(self):
                pass

        @integration
        class Real(unittest.TestCase):
            def test_adapter(self):
                pass

        def suite():
            return unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(c) for c in (Mixed, Real))

        fast = list(select(suite(), 'default'))
        real = list(select(suite(), 'integration'))
        self.assertEqual([t._testMethodName for t in fast], ['test_fast', 'test_platform'])
        self.assertEqual([t._testMethodName for t in real], ['test_real', 'test_adapter'])
        self.assertTrue(getattr(fast[1].test_platform, '__unittest_skip__'))
        self.assertFalse({t.id() for t in fast} & {t.id() for t in real})
        self.assertEqual(len(fast) + len(real), suite().countTestCases())

    def test_unwrapped_policy_copy_stays_in_default(self):
        class Policy(unittest.TestCase):
            @integration
            def test_contract(self):
                pass
            test_policy = inspect.unwrap(test_contract)

        suite = unittest.defaultTestLoader.loadTestsFromTestCase(Policy)
        self.assertEqual([t._testMethodName for t in select(suite, 'default')], ['test_policy'])

    def test_import_errors_fail_in_either_tier(self):
        for tier in ('default', 'integration'):
            suite = unittest.defaultTestLoader.loadTestsFromName('tests.no_such_q12_module')
            result = unittest.TestResult()
            select(suite, tier).run(result)
            self.assertEqual(len(result.errors), 1)

    def test_named_method_shards_use_the_same_partition_as_modules(self):
        class Mixed(unittest.TestCase):
            def test_fast(self):
                pass

            @integration
            def test_real(self):
                pass

        module = SimpleNamespace(Mixed=Mixed)
        for tier, expected in (('default', 'test_fast'), ('integration', 'test_real')):
            with patch.dict(tiers.os.environ, {tiers.ENV: tier}):
                loader = tiers.TierLoader()
                whole = loader.loadTestsFromNames(['Mixed'], module)
                shards = loader.loadTestsFromNames(['Mixed.test_fast', 'Mixed.test_real'], module)
                self.assertEqual([t.id() for t in whole], [t.id() for t in shards])
                self.assertEqual([t._testMethodName for t in whole], [expected])
                self.assertEqual(loader.loadTestsFromNames(
                    ['Mixed.test_real' if tier == 'default' else 'Mixed.test_fast'], module).countTestCases(), 0)
