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
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest import mock

from drover import ci, runner


class FakeCommands:
    """Ordered, strict subprocess.run boundary shared by pwsh, Python and Git tests.

    Pass a test-owned temporary root. No executable is created or resolved and no
    host environment is copied. Use the executable paths and env below, or expect
    the literal argv of the production caller. Every supplied subprocess option
    must match exactly, including omitted options (cwd/env/timeout included).

    Example: fake.expect([fake.python, '-V'], cwd=fake.cwd, env=fake.env,
                         timeout=10, capture_output=True, text=True, output='Python test')
    Inject fake.run into a runner seam, or use fake.patch(module) to replace only
    that module's subprocess reference. Call assert_done() for injected runners;
    patch() checks for unused expectations on normal exit.
    """

    def __init__(self, root):
        self.root = Path(root)
        self.cwd = self.root / 'checkout'
        home, state, bins = (self.root / name for name in ('home', 'state', 'bin'))
        for path in (self.cwd, home, state, bins):
            path.mkdir(parents=True, exist_ok=True)
        self.pwsh, self.python, self.git = (bins / name for name in ('pwsh', 'python', 'git'))
        self.env = {'HOME': str(home), 'USERPROFILE': str(home), 'DROVER_HOME': str(state), 'PATH': str(bins)}
        self.calls, self._expected = [], []

    @staticmethod
    def _command(argv):
        if isinstance(argv, (str, bytes, os.PathLike)):
            raise AssertionError('fake commands require an argv sequence')
        return [os.fspath(arg) for arg in argv]

    @staticmethod
    def _options(options):
        # Preserve the observed environment even if the caller mutates it later.
        return {k: dict(v) if k == 'env' and v is not None else v for k, v in options.items()}

    def expect(self, argv, *, returncode=0, output='', stderr_output='', error=None, **options):
        """Queue output (including malformed text), an exit code, or an exception.

        TimeoutExpired/FileNotFoundError and other errors are raised verbatim.
        Output follows run's text/bytes, capture, redirection and check behavior.
        """
        self._expected.append((self._command(argv), self._options(options), returncode, output, stderr_output, error))

    def run(self, argv, **options):
        command, observed = self._command(argv), self._options(options)
        self.calls.append((command, observed))
        if not self._expected:
            raise AssertionError(f'unexpected command: {command!r} {observed!r}')
        expected, contract, code, output, stderr, error = self._expected[0]
        if command != expected or observed != contract:
            raise AssertionError(f'command contract mismatch:\nexpected: {expected!r} {contract!r}'
                                 f'\nobserved: {command!r} {observed!r}')
        self._expected.pop(0)
        if error is not None:
            raise error
        text = options.get('text') or options.get('universal_newlines') or options.get('encoding')
        encoding, errors = options.get('encoding') or 'utf-8', options.get('errors') or 'strict'
        def convert(value):
            if text:
                return value.decode(encoding, errors) if isinstance(value, bytes) else value
            return value.encode(encoding, errors) if isinstance(value, str) else value
        output, stderr = convert(output), convert(stderr)
        stdout_target = subprocess.PIPE if options.get('capture_output') else options.get('stdout')
        stderr_target = subprocess.PIPE if options.get('capture_output') else options.get('stderr')
        if stderr_target == subprocess.STDOUT:
            output, stderr = output + stderr, None
        def emit(value, target):
            if target == subprocess.PIPE:
                return value
            if hasattr(target, 'write') and value is not None:
                target.write(value)
            return None
        result = subprocess.CompletedProcess(command, code, emit(output, stdout_target), emit(stderr, stderr_target))
        if options.get('check') and code:
            raise subprocess.CalledProcessError(code, command, output=result.stdout, stderr=result.stderr)
        return result

    def assert_done(self):
        if self._expected:
            raise AssertionError(f'unused command expectations: {[row[0] for row in self._expected]!r}')

    @contextmanager
    def patch(self, *modules):
        """Patch consumer modules, never the shared subprocess module or another test."""
        if subprocess in modules:
            raise ValueError('patch a consumer module, not subprocess itself')
        with ExitStack() as stack:
            for module in modules:
                stack.enter_context(mock.patch.object(module, 'subprocess', self))
            yield self
            self.assert_done()

    def __getattr__(self, name):
        value = getattr(subprocess, name)
        if callable(value) and not (isinstance(value, type) and issubclass(value, subprocess.SubprocessError)):
            raise AssertionError(f'unscripted subprocess API: {name}')
        return value


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
