"""Single-instance lock: an exclusive OS lock on a file, released when the holder exits or dies.

The hub holds <state dir>\\dispatcher.lock for as long as its dispatcher thread runs; `python -m drover.dispatcher`
refuses while it is held. Windows uses msvcrt, POSIX uses flock (imported only there).
"""
import errno
import os
import sys
import time
from pathlib import Path

from . import config


class LockHeld(RuntimeError):
    pass


def default_path(env=None):
    env = os.environ if env is None else env
    return Path(env.get("DROVER_DISPATCHER_LOCK") or config.state_path("dispatcher.lock"))


class FileLock:
    def __init__(self, path, *, blocking=False):
        self.path, self.fd = Path(path), None
        self.blocking = blocking

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if sys.platform == "win32":
                import msvcrt
                while True:
                    try:
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                        break
                    except OSError as e:
                        if not self.blocking or e.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                            raise
                        time.sleep(0.01)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | (0 if self.blocking else fcntl.LOCK_NB))
        except OSError as e:
            os.close(fd)
            raise LockHeld(f"another dispatcher holds {self.path}") from e
        self.fd = fd
        return self

    def release(self):
        if self.fd is None:
            return
        fd, self.fd = self.fd, None
        try:
            if sys.platform == "win32":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    @property
    def held(self):
        return self.fd is not None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()


def acquire(path=None):
    return FileLock(path or default_path()).acquire()
