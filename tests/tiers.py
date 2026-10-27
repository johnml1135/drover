"""Disjoint test tiers: default policy tests and explicitly marked integration contracts.

scripts/test.py selects a tier before sharding and executes it through this module.
Plain unittest still skips integration contracts unless DROVER_TESTS=integration.
Landing gates explicitly run both tiers; neither tier includes the other.
"""
from functools import wraps
import os
import sys
import unittest

ENV = 'DROVER_TESTS'
MARKER = '__drover_integration__'


def enabled():
    return 'integration' in os.environ.get(ENV, '').replace(',', ' ').split()


def integration(obj):
    """Mark a function or TestCase class as an integration contract."""
    if isinstance(obj, type):
        marked = obj
    else:
        # Always wrap: inspect.unwrap can copy a contract's body into a fast
        # fixture without inheriting its tier, even when integration is enabled.
        @wraps(obj)
        def marked(*args, **kwargs):
            return obj(*args, **kwargs)
    marked = unittest.skipUnless(enabled(), f'integration tier: set {ENV}=integration')(marked)
    setattr(marked, MARKER, True)
    return marked


def select(suite, tier):
    """Flatten and select a suite, preserving discovery errors in either tier."""
    chosen = unittest.TestSuite()
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            chosen.addTests(select(test, tier))
            continue
        method = getattr(test, test._testMethodName)
        real = getattr(type(test), MARKER, False) or getattr(method, MARKER, False)
        if isinstance(test, unittest.loader._FailedTest) or real == (tier == 'integration'):
            chosen.addTest(test)
    return chosen


class TierLoader(unittest.TestLoader):
    def loadTestsFromNames(self, names, module=None):
        return select(super().loadTestsFromNames(names, module), 'integration' if enabled() else 'default')

    def discover(self, *args, **kwargs):
        return select(super().discover(*args, **kwargs), 'integration' if enabled() else 'default')


if __name__ == '__main__':
    program = unittest.main(module=None, testLoader=TierLoader(), exit=False)
    # A changed module may contain no tests in this tier (unittest 3.14 exits 5).
    sys.exit(0 if program.result.wasSuccessful() else 1)
