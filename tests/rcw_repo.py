"""Repository model for pipeline and CI policy tests.

Files remain ordinary small files so dirty-work and scope assertions are readable.
Commits, indexes, remotes, ownership and registrations live in memory; no Git CLI
is emulated and no command from a test is executed.
"""
import difflib
import hashlib
import json
from pathlib import Path

from drover import config, origin, placement, repos

SCRIPT = '.drover/scripts/drover/test.ps1'


def key(path):
    """One spelling for a checkout: Windows (H:\\x), WSL (/mnt/h/x), any slashes, drive-mount case-folded."""
    text = placement.to_wsl_path(placement.to_windows_path(str(path))).replace('\\', '/').rstrip('/')
    return text.casefold() if placement.MNT_RE.match(text) else text


def fs(path):
    """The spelling this host can open: H:\\x on Windows, /mnt/h/x on WSL, whatever form arrived."""
    return Path(placement.native(path))


def files(path):
    path = fs(path)
    return {str(p.relative_to(path)).replace('\\', '/'): p.read_text()
            for p in Path(path).rglob('*') if p.is_file() and '.git' not in p.relative_to(path).parts}


def write_tree(path, tree):
    path = fs(path)
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
        self.commits, self.branches, self.checkouts, self.tags, self.indexes = {}, {}, {}, {}, {}
        self.online = True
        self.checkouts[key(self.repo)] = 'main'
        write_tree(self.repo, {'base.txt': 'base\n', SCRIPT: '# fixture action\n'})
        self.save(self.repo, 'base')
        self.remote_tip = self.tracking = self.head()
        self.settings = {}

    def cleanup(self):
        pass  # PolicyCase owns the temporary directory.

    def head(self, ref='HEAD', cwd=None):
        if ref == 'HEAD':
            ref = self.checkouts[key(cwd or self.repo)]
        if ref == 'origin/main':
            return self.tracking
        return self.branches.get(ref, ref)

    def tree(self, ref='HEAD', cwd=None):
        return self.commits[self.head(ref, cwd)]['tree']

    def save(self, cwd=None, message='work'):
        cwd = fs(cwd or self.repo)
        branch = self.checkouts[key(cwd)]
        parent = self.branches.get(branch)
        tree = files(cwd)
        sha = hashlib.sha1(json.dumps([parent, tree, message], sort_keys=True).encode()).hexdigest()
        self.commits[sha] = dict(tree=tree, message=message.strip(), parent=parent)
        self.branches[branch] = sha
        self.indexes[key(cwd)] = {}
        return sha

    def commit(self, name, text=None, cwd=None, msg=None):
        path = fs(cwd or self.repo) / name
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
        path = fs(path)
        if base is not None:
            self.branches[branch] = self.head(base)
        self.checkouts[key(path)] = branch
        write_tree(path, self.tree(branch))
        if path == self.repo:
            return
        admin = self.repo / '.git' / 'worktrees' / path.name
        admin.mkdir(parents=True, exist_ok=True)
        (path / '.git').write_text('gitdir: ' + str(admin) + '\n')
        self.repair(path)

    def repair(self, path):
        import os
        path = fs(path)
        admin = self.repo / '.git' / 'worktrees' / path.name
        (path / '.git').write_text('gitdir: ' + os.path.relpath(admin, path) + '\n')
        return True

    def status(self, cwd=None):
        path = fs(cwd or self.repo)
        old, new = self.tree(cwd=path), files(path)
        return [(p, old.get(p), new.get(p), self.indexes.get(key(path), {}).get(p))
                for p in sorted(old.keys() | new.keys()) if old.get(p) != new.get(p)]

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
        found = next((f for f in self.fixtures if key(path) in f.checkouts), None)
        if found is None:
            known = sorted(k for f in self.fixtures for k in f.checkouts)
            raise AssertionError(f'no fixture checkout for {str(path)!r} (key {key(path)!r}); known: {known}')
        return found

    def is_checkout(self, path):
        return any(key(path) in f.checkouts for f in self.fixtures)

    def reachable(self, path):
        return self.is_checkout(path) and fs(path).exists()

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

    def branch(self, path):
        return self.fixture(path).checkouts[key(path)]

    def owned(self, path, tid):
        return (self.fixture(path).tags.get(key(path)) or {}).get('owner') == 'drover:' + tid

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

    def add(self, repo, dest, branch, base, remote='origin'):
        f = self.fixture(repo)
        f.checkout(dest, branch, None if branch in f.branches else base)

    def changed_paths(self, path, base, tip='HEAD'):
        f = self.fixture(path)
        old, new = f.tree(base), f.tree(tip, path)
        return sorted(p for p in old.keys() | new.keys() if old.get(p) != new.get(p))

    def diff_text(self, path, base, tip):
        f = self.fixture(path)
        old, new = f.tree(base), f.tree(tip, path)
        return ''.join(''.join(difflib.unified_diff(old.get(p, '').splitlines(True), new.get(p, '').splitlines(True), p, p))
                       for p in self.changed_paths(path, base, tip))

    def diff_lines(self, path, base, tip):
        return sum(line.startswith(('+', '-')) and not line.startswith(('+++', '---'))
                   for line in self.diff_text(path, base, tip).splitlines())
