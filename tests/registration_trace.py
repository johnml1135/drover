"""What real git does over the whole damage fixture of section 5.3, recorded by hand and replayed in the default tier.

`record` runs real git in a temp tree, so it runs only by hand, once per side, and never from a test:

    env -u DROVER_TESTS $PY -m tests.registration_trace --side windows      # from the worktree, on Windows
    env -u DROVER_TESTS python3 -m tests.registration_trace --side wsl      # from the worktree, under WSL

Each case's tree is one root holding `repo` and usually `wt`. Before the damage the listing and the admin files are
kept as `before`; after it, the exit code, stdout, rows and files. Paths are stored under a placeholder that keeps
the pointer's form: `<DRIVE>:/...`, `<MNT>/...` or `<EXT4>/...` each stand for the case's root in that spelling, so
the drive-letter and `/mnt` cases stay apart. Each side's own tree is in its native form. `rows` are git's fields in
git's order: two rows at one path stay two rows, and order differs by side, so nothing here keys by path.
"""
import argparse
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, NamedTuple

from drover.placement_paths import cleanup_path

TRACE_PATH = Path(__file__).with_suffix('.json')
DRIVE, MNT, EXT4 = '<DRIVE>:', '<MNT>', '<EXT4>'
NATIVE = {'windows': DRIVE, 'wsl': EXT4}
UNREADABLE, VANISHED = '<UNREADABLE>', '<VANISHED>'
FOREIGN_LETTER = 'X'                                # a drive for a posix tree, where the pointer only spells a place
HOST_PATH = re.compile(r'[A-Za-z]:[\\/]|/mnt/|/home/')
ADMIN_FILES = {'HEAD', 'commondir', 'gitdir', 'locked'}
SKIP_DIRECTORIES = {'hooks', 'info', 'logs', 'objects'}
SKIP_FILES = {'COMMIT_EDITMSG', 'ORIG_HEAD', 'description', 'index'}


def spellings(root):
    """The root in each form a pointer can take. A form the root has no true spelling in is still a distinct one."""
    text = str(root).replace('\\', '/').rstrip('/')
    match = re.fullmatch(r'([A-Za-z]):/(.*)', text)
    letter, tail = (match[1], match[2]) if match else (FOREIGN_LETTER, text.lstrip('/'))
    # A drive tree's ext4 spelling must name nothing on Windows, where a bare `/Users/...` would land on the drive.
    ext4 = f'/home/trace/{tail}' if match else f'/{tail}'
    return {DRIVE: f'{letter}:/{tail}', MNT: f'/mnt/{letter.lower()}/{tail}', EXT4: ext4}


def mask(text, root):
    for placeholder, spelling in spellings(root).items():
        text = text.replace(spelling, placeholder)
    return text


def expand(text, root, side):
    """The recording side's native form names `root` itself, so a relative pointer lands where the rows say."""
    forms = {**spellings(root), NATIVE[side]: str(root).replace('\\', '/').rstrip('/')}
    for placeholder, spelling in forms.items():
        text = text.replace(placeholder, spelling)
    return text


def rows_of(stdout):
    """git's rows as listed, each block's fields as they stand; a second parser, so replaying `entries` is a check."""
    rows = []
    for block in stdout.split('\0\0'):
        row = {key: value for key, _, value in (field.partition(' ') for field in block.split('\0') if field)}
        if 'worktree' in row:
            rows.append(row)
    return rows


class Tree:
    """One case's temp tree. Real git runs here; nothing outside this root is written."""

    def __init__(self, root, config):
        self.root, self.repo, self.wt = root, root / 'repo', root / 'wt'
        self.common = self.repo / '.git'
        self.markers = {}
        self.env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        self.env.update(HOME=str(root), GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=str(config),
                        GIT_AUTHOR_NAME='trace', GIT_AUTHOR_EMAIL='trace@example.invalid',
                        GIT_AUTHOR_DATE='2026-11-02T00:00:00Z', GIT_COMMITTER_NAME='trace',
                        GIT_COMMITTER_EMAIL='trace@example.invalid', GIT_COMMITTER_DATE='2026-11-02T00:00:00Z')

    @property
    def admin(self):
        return self.common / 'worktrees' / 'wt'

    def run(self, *args, where=None, stdin=b''):
        return subprocess.run(['git', '-C', str(where or self.repo), *args], input=stdin, capture_output=True,
                              env=self.env, timeout=60)

    def git(self, *args, where=None, stdin=b''):
        done = self.run(*args, where=where, stdin=stdin)
        if done.returncode:
            raise RuntimeError(f'{args}: {done.stderr.decode(errors="replace")}')
        return done.stdout.decode('utf-8').strip()

    def write(self, name, text):
        path = self.admin / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode('utf-8'))

    def pointer(self, form):
        self.write('gitdir', spellings(self.root)[form] + tail + '\n')

    def native(self):
        return str(self.root).replace('\\', '/').rstrip('/')

    def listing(self):
        done = self.run('worktree', 'list', '--porcelain', '-z')
        stdout = mask(done.stdout.decode('utf-8'), self.root)
        return done.returncode, stdout, rows_of(stdout) if done.returncode == 0 else []

    def files(self):
        found = {}
        for folder, directories, names in os.walk(self.root):
            directories[:] = [name for name in directories if name not in SKIP_DIRECTORIES]
            for name in directories:
                path = Path(folder) / name
                if name in ADMIN_FILES and not any(path.iterdir()):
                    found[path.relative_to(self.root).as_posix()] = UNREADABLE
            for name in names:
                if name not in SKIP_FILES:
                    path = Path(folder) / name
                    found[path.relative_to(self.root).as_posix()] = mask(path.read_bytes().decode('utf-8'), self.root)
        return {**found, **self.markers}


def seeded(tree):
    tree.git('init', '-b', 'main', 'repo', where=tree.root)
    tree.git('commit', '--allow-empty', '-m', 'seed')


def linked(tree, *flags):
    seeded(tree)
    tree.git('worktree', 'add', *flags, '-b', 'wt', '../wt')


def detached(tree):
    seeded(tree)
    tree.git('worktree', 'add', '--detach', '../wt')


def bare(tree):
    tree.common = tree.repo
    tree.git('init', '--bare', '-b', 'main', 'repo', where=tree.root)
    empty = tree.git('hash-object', '-t', 'tree', '-w', '--stdin')
    tree.git('update-ref', 'refs/heads/main', tree.git('commit-tree', empty, '-m', 'seed'))
    tree.git('worktree', 'add', '-b', 'wt', '../wt', 'main')


def unlink(path):
    Path(path).unlink()


def as_directory(path):
    Path(path).unlink(missing_ok=True)
    Path(path).mkdir()


def lock(tree, *flags):
    tree.git('worktree', 'lock', *flags, '../wt')


def extension(tree):
    tree.git('config', 'core.repositoryformatversion', '1')
    tree.git('config', 'extensions.droverunknown', 'true')


def ghost(tree):
    (tree.common / 'worktrees' / 'ghost').mkdir()
    (tree.common / 'worktrees' / 'ghost' / 'locked').write_bytes(b'wsl\n')


def stray(tree):
    (tree.common / 'worktrees' / 'stray').write_bytes(b'not an admin directory\n')


def vanish(tree):
    # A race cannot be recorded, so this is the state the reader meets once the directory has gone.
    shutil.rmtree(tree.admin)
    tree.markers[tree.admin.relative_to(tree.root).as_posix()] = VANISHED


class Case(NamedTuple):
    damage: Callable[[Tree], None] = lambda tree: None
    build: Callable[[Tree], None] = linked


CASES = {
    'relative': Case(build=lambda tree: linked(tree, '--relative-paths')),
    'absolute': Case(),
    'drive-letter': Case(lambda tree: tree.pointer(DRIVE)),
    '/mnt': Case(lambda tree: tree.pointer(MNT)),
    'ext4': Case(lambda tree: tree.pointer(EXT4)),
    'pointer without /.git': Case(lambda tree: tree.write('gitdir', tree.native() + '/wt\n')),
    'locked with a reason': Case(lambda tree: lock(tree, '--reason', 'wsl')),
    'locked without a reason': Case(lock),
    'locked with trailing blank lines': Case(lambda tree: tree.write('locked', 'wsl\n\n\n')),
    'locked unreadable': Case(lambda tree: as_directory(tree.admin / 'locked')),
    'commondir empty': Case(lambda tree: tree.write('commondir', '')),
    'commondir a directory': Case(lambda tree: as_directory(tree.admin / 'commondir')),
    'commondir wrong': Case(lambda tree: tree.write('commondir', '../../../elsewhere/.git\n')),
    'commondir missing': Case(lambda tree: unlink(tree.admin / 'commondir')),
    'detached': Case(build=detached),
    'garbage HEAD': Case(lambda tree: tree.write('HEAD', 'not a head\n')),
    'main HEAD garbage': Case(lambda tree: (tree.common / 'HEAD').write_bytes(b'not a head\n')),
    'main HEAD missing': Case(lambda tree: unlink(tree.common / 'HEAD')),
    'main refs missing': Case(lambda tree: shutil.rmtree(tree.common / 'refs')),
    'broken config': Case(lambda tree: (tree.common / 'config').write_bytes(b'[core\n\tbroken\n')),
    'unknown extension': Case(extension),
    'missing directory': Case(lambda tree: shutil.rmtree(tree.wt)),
    'empty gitdir': Case(lambda tree: tree.write('gitdir', '')),
    'only locked': Case(ghost),
    'stray file': Case(stray),
    'directory vanishing': Case(vanish),
    'bare repos': Case(build=bare),
    'main checkout': Case(build=seeded),
}


def record_case(case, root, config):
    tree = Tree(root, config)
    case.build(tree)
    code, stdout, rows = tree.listing()
    if code:
        raise RuntimeError(f'the healthy listing failed in {root}')
    before = dict(stdout=stdout, rows=rows, files=tree.files())
    case.damage(tree)
    code, stdout, rows = tree.listing()
    return dict(before=before, code=code, stdout=stdout, rows=rows, files=tree.files())


def record(side):
    """Real git over every case; by hand only."""
    if side != ('windows' if sys.platform == 'win32' else 'wsl'):
        raise SystemExit(f'--side {side} is not this host')
    with tempfile.TemporaryDirectory(prefix='registration-trace-') as temporary:
        base = Path(temporary).resolve()
        if side == 'wsl' and str(base).startswith('/mnt/'):
            raise SystemExit('the WSL recording needs a temp tree on ext4')
        config = base / 'gitconfig'
        config.write_bytes(b'')
        recording = {}
        for number, (name, case) in enumerate(CASES.items()):
            root = base / f'case{number}'
            root.mkdir()
            recording[name] = record_case(case, root, config)
    text = json.dumps(recording)
    if HOST_PATH.search(text) or base.name in text:
        raise SystemExit('a host path survived the placeholders: ' + str(HOST_PATH.search(text)))
    return recording


def load():
    return json.loads(TRACE_PATH.read_text(encoding='utf-8'))


def recorded(case_name, side, root=None):
    """The recording, with its placeholders expanded against `root` when one is given.

    The stored rows hold git's own spelling. Expanded, `worktree` takes this host's `cleanup_path`, as `entries` gives.
    """
    answer = load()[case_name][side]
    if root is None:
        return copy.deepcopy(answer)

    def text(value):
        return expand(value, root, side)

    def rows(values):
        return [{key: cleanup_path(text(value)) if key == 'worktree' else text(value) for key, value in row.items()}
                for row in values]

    def expanded(part):
        return dict(stdout=text(part['stdout']), rows=rows(part['rows']),
                    files={name: text(value) for name, value in part['files'].items()})

    return dict(expanded(answer), before=expanded(answer['before']), code=answer['code'])


def materialize(case_name, side, root, *, damaged=True):
    """Rebuild the case's tree under `root` from the recorded bytes, with no git; returns the repo."""
    root = Path(root)
    answer = recorded(case_name, side, root)
    for name, content in (answer if damaged else answer['before'])['files'].items():
        path = root / name
        if content == UNREADABLE:
            path.mkdir(parents=True, exist_ok=True)
        elif content != VANISHED:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content.encode('utf-8'))
    return root / 'repo'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--side', choices=sorted(NATIVE), required=True)
    side = parser.parse_args(argv).side
    recording = record(side)
    trace = load() if TRACE_PATH.exists() else {}
    trace = {name: {**trace.get(name, {}), side: recording[name]} for name in CASES}
    TRACE_PATH.write_bytes((json.dumps(trace, indent=1, sort_keys=True) + '\n').encode('utf-8'))
    for name in CASES:
        print(f'{recording[name]["code"]:>3} {len(recording[name]["rows"])} {name}')


if __name__ == '__main__':
    main()
