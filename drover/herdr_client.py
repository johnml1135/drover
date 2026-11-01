"""The one client that reaches Herdr. Only the verbs in ALLOWED can be sent, so `server stop` and bare `herdr` cannot.

Herdr closed is normal: a stopped server, a timeout or a reply nothing can read all raise HerdrUnavailable, which says
nothing about any pane. HerdrRefused comes from a running server, with two exceptions: `usage` is a command line
rejected before any server is asked, and `start_unconfirmed` is an `agent start` that outlived its budget and may have
started the agent anyway. Only a missing-pane code shows that a pane is gone; other refusals say nothing about it.
Errors carry fixed words, Herdr's code and a length, never argument text or Herdr's own prose, which can quote it.
"""
import json
import os
import re
import shutil
import subprocess
import time

from . import config, placement_exec

ALLOWED = {
    'agent': ('get', 'list', 'read', 'prompt', 'send-keys', 'start', 'rename'),
    'pane': ('get', 'list', 'read', 'run', 'send-text', 'send-keys', 'close', 'process-info', 'report-agent',
             'report-agent-session', 'release-agent'),
    'tab': ('create', 'list'),
    'workspace': ('list',),
}
REMOTE_OPTIONS = {'--machine', '--session', '--remote'}
SERVER_DOWN = ('server_not_running', 'server_unavailable', 'protocol_mismatch')
USAGE_ERRORS = ('usage:', 'unknown option:', 'unexpected argument:', 'missing value')
START = ('agent', 'start')
START_TIMEOUT_S = 35.0      # Herdr's own startup wait is 30 s, and it must end first


class HerdrUnavailable(RuntimeError):
    """Herdr could not answer. Nothing is known about any pane."""


class HerdrRefused(RuntimeError):
    """A running Herdr said no; `code` is its reason."""

    def __init__(self, code, message=''):
        super().__init__(message or code)
        self.code = code


def _check(argv):
    allowed = all(isinstance(arg, str) and '\0' not in arg for arg in argv) and len(argv) >= 2 and \
        argv[1] in ALLOWED.get(argv[0], ())
    options = argv[:argv.index('--')] if '--' in argv else argv
    if not allowed or REMOTE_OPTIONS.intersection(options):
        group = next((name for name in ALLOWED if argv[:1] == (name,)), 'no allowed group')
        raise ValueError(f'herdr command is not allowed ({group})')


def _code(value):
    """Herdr's code, or `other` for anything that is not one: a free-form error can quote an argument."""
    return value if isinstance(value, str) and re.fullmatch(r'[a-z][a-z0-9_]*', value) else 'other'


def _parsed(text):
    try:
        return json.loads(text or '')
    except ValueError:
        return None


class Herdr:
    def __init__(self, binary, socket='', *, enabled=True, timeout_s=10.0, retry_s=60.0, run=subprocess.run,
                 clock=time.monotonic):
        self.binary = binary
        self.socket = socket
        self.enabled = enabled
        self.timeout_s = timeout_s
        self.retry_s = retry_s
        self.run = run
        self.clock = clock
        self._retry_at = float('-inf')

    def call(self, *argv) -> dict:
        """The JSON `result` of a command; a command that printed nothing gives {}."""
        done = self._run(argv)
        self._raise_any_error(argv, done)
        if not (done.stdout or '').strip():
            return {}
        reply = _parsed(done.stdout)
        result = reply.get('result') if isinstance(reply, dict) else None
        if not isinstance(result, dict):    # a clean exit shows Herdr answered, so no window opens
            raise HerdrUnavailable(f'herdr {argv[0]} {argv[1]} gave a reply that cannot be read')
        return result

    def text(self, *argv) -> str:
        """The output of a command that prints a screen. A clean exit is never parsed: a screen may look like JSON."""
        done = self._run(argv)
        if done.returncode:
            self._raise_any_error(argv, done)
        return done.stdout or ''

    def available(self) -> bool:
        try:
            self.call('workspace', 'list')
        except HerdrRefused as refused:
            return refused.code != 'usage'
        except HerdrUnavailable:
            return False
        return True

    def _run(self, argv):
        _check(argv)
        if not self.enabled or not self.binary:
            raise HerdrUnavailable('herdr is disabled or has no binary')
        if self.clock() < self._retry_at:
            raise HerdrUnavailable('herdr was unavailable and the retry window is still open')
        starting = argv[:2] == START
        timeout = max(self.timeout_s, START_TIMEOUT_S) if starting else self.timeout_s
        env = dict(os.environ)
        if self.socket:
            env['HERDR_SOCKET_PATH'] = self.socket
        try:
            return self.run([self.binary, *argv], stdin=subprocess.DEVNULL, capture_output=True, encoding='utf-8',
                            errors='replace', timeout=timeout, env=env, **placement_exec.NO_WINDOW)
        except subprocess.TimeoutExpired:   # the exception quotes the whole command, so it is not chained
            if starting:    # the agent may be up: reads stay open, and this must not read as "nothing happened"
                raise HerdrRefused('start_unconfirmed', f'herdr agent start outlived {timeout}s') from None
            raise self._down(f'herdr {argv[0]} {argv[1]} timed out after {timeout}s') from None
        except OSError as exc:
            raise self._down(f'herdr {argv[0]} {argv[1]} did not start: {exc.strerror or type(exc).__name__}') from None

    def _raise_any_error(self, argv, done):
        label = f'herdr {argv[0]} {argv[1]}'
        for stream in (done.stdout, done.stderr):
            reply = _parsed(stream)
            error = reply.get('error') if isinstance(reply, dict) else None
            if not error:
                continue
            code = _code(error.get('code') if isinstance(error, dict) else error)
            if code in SERVER_DOWN:
                raise self._down(f'{label}: {code}')
            raise HerdrRefused(code, f'{label}: {code}')
        if done.returncode:
            said = (done.stderr or done.stdout or '').lstrip()
            kind = 'other'
            if said.startswith(USAGE_ERRORS):
                kind = 'usage'
            elif said.startswith('failed to connect'):
                kind = 'connect'
            detail = f'{label} exited {done.returncode}: {kind}, {len(said)} characters of error output'
            if kind == 'usage':
                raise HerdrRefused(kind, detail)
            raise self._down(detail)

    def _down(self, message):
        self._retry_at = self.clock() + self.retry_s
        return HerdrUnavailable(message)


def from_config(cfg=None) -> Herdr:
    """A client for `[panes]`. A binary that cannot be found leaves the client with none, so it makes no call."""
    panes = (config.get() if cfg is None else cfg)['panes']
    return Herdr(shutil.which(panes['herdr_bin'] or 'herdr') or '', panes['socket'], enabled=panes['enabled'],
                 timeout_s=panes['call_timeout_s'], retry_s=panes['retry_unavailable_s'])
