"""The one client that reaches Herdr. Only the verbs in ALLOWED can be sent, so `server stop` and bare `herdr` cannot.

Herdr closed is normal: a stopped server, a timeout or a reply nothing can read all raise HerdrUnavailable, which says
nothing about any pane. Only HerdrRefused comes from a running server, so only it can show that a pane is missing.
Errors name the verb and never its arguments, because a prompt must not reach a log.
"""
import json
import os
import shutil
import subprocess
import time

from . import config, placement_exec

ALLOWED = {
    'agent': ('get', 'list', 'read', 'prompt', 'send-keys', 'start', 'rename'),
    'pane': ('get', 'list', 'read', 'split', 'run', 'send-text', 'send-keys', 'close', 'process-info', 'report-agent',
             'report-agent-session', 'release-agent'),
    'tab': ('create', 'list'),
    'workspace': ('list',),
}
SERVER_DOWN = ('server_not_running', 'server_unavailable')
DETAIL_MAX = 200


class HerdrUnavailable(RuntimeError):
    """Herdr could not answer. Nothing is known about any pane."""


class HerdrRefused(RuntimeError):
    """A running Herdr said no; `code` is its reason."""

    def __init__(self, code, message=''):
        super().__init__(message or code)
        self.code = code


def _check(argv):
    allowed = all(isinstance(arg, str) for arg in argv) and len(argv) >= 2 and argv[1] in ALLOWED.get(argv[0], ())
    if not allowed:
        raise ValueError(f"herdr command is not allowed: {' '.join(str(arg) for arg in argv[:2]) or 'bare herdr'}")


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
        if not isinstance(result, dict):
            raise self._down(f'herdr {argv[0]} {argv[1]} gave a reply that cannot be read')
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
        except HerdrRefused:
            return True
        except HerdrUnavailable:
            return False
        return True

    def _run(self, argv):
        _check(argv)
        if not self.enabled or not self.binary:
            raise HerdrUnavailable('herdr is disabled or has no binary')
        if self.clock() < self._retry_at:
            raise HerdrUnavailable('herdr was unavailable and the retry window is still open')
        env = dict(os.environ)
        if self.socket:
            env['HERDR_SOCKET_PATH'] = self.socket
        try:
            return self.run([self.binary, *argv], stdin=subprocess.DEVNULL, capture_output=True, encoding='utf-8',
                            errors='replace', timeout=self.timeout_s, env=env, **placement_exec.NO_WINDOW)
        except subprocess.TimeoutExpired:   # the exception quotes the whole command, so it is not chained
            raise self._down(f'herdr {argv[0]} {argv[1]} timed out after {self.timeout_s}s') from None
        except OSError as exc:
            raise self._down(f'herdr {argv[0]} {argv[1]} did not start: {exc.strerror or type(exc).__name__}') from None

    def _raise_any_error(self, argv, done):
        label = f'herdr {argv[0]} {argv[1]}'
        for stream in (done.stdout, done.stderr):
            reply = _parsed(stream)
            error = reply.get('error') if isinstance(reply, dict) else None
            if not error:
                continue
            details = error if isinstance(error, dict) else {'code': str(error)}
            code, message = details.get('code', ''), details.get('message', '')
            detail = f'{label}: {code} {str(message)[:DETAIL_MAX]}'.rstrip()
            if code in SERVER_DOWN:
                raise self._down(detail)
            raise HerdrRefused(code, detail)
        if done.returncode:
            raise self._down(f'{label} exited {done.returncode}: {(done.stderr or "")[:DETAIL_MAX]}'.rstrip())

    def _down(self, message):
        self._retry_at = self.clock() + self.retry_s
        return HerdrUnavailable(message)


def from_config(cfg=None) -> Herdr:
    """A client for `[panes]`. A binary that cannot be found leaves the client with none, so it makes no call."""
    panes = (config.get() if cfg is None else cfg)['panes']
    return Herdr(shutil.which(panes['herdr_bin'] or 'herdr') or '', panes['socket'], enabled=panes['enabled'],
                 timeout_s=panes['call_timeout_s'], retry_s=panes['retry_unavailable_s'])
