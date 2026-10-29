from drover import placement
from drover.launch import RunnerLauncher
"""Runner tests: real subprocesses running small python child scripts on the current OS."""
import ctypes
import re
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from drover import runner
from tests.tiers import integration

from tests.test_runner import Base, PKG_ROOT, ENV, PY, alive, wait_for, _cleanup_detached_fixture


def wsl_exe():
    return shutil.which("wsl.exe") or next((p for p in ("/mnt/c/Windows/System32/wsl.exe",) if os.path.exists(p)),
                                           None)



def default_distro(exe):
    try:
        r = subprocess.run([exe, "-l", "-q"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    names = r.stdout.decode("utf-16-le", errors="replace").replace("\x00", "").split()
    return next((n for n in names if not n.startswith("docker-desktop") and re.fullmatch(r"[A-Za-z0-9._-]+", n)), None)



@integration
class WslTests(unittest.TestCase):
    def test_wsl_argv_maps_paths_only_in_runner_options(self):
        a = RunnerLauncher("unused", distro="Ubuntu-24.04").runner_argv( ["--run-id", "x", "--runs-dir", "C:\\drover\\runs", "--stdin-file",
                                             "C:/drover/runs/x/in.txt", "--cwd=R:\\repos\\w", "--", "codex",
                                             "C:\\keep\\this", "--x=D:/keep"], cd="R:\\repos\\w")
        self.assertEqual(a[:7], ["wsl.exe", "-d", "Ubuntu-24.04", "--cd", "/mnt/r/repos/w", "--exec", "/usr/bin/env"])
        self.assertEqual(a[a.index("python3"):][:3], ["python3", "-m", "drover.runner"])
        self.assertNotIn("--", a[:6])
        self.assertIn("/mnt/c/drover/runs", a)
        self.assertIn("/mnt/c/drover/runs/x/in.txt", a)
        self.assertIn("--cwd=/mnt/r/repos/w", a)
        self.assertEqual(a[-3:], ["codex", "C:\\keep\\this", "--x=D:/keep"])

    def test_wsl_argv_rejects_bad_distro(self):
        for bad in ("a b", "x;rm", "", "-e", "a$(x)", "d\n"):
            with self.assertRaises(ValueError, msg=bad):
                RunnerLauncher("unused", distro=bad).runner_argv([])

    def test_live_wsl_passes_shell_metacharacters_literally(self):
        exe = wsl_exe()
        distro = exe and default_distro(exe)
        if not distro:
            self.skipTest("wsl.exe or a distro is absent")
        with tempfile.TemporaryDirectory() as runs:
            code = "import sys,json;print('ARGS'+json.dumps(sys.argv[1:]))"
            args = ["--run-id", "w1", "--runs-dir", runs, "--", "python3", "-c", code, "$(id -u)", "'", "a;echo INJECT"]
            argv = wsl_argv_for_exe(exe, distro, args)
            r = subprocess.run(argv, capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            out = (Path(runs) / "w1" / "out.log").read_text()
            line = next(l for l in out.splitlines() if l.startswith("ARGS"))
            self.assertEqual(json.loads(line[4:]), ["$(id -u)", "'", "a;echo INJECT"])



def wsl_argv_for_exe(exe, distro, args):
    argv = RunnerLauncher("unused", distro=distro).runner_argv(args, cd=PKG_ROOT)
    argv[0] = exe
    return argv



@unittest.skipIf(runner.IS_WINDOWS, 'configured writable roots use the Linux temporary filesystem')
@integration
class ConfiguredEnvironmentTests(Base):
    def test_runner_prepares_configured_roots_and_passes_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'cache'
            cfg = {'env': {'REPO_SETTING': 'literal value'}, 'sandbox_writable_roots': [str(root)]}
            code = ('import json,os,stat; from pathlib import Path; '
                    'print(json.dumps([os.environ["REPO_SETTING"],stat.S_IMODE(Path(' + repr(str(root)) + ').stat().st_mode)]))')
            proc, status = self.run_runner('configured', code, extra=('--run-config', json.dumps(cfg)))
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(status['exit_code'], 0)
            self.assertEqual(json.loads(self.out('configured')), ['literal value', 0o777])

    def test_runner_refuses_roots_outside_tmp_and_symlinked_roots(self):
        with tempfile.TemporaryDirectory() as temp:
            link = Path(temp) / 'link'
            link.symlink_to(Path(temp), target_is_directory=True)
            for root in ('/var/cache/never-create', str(link / 'escape')):
                with self.subTest(root=root):
                    cfg = {'sandbox_writable_roots': [root]}
                    proc, status = self.run_runner('rejected', 'raise RuntimeError("must not run")',
                                                   extra=('--run-config', json.dumps(cfg)))
                    self.assertEqual(proc.returncode, 1)
                    self.assertIn('spawn-failed:', status['reason'])
                    self.assertIsNone(status['exit_code'])



@integration
class NativeWindowsEnvironmentTests(Base):
    @unittest.skipUnless(runner.IS_WINDOWS, 'native Windows runner contract')
    def test_configured_linux_roots_do_not_prevent_native_workers(self):
        cfg = {'env': {'REPO_SETTING': 'native'}, 'sandbox_writable_roots': ['/tmp/repo-cache']}
        proc, status = self.run_runner('native-configured', 'import os; print(os.environ["REPO_SETTING"])',
                                       extra=('--run-config', json.dumps(cfg)))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(status['exit_code'], 0)
        self.assertEqual(self.out('native-configured').strip(), 'native')
