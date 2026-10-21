"""Test tiers.

The default tier is fast and hermetic enough to run on every edit.  The integration tier holds the tests that start
the real drover runner, real child processes, real timeouts or a real gate on a real repo; they check wiring the fast
tier fakes.  They are skipped unless the environment says `DROVER_TESTS=integration` (the pre-landing run, which
runs both tiers).  A test that changes tier still exists and still runs there; nothing is deleted.

    @integration                      # a test, a TestCase class, or a module-level pytestmark-like call
    class RealRunnerTests(unittest.TestCase): ...
"""
import os
import unittest

ENV = 'DROVER_TESTS'


def enabled():
    return 'integration' in os.environ.get(ENV, '').replace(',', ' ').split()


def integration(obj):
    """Skip `obj` (a test function or TestCase class) unless DROVER_TESTS=integration."""
    return unittest.skipUnless(enabled(), f'integration tier: set {ENV}=integration')(obj)
