"""Answer git questions as yes, no or unknown, so a timeout means one thing to every caller.

`ask` is a question, `out` the stdout of a command that must succeed, `mutate` a command that changes something. A
run that did not end, or could not start, has its own `why`: there is no 124 or 127 and no raised `TimeoutExpired`.
Path mapping stays in `placement_git` (ADR-0007): argv and env come from there, then `process.run`, then the count.
Nothing retries.
"""
import os
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Literal

from . import placement_git, process
from .placement_git import request_git     # the hub's per-request count; the same object until Task 28 moves it

__all__ = ['Answer', 'Why', 'GitReply', 'GitDone', 'GitFailed', 'QUICK', 'NORMAL', 'FETCH', 'LONG',
           'ask', 'out', 'mutate', 'argv', 'request_git']

QUICK, NORMAL, FETCH, LONG = 30.0, 60.0, 120.0, 600.0     # seconds


class Answer(Enum):
    YES = 'yes'
    NO = 'no'
    UNKNOWN = 'unknown'

    def __bool__(self):     # `if answer:` and `not answer` are TypeErrors: unknown can never pass for yes
        raise TypeError('compare an Answer with `is`')


Why = Literal['timeout', 'unavailable', 'exit']


@dataclass(frozen=True)
class GitReply:
    answer: Answer
    out: str
    err: str
    code: int | None
    why: Why | None         # set exactly when answer is UNKNOWN


@dataclass(frozen=True)
class GitDone:
    ok: bool                # exit 0, and nothing else
    out: str
    err: str
    code: int | None
    why: Why | None         # set exactly when not ok


class GitFailed(OSError):
    """An OSError, as RegistrationsUnknown is, so every existing catch tuple already holds it.
    str() never holds stderr: it can carry a credential-bearing remote URL. Read .err to show it."""

    def __init__(self, args_, why: Why, code, err: str, out: str):
        self.args_, self.why, self.code, self.err, self.out = tuple(args_), why, code, err, out
        command = f'git {self.args_[0]}' if self.args_ else 'git'
        super().__init__(f'{command} failed: exit {code}' if why == 'exit' else f'{command} failed: {why}')

    def __reduce__(self):
        return type(self), (self.args_, self.why, self.code, self.err, self.out)


_WHY = {'timed_out': 'timeout', 'unavailable': 'unavailable'}


def _run(repo, args, timeout_s, env, side):
    """(code, out, err, why): why is None for an exit, which `code` then holds."""
    try:
        command, extra = placement_git._git_command(repo, args, side=side)
    except process.CommandTooLong:
        raise                                   # a caller's bug, refused before any spawn
    except (OSError, ValueError) as error:      # WslOffline, a bad .git pointer, an unreadable config
        return None, '', f'git unavailable: {error}', 'unavailable'
    if extra:
        env = {**(os.environ if env is None else env), **extra}
    done = process.run(process.Command(command, timeout_s, env=env))
    with placement_git.git_spawn_lock:
        placement_git.git_spawns += 1
    count = request_git.get()
    if count is not None:
        count['spawns'] += 1
    out, err = placement_git._text(done.out), placement_git._text(done.err)
    if done.kind == 'exited':
        return done.code, out, err, None
    return None, out, err if done.kind == 'timed_out' else f'git unavailable: {done.detail}', _WHY[done.kind]


def ask(repo, *args, timeout_s: float, no: tuple[int, ...], env=None, side=None) -> GitReply:
    """A question. Exit 0 is YES; an exit in `no`, which each caller must state, is NO; anything else is UNKNOWN."""
    code, stdout, stderr, why = _run(repo, args, timeout_s, env, side)
    if why is None and code == 0:
        return GitReply(Answer.YES, stdout, stderr, code, None)
    if why is None and code in no:
        return GitReply(Answer.NO, stdout, stderr, code, None)
    return GitReply(Answer.UNKNOWN, stdout, stderr, code, why or 'exit')


def out(repo, *args, timeout_s: float, env=None, side=None,
        error: Callable[[GitFailed], Exception] | None = None) -> str:
    """stdout of a command that must succeed. Anything else raises GitFailed, or what `error` builds from it, with
    `.why` set."""
    code, stdout, stderr, why = _run(repo, args, timeout_s, env, side)
    if why is None and code == 0:
        return stdout
    failed = GitFailed(args, why or 'exit', code, stderr, stdout)
    if error is None:
        raise failed
    raise error(failed) from failed


def mutate(repo, *args, timeout_s: float, env=None, side=None) -> GitDone:
    """A command that changes something. There is no Answer: it worked, or it did not."""
    code, stdout, stderr, why = _run(repo, args, timeout_s, env, side)
    ok = why is None and code == 0
    return GitDone(ok, stdout, stderr, code, None if ok else why or 'exit')


def argv(repo, args, side=None) -> list[str]:
    """The argv `ask`, `out` and `mutate` run, for the journal's restore_argv."""
    return placement_git.git_argv(repo, args, side=side)
