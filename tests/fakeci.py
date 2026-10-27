"""An in-process CI launcher for unit tests.

The production path is CI.run -> RunnerLauncher -> `python -m drover.runner` -> `python drover/ci.py --plan` -> the
repo's gate script, polled every 0.25 s.  Most tests only need the gate script's verdict, so this launcher executes
the same plan (`ci._execute`) in the test process: the gate script still runs for real, but the runner spawn, the
second Python start and the poll sleep are gone.  The few tests that check the runner/launcher wiring itself use the
real RunnerLauncher and live in the integration tier (see tests/tiers.py).
"""
import argparse
import json
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from drover import ci, runner


class _Subprocess:
    """`ci.subprocess` with every run() writing its output to the run's log, as the runner would."""

    def __init__(self, log):
        self.log = log

    def run(self, *args, **kw):
        kw.setdefault('stdout', self.log)
        kw.setdefault('stderr', subprocess.STDOUT)
        return subprocess.run(*args, **kw)

    def __getattr__(self, name):
        return getattr(subprocess, name)


@contextmanager
def _environment(env):
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class Handle:
    pid = 1

    def __init__(self, result):
        self.result = result

    def poll(self):
        return self.result

    def terminal(self):
        return None

    def cancel(self):
        pass


def execute_gate(spec, runs_dir):
    """Run a gate spec's plan in-process; returns the execution dict a finished runner handle reports."""
    parser = argparse.ArgumentParser()
    parser.add_argument('--plan', required=True)
    plan_file = parser.parse_known_args(spec['argv'][1:])[0].plan
    plan = json.loads(Path(plan_file).read_text(encoding='utf-8'))
    log = runner.run_dir(runs_dir, spec['run_id']) / runner.OUT_FILE
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, 'w', encoding='utf-8') as out, _environment((spec.get('run_config') or {}).get('env', {})), \
            mock.patch.object(ci, 'subprocess', _Subprocess(out)):
        try:
            code = ci._execute(plan)
        except (ci.CIError, OSError, ValueError, KeyError) as error:
            print(f'CI: {error}', file=out)
            code = 2
    # The runner's terminal status: CI.run uses it to tell a finished run from one it must keep for adoption.
    runner.write_json_atomic(runner.run_dir(runs_dir, spec['run_id']) / runner.STATUS_FILE,
                             {'state': 'exited', 'exit_code': code, 'nonce': spec.get('nonce'), 'run_id': spec['run_id']})
    return {'status': 'done' if code == 0 else 'error', 'record': None, 'text': '', 'handoff': None,
            'returncode': code, 'timeout': None,
            'usage': {'tokens_in': 0, 'tokens_out': 0, 'tokens_cached': 0, 'context': 0}}


class InProcessLauncher:
    """A CI launcher whose gate runs finish inside launch()."""

    def __init__(self, runs_dir):
        self.runs_dir, self.specs = str(runs_dir), []

    def launch(self, spec):
        self.specs.append(spec)
        return Handle(execute_gate(spec, self.runs_dir))

    def adopt(self, spec):
        raise AssertionError('the in-process launcher never leaves a runner to adopt')

    def runners(self, want_wsl=False):
        return {}

    def recover_ci(self, spec, status):
        return True


def resources_go(**_):
    """Admission probe that always admits, so tests not about admission never read host RAM or load."""
    return dict(verdict='go', reasons=[])
