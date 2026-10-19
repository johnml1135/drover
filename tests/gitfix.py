"""Fake git repos in temp dirs for the landing, gate and garden tests."""
import os
import subprocess
import tempfile
from pathlib import Path


def sh(*args, cwd=None, check=True, text=True):
    command = [str(a) for a in args]
    env = os.environ.copy()
    env.update(GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never', GIT_ASKPASS='')
    is_clone = len(command) > 1 and Path(command[0]).stem.lower() == 'git' and command[1] == 'clone'
    attempts = 2 if is_clone else 1
    r = None
    for _ in range(attempts):
        try:
            run_env = {**env, **({'GIT_TRACE': '1'} if is_clone else {})}
            r = subprocess.run(command, cwd=cwd, env=run_env, text=text, capture_output=True, timeout=60)
        except subprocess.TimeoutExpired as error:
            stdout, stderr = error.stdout or '', error.stderr or ''
            if isinstance(stdout, bytes):
                stdout = stdout.decode(errors='replace')
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors='replace')
            r = subprocess.CompletedProcess(command, 124, stdout, stderr)
        if not check or r.returncode == 0:
            return r
    if check and r.returncode:
        stderr = r.stderr.decode(errors='replace') if isinstance(r.stderr, bytes) else r.stderr
        raise AssertionError(f'{" ".join(command)} failed with return code {r.returncode}; '
                             f'GIT_TRACE=1 stderr:\n{stderr}')
    return r


class Repo:
    """A bare `remote.git` and a clone `repo` with one pushed base commit on main."""

    def __init__(self, ignore=''):
        self.tmp = tempfile.TemporaryDirectory(prefix='drover-fix-', dir=os.environ.get('DROVER_TEST_TMPDIR'))
        self.root = Path(self.tmp.name)
        self.remote, self.repo = self.root / 'remote.git', self.root / 'repo'
        sh('git', 'init', '-q', '--bare', '-b', 'main', self.remote)
        # An explicit file URL avoids Git for Windows interpreting a drive-letter path as remote syntax.
        sh('git', 'clone', '-q', self.remote.resolve().as_uri(), self.repo)
        for k, v in (('user.name', 'test'), ('user.email', 't@example.invalid')):
            self.git('config', k, v)
        self.commit('base.txt', 'base\n')
        if ignore:
            self.commit('.gitignore', ignore)
        self.git('push', '-q', 'origin', 'HEAD:main')
        self.git('fetch', '-q', 'origin')

    def cleanup(self):
        self.tmp.cleanup()

    def git(self, *args, cwd=None):
        return sh('git', '-C', cwd or self.repo, *args).stdout.strip()

    def commit(self, name, text=None, cwd=None, msg=None):
        where = Path(cwd or self.repo)
        p = where / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text if text is not None else name + '\n')
        self.git('add', name, cwd=where)
        self.git('commit', '-q', '-m', msg or name, cwd=where)
        return self.git('rev-parse', 'HEAD', cwd=where)

    def branch(self, name, files, base='origin/main'):
        """A branch `name` off base with one commit per (file, text); returns the tip sha."""
        self.git('branch', '-q', name, base)
        wt = self.root / 'tmp-wt' / name
        self.git('worktree', 'add', '-q', wt, name)
        for f, t in files.items():
            self.commit(f, t, cwd=wt, msg=f'{name}: {f}')
        tip = self.git('rev-parse', 'HEAD', cwd=wt)
        self.git('worktree', 'remove', '--force', wt)
        return tip

    def remote_head(self):
        return self.git('ls-remote', self.remote, 'refs/heads/main').split()[0]
