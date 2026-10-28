"""Tests never read or write the owner's real ~/.drover: DROVER_HOME points at an empty temp directory, and the
configuration variables a developer may have set are removed."""
import atexit
import os
import shutil
import sys
import tempfile

from drover import config

_home = tempfile.mkdtemp(prefix="drover-test-home-")
atexit.register(shutil.rmtree, _home, ignore_errors=True)
os.environ["DROVER_HOME"] = _home
for _name in config.env_names():
    os.environ.pop(_name, None)
config.reset()

# Default-tier purity: a default-tier test may not start a process or sleep for real. The guard raises in the calling
# test; integration contracts are unaffected. Every violation is also recorded and fails the running test after it
# returns, so an `except Exception` cannot hide it. Best-effort: threads started outside a test module's frames,
# os.spawn*/fork and asyncio subprocesses are not covered. Shrink-only: tests/test_guards.py pins the size of IMPURE.
import subprocess
import time
import unittest

if "integration" not in os.environ.get("DROVER_TESTS", "").replace(",", " ").split():
    VIOLATIONS = []
    IMPURE = {
        "tests.test_allowance",
        "tests.test_artifact_records",
        "tests.test_config_explain",
        "tests.test_config_layout",
        "tests.test_config_migration",
        "tests.test_decide",
        "tests.test_decisions",
        "tests.test_delete_regressions",
        "tests.test_deploy_hub",
        "tests.test_disk_boundaries",
        "tests.test_disk_branch",
        "tests.test_disk_caches",
        "tests.test_disk_ci_admission",
        "tests.test_disk_fix6",
        "tests.test_disk_fix7",
        "tests.test_disk_fix8",
        "tests.test_disk_fixes",
        "tests.test_disk_lock_wrapper",
        "tests.test_disk_ownership",
        "tests.test_disk_policy",
        "tests.test_disk_report",
        "tests.test_disk_state",
        "tests.test_disk_trigger",
        "tests.test_disk_trim",
        "tests.test_disk_worktree_add",
        "tests.test_disk_worktrees",
        "tests.test_dispatcher",
        "tests.test_garden",
        "tests.test_garden_branch",
        "tests.test_hub_api",
        "tests.test_hub_dispatch",
        "tests.test_hub_ops",
        "tests.test_hub_runtime",
        "tests.test_hub_ui",
        "tests.test_landing",
        "tests.test_launch_runner",
        "tests.test_notification_acceptance",
        "tests.test_notification_fences",
        "tests.test_origin_sync",
        "tests.test_pipeline",
        "tests.test_placement",
        "tests.test_proc_scan",
        "tests.test_reload_protocol",
        "tests.test_repo_entry",
        "tests.test_repo_scopes",
        "tests.test_repos",
        "tests.test_resources",
        "tests.test_task_access",
        "tests.test_task_refresh",
        "tests.test_task_views",
        "tests.test_tool_discovery",
        "tests.test_tuner",
        "tests.test_type_yaml",
        "tests.test_usage",
        "tests.test_waiting_migration",
        "tests.test_wizard",
        "tests.test_wizard_scopes",
    }  # test modules still allowed to start processes or sleep (filled in below)
    _real_sleep = time.sleep

    def _caller_module():
        frame = sys._getframe(1)
        while frame is not None:
            name = frame.f_globals.get("__name__", "")
            if name.startswith("tests.test_"):
                return name
            frame = frame.f_back
        return None

    def _check(what):
        module = _caller_module()
        if module is not None and module not in IMPURE:
            message = f"default tier must not use {what} ({module}); mark the test integration or fake it"
            VIOLATIONS.append(message)
            raise AssertionError(message)

    _real_popen_init = subprocess.Popen.__init__

    def _popen_init(self, *args, **kwargs):
        _check("subprocess")
        _real_popen_init(self, *args, **kwargs)

    def _system(*args, **kwargs):
        _check("os.system")
        return _real_system(*args, **kwargs)

    def _sleep(seconds):
        if seconds >= 0.05:
            _check("a real sleep")
        _real_sleep(seconds)

    _real_system = os.system
    subprocess.Popen.__init__ = _popen_init
    os.system = _system
    time.sleep = _sleep

    _real_run = unittest.TestCase.run

    def _run(self, result=None):
        seen = len(VIOLATIONS)
        outcome = _real_run(self, result)
        if result is not None and len(VIOLATIONS) > seen:
            error = AssertionError("; ".join(VIOLATIONS[seen:]))
            result.addFailure(self, (AssertionError, error, None))
        return outcome

    unittest.TestCase.run = _run
