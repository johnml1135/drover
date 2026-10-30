"""Repository facts and finished CI receipts for pipeline policy tests.

Files remain ordinary small files so dirty-work and scope assertions are readable.
Commits, indexes, remotes, ownership and registrations live in memory.
CheckoutGit models checkout bytes for adapter contracts; no command is executed.
"""
import difflib
import hashlib
import json
import re
from pathlib import Path

from drover import ci, config, origin, placement, repos, runner

SCRIPT = '.drover/scripts/drover/test.ps1'

def files(path):
    return {str(p.relative_to(path)).replace('\\', '/'): p.read_text()
            for p in Path(path).rglob('*') if p.is_file() and '.git' not in p.relative_to(path).parts}

def write_tree(path, tree):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    for name in files(path).keys() - tree.keys():
        (path / name).unlink()
    for name, text in tree.items():
        dest = path / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)

class MemoryRepo:
    def __init__(self, root):
        self.root = Path(root)
        self.repo, self.remote = self.root / 'repo', self.root / 'remote.git'
        self.repo.mkdir(parents=True)
        (self.repo / '.git').mkdir()
        (self.repo / '.git' / 'config').write_text('[core]\nrepositoryformatversion=0\n')
        self.commits, self.branches, self.checkouts, self.tags, self.indexes = {}, {}, {}, {}, {}
        self.online = True
        self.checkouts[str(self.repo)] = 'main'
        write_tree(self.repo, {'base.txt': 'base\n', SCRIPT: '# fixture action\n'})
        self.save(self.repo, 'base')
        self.remote_tip = self.tracking = self.head()

    def cleanup(self):
        pass  # PolicyBase owns the temporary directory.

    def head(self, ref='HEAD', cwd=None):
        ref = ref.removeprefix('refs/heads/').removeprefix('refs/remotes/')
        if ref == 'HEAD':
            ref = self.checkouts[str(cwd or self.repo)]
        if ref == 'origin/main':
            return self.tracking
        return self.branches.get(ref, ref)

    def tree(self, ref='HEAD', cwd=None):
        return self.commits[self.head(ref, cwd)]['tree']

    def save(self, cwd=None, message='work'):
        cwd = Path(cwd or self.repo)
        branch = self.checkouts[str(cwd)]
        parent = self.branches.get(branch)
        tree = files(cwd)
        sha = hashlib.sha1(json.dumps([parent, tree, message], sort_keys=True).encode()).hexdigest()
        self.commits[sha] = dict(tree=tree, message=message.strip(), parent=parent)
        self.branches[branch] = sha
        self.indexes[str(cwd)] = {}
        return sha

    def commit(self, name, text=None, cwd=None, msg=None):
        path = Path(cwd or self.repo) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text if text is not None else name + '\n')
        return self.save(cwd, msg or name)

    def branch(self, name, values, base='origin/main'):
        self.branches[name] = self.head(base)
        parent = self.head(base)
        tree = {**self.tree(base), **values}
        sha = hashlib.sha1(json.dumps([parent, tree, name], sort_keys=True).encode()).hexdigest()
        self.commits[sha] = dict(tree=tree, message=name, parent=parent)
        self.branches[name] = sha
        return sha

    def checkout(self, path, branch, base=None):
        path = Path(path)
        if base is not None:
            self.branches[branch] = self.head(base)
        self.checkouts[str(path)] = branch
        write_tree(path, self.tree(branch))
        admin = self.repo / '.git' / 'worktrees' / path.name
        admin.mkdir(parents=True, exist_ok=True)
        (path / '.git').write_text('gitdir: ' + str(admin) + '\n')
        self.repair(path)

    def repair(self, path):
        import os
        path = Path(path)
        admin = self.repo / '.git' / 'worktrees' / path.name
        (path / '.git').write_text('gitdir: ' + os.path.relpath(admin, path) + '\n')
        return True

    def admin(self, path):
        return self.repo / '.git' / 'worktrees' / Path(path).name

    def message(self, cwd=None):
        return self.commits[self.head(cwd=cwd)]['message']

    def stage(self, name, cwd=None):
        path = Path(cwd or self.repo)
        self.indexes.setdefault(str(path), {})[name] = (path / name).read_text() if (path / name).exists() else None

    def status(self, cwd=None):
        path = Path(cwd or self.repo)
        old, new = self.tree(cwd=path), files(path)
        return [(p, old.get(p), new.get(p), self.indexes.get(str(path), {}).get(p))
                for p in sorted(old.keys() | new.keys()) if old.get(p) != new.get(p)]

    def push(self, sha='HEAD'):
        self.remote_tip = self.head(sha)

    def ancestor(self, base, tip):
        base, tip = self.head(base), self.head(tip)
        while tip:
            if base == tip:
                return True
            tip = self.commits[tip]['parent']
        return False


class MemoryRepository:
    def __init__(self, fixture):
        self.fixtures = [fixture]

    def fixture(self, path):
        return next(f for f in self.fixtures if str(path) in f.checkouts)

    def is_checkout(self, path):
        return any(str(path) in f.checkouts for f in self.fixtures)

    def reachable(self, path):
        return self.is_checkout(path) and Path(path).exists()

    def canonical(self, path):
        return str(Path(path).resolve())

    def prepare_worktree(self, path):
        pass

    def load(self, prog, revision='HEAD', checkout=None, **kw):
        # Keep real normalization and committed config parsing; supply committed bytes.
        from copy import deepcopy
        prog = deepcopy(prog)
        answer = prog.get('_test_script')
        machine = prog.get('_machine', {k: v for k, v in prog.items() if not k.startswith('_')})
        path = checkout or machine.get('repo')
        versioned = {}
        if self.reachable(path):
            f = self.fixture(path)
            text = f.tree(revision, path).get('.drover/config.toml')
            if text:
                versioned = repos.parse_committed_config(text, f.head(revision, path))
        if not machine.get('repo'):
            raise repos.RepoError('repo is required')
        merged = config._merge(versioned, machine)
        if answer and 'test' not in versioned.get('scripts', {}):
            merged = config._merge(merged, {'scripts': {'test': answer}})
        out = repos._ResolvedRepo(repos.normalize(merged))
        out['_machine'] = machine
        if answer:
            out['_test_script'] = answer
        return out

    def head(self, path):
        return self.fixture(path).head(cwd=path)

    def resolve(self, path, ref):
        return self.fixture(path).head(ref)

    def ancestor(self, path, base, tip):
        return self.fixture(path).ancestor(base, tip)

    def branch(self, path):
        return self.fixture(path).checkouts[str(path)]

    def inventory(self, repo, side=None):
        f = self.fixture(repo)
        return [dict(worktree=p, branch='refs/heads/' + branch) for p, branch in f.checkouts.items()]

    def owned(self, path, tid):
        return (self.fixture(path).tags.get(str(path)) or {}).get('owner') == 'drover:' + tid

    def tag(self, path, name, tid):
        self.fixture(path).tags[str(path)] = dict(owner='drover:' + tid, repo=name)

    def repair(self, path):
        return placement.relativize_gitdir(path)

    def dirty(self, path):
        return '\n'.join(p for p, *_ in self.fixture(path).status(path))

    def status(self, path):
        return self.dirty(path)

    def sync(self, repo, remote, default):
        f = self.fixture(repo)
        if not f.online:
            raise origin.FetchFailed(f'fetch failed: {remote}/{default}; repo paused until owner retries')
        f.tracking = f.remote_tip
        if f.ancestor(f.tracking, default):
            return
        if not f.ancestor(default, f.tracking):
            raise origin.SyncError(f'{default} diverged from {remote}')
        f.branches[default] = f.tracking
        write_tree(f.repo, f.tree(default))

    def start_ref(self, repo, branch, base, remote):
        f = self.fixture(repo)
        if branch in f.branches:
            return 'refs/heads/' + branch
        return base

    def add(self, repo, dest, branch, start_ref, remote='origin'):
        f = self.fixture(repo)
        f.checkout(dest, branch, None if branch in f.branches else start_ref)

    def remove(self, repo, path):
        import shutil
        f = self.fixture(repo)
        del f.checkouts[str(path)]
        f.tags.pop(str(path), None)
        shutil.rmtree(path)

    def changes(self, path):
        return [p for p, *_ in self.fixture(path).status(path)]

    def trailers(self, path):
        message = self.fixture(path).message(path)
        return [next(iter(re.findall('^' + name + r': (.*)$', message, re.M)), '')
                for name in ('Drover-Task', 'Drover-Run')]

    def commit(self, path, message):
        return self.fixture(path).save(path, message)

    def changed_paths(self, path, base, tip='HEAD'):
        f = self.fixture(path)
        old, new = f.tree(base), f.tree(tip, path)
        return sorted(p for p in old.keys() | new.keys() if old.get(p) != new.get(p))

    def changed_docs_only(self, path, base, tip, globs):
        return ci.docs_only(self.changed_paths(path, base, tip), globs)

    def diff_text(self, path, base, tip):
        f = self.fixture(path)
        old, new = f.tree(base), f.tree(tip, path)
        return ''.join(''.join(difflib.unified_diff(old.get(p, '').splitlines(True), new.get(p, '').splitlines(True), p, p))
                       for p in self.changed_paths(path, base, tip))

    def diff_lines(self, path, base, tip):
        return sum(line.startswith(('+', '-')) and not line.startswith(('+++', '---'))
                   for line in self.diff_text(path, base, tip).splitlines())


def finish_gate(spec, runs_dir, *, code=0, output=''):
    """Return a finished runner and write the receipt consumed by real CI.result."""
    d = runner.run_dir(runs_dir, spec['run_id'])
    plan = json.loads((d / 'plan.json').read_text())
    (d / runner.OUT_FILE).write_text(output)
    receipt = dict(version=ci.VERSION, sha=plan['sha'], status='passed' if code == 0 else 'failed',
                   passed=code == 0, exit_code=code, tree_unchanged=True, kind=plan['kind'],
                   duration_s=.125, steps=[], platform=spec['placement'], base=plan['base'])
    (d / 'receipt.json').write_text(json.dumps(receipt))
    runner.write_json_atomic(d / runner.STATUS_FILE,
                             dict(state='exited', exit_code=code, nonce=spec.get('nonce'), run_id=spec['run_id']))
    return dict(status='done' if code == 0 else 'error', record=None, text='', handoff=None,
                returncode=code, timeout=None,
                usage=dict(tokens_in=0, tokens_out=0, tokens_cached=0, context=0))


def read_checkouts(source, entries, configured, root):
    from drover.task_context import split_read
    reads = []
    for entry in entries:
        name, branch = split_read(entry)
        f = source.fixture(repos.repo_path(configured[name]))
        sha = f.head(branch or 'main')
        dest = Path(root) / (name + '-' + sha[:12])
        write_tree(dest, f.tree(sha))
        reads.append(dict(entry=entry, path=str(dest), sha=sha))
    return reads


class CheckoutGit:
    """Worktree adapter fake: model checkout bytes with inherited autocrlf=true."""
    def __init__(self, tree, *, legacy=False):
        self.tree, self.legacy, self.adds = tree, legacy, []

    def __call__(self, repo, *args, **kw):
        import subprocess
        if 'worktree' in args and 'add' in args:
            self.adds.append(args)
            if self.legacy and '--relative-paths' in args:
                return subprocess.CompletedProcess(args, 129, '', "error: unknown option `relative-paths'")
            dest = Path(args[-2])  # The adapter creates a new branch: -b branch dest base.
            dest.mkdir(parents=True)
            for name, blob in self.tree.items():
                path = dest / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(blob if args[:2] == ('-c', 'core.autocrlf=false')
                                 else blob.replace(b'\n', b'\r\n'))
            (dest / '.git').write_text('gitdir: ' + str(Path(repo) / '.git/worktrees' / dest.name) + '\n')
        code = 1 if args[:1] == ('rev-parse',) else 0
        return subprocess.CompletedProcess(args, code, '', '')
