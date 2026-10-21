"""Fake git repos in temp dirs for the landing, gate and garden tests."""
import atexit
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from pathlib import Path


def clean_parent():
    """A temp parent with no `.git` at or above it (a stray /tmp/.git must not turn fixtures into repo content)."""
    for base in (tempfile.gettempdir(), '/dev/shm', '/var/tmp'):
        path = Path(base).absolute()
        if path.is_dir() and not any((p / '.git').exists() for p in (path, *path.parents)):
            return str(path)
    return None


def sh(*args, cwd=None, check=True, text=True, input=None):
    command = [str(a) for a in args]
    env = os.environ.copy()
    env.update(GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never', GIT_ASKPASS='')
    is_clone = len(command) > 1 and Path(command[0]).stem.lower() == 'git' and command[1] == 'clone'
    attempts = 2 if is_clone else 1
    r = None
    for _ in range(attempts):
        try:
            run_env = {**env, **({'GIT_TRACE': '1'} if is_clone else {})}
            r = subprocess.run(command, cwd=cwd, env=run_env, text=text, capture_output=True, timeout=60,
                               **({"input": input} if input is not None else {"stdin": subprocess.DEVNULL}))
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


def _remove_tree(path):
    """rmtree that clears the read-only bit (git objects on Windows) and retries."""
    def retry(function, target, *_):
        try:
            os.chmod(target, stat.S_IWRITE)
            function(target)
        except OSError:
            pass
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


_templates, _template_lock, _template_dir = {}, threading.Lock(), []


def _template(ignore):
    """The finished fixture for one shape, built once per process and copied for every Repo()."""
    with _template_lock:
        if ignore not in _templates:
            if not _template_dir:
                _template_dir.append(tempfile.mkdtemp(prefix='drover-fix-template-', dir=os.environ.get('DROVER_TEST_TMPDIR') or clean_parent()))
                atexit.register(_remove_tree, _template_dir[0])
            root = Path(_template_dir[0]) / f'shape{len(_templates)}'
            root.mkdir()
            remote, repo = root / 'remote.git', root / 'repo'
            sh('git', 'init', '-q', '--bare', '-b', 'main', remote)
            # An explicit file URL avoids Git for Windows interpreting a drive-letter path as remote syntax.
            sh('git', 'clone', '-q', remote.resolve().as_uri(), repo)
            for k, v in (('user.name', 'test'), ('user.email', 't@example.invalid')):
                sh('git', '-C', repo, 'config', k, v)
            files = [('base.txt', 'base\n')] + ([('.gitignore', ignore)] if ignore else [])
            for name, text in files:
                (repo / name).write_text(text)
                sh('git', '-C', repo, 'add', name)
                sh('git', '-C', repo, 'commit', '-q', '-m', name)
            sh('git', '-C', repo, 'push', '-q', 'origin', 'HEAD:main')
            sh('git', '-C', repo, 'fetch', '-q', 'origin')
            _templates[ignore] = root
        return _templates[ignore]


class Repo:
    """A bare `remote.git` and a clone `repo` with one pushed base commit on main.

    Each shape (the `ignore` text) is built once per process; every Repo() is a private copy of it with origin
    repointed at its own remote.git, so tests share nothing."""

    def __init__(self, ignore=''):
        template = _template(ignore)
        self.tmp = tempfile.TemporaryDirectory(prefix='drover-fix-', dir=os.environ.get('DROVER_TEST_TMPDIR') or clean_parent())
        self.root = Path(self.tmp.name)
        self.remote, self.repo = self.root / 'remote.git', self.root / 'repo'
        shutil.copytree(template / 'remote.git', self.remote)
        shutil.copytree(template / 'repo', self.repo)
        self.git('config', 'remote.origin.url', self.remote.resolve().as_uri())

    def cleanup(self):
        self.tmp.cleanup()

    def git(self, *args, cwd=None, input=None):
        return sh('git', '-C', cwd or self.repo, *args, input=input).stdout.strip()

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
