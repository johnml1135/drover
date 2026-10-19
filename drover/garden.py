"""Garden: classify worktrees, propose one owner batch, remove only what is approved, tag, purge build output.

python -m drover.garden tag WORKTREE --repo-name P --kind slice|station [--owner A] [--purpose T]
python -m drover.garden propose --repo R [--db LEDGER] [--base origin/main] [--json]
python -m drover.garden execute --db LEDGER --repo R --question N [--apply]
python -m drover.garden remove-merged --repo R [--apply]
python -m drover.garden purge WORKTREE [--apply]

Clean worktrees whose branch is merged or patch-equivalent (`git cherry` shows no '+') are removable by script.
Unmerged ones get a `branch_superseded` decision (noul); below tau they are marked needs-haiku-verdict. Dirty and
unmerged worktrees go into one ledger question the owner approves in one tap. Removal is by path, never `worktree
prune`, and a dirty worktree is removed only when it was listed as dirty in the approved question.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from .winproc import NO_WINDOW

DAY = 86400
TAG = 'drover-tag.json'
SIDE_LOCKS = ('wsl', 'windows')
BUILD_DIRS = ('obj', 'artifacts')        # purged whole when ignored and untracked
BIN_CHILDREN = ('Debug', 'Release')      # bin/ itself may hold caches (bin/.cache) that are evidence: report only
APPROVE = 'Approve all'
DENY = 'Deny all'
ITEMS_MARK = '<!-- garden-items '
EPIC_TITLE = 'Garden'


def git(cwd, *args, check=False):
    # GIT_OPTIONAL_LOCKS=0: `git status` must not rewrite the index, so a scan writes nothing.
    r = subprocess.run(['git', '-C', str(cwd), *args], text=True, capture_output=True, errors='replace',
                       env=dict(os.environ, GIT_OPTIONAL_LOCKS='0'), **NO_WINDOW)
    if check and r.returncode:
        raise RuntimeError('git %s: %s' % (' '.join(args), r.stderr.strip()))
    return r


def this_side():
    return 'windows' if os.name == 'nt' else 'wsl'


def side_of(path):
    return 'windows' if re.match(r'^[A-Za-z]:', path) else 'wsl'


def to_local(path):
    """A Windows path read from git metadata, as this side can open it."""
    m = re.match(r'^([A-Za-z]):[\\/](.*)$', path)
    if m and os.name != 'nt':
        return '/mnt/%s/%s' % (m.group(1).lower(), m.group(2).replace('\\', '/'))
    return path


def common_dir(repo):
    out = git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir', check=True).stdout.strip()
    return Path(to_local(out))


def state_dir():
    return Path(os.environ.get('DROVER_STATE_DIR') or Path.home() / '.local/state/drover')


def _mtime(path):
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


# ---- tags -----------------------------------------------------------------------------------------------------

def admin_dir_of(worktree):
    """The worktree's admin directory, read from its .git pointer (ids get numeric suffixes; never guess)."""
    pointer = Path(worktree) / '.git'
    text = pointer.read_text().strip() if pointer.is_file() else ''
    if not text.startswith('gitdir: '):
        raise ValueError('%s is not a linked worktree' % worktree)
    admin = Path(to_local(text[len('gitdir: '):]))
    return admin if admin.is_absolute() or re.match(r'^[A-Za-z]:', str(admin)) else (Path(worktree) / admin).resolve()   # --relative-paths


def read_tag(meta):
    try:
        return json.loads((Path(meta) / TAG).read_text())
    except (OSError, ValueError):
        return None


def write_tag(meta, tag):
    path = Path(meta) / TAG
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(tag, sort_keys=True))
    os.replace(tmp, path)


def tag_worktree(worktree, repo_name, kind, owner=None, purpose=None, base='origin/main', now=None):
    meta = admin_dir_of(worktree)
    now = time.time() if now is None else now
    # base_sha is the fork point: a HEAD still there has nothing of its own, so it is never "merged".
    fork = git(worktree, 'merge-base', 'HEAD', base).stdout.strip() or git(worktree, 'rev-parse', 'HEAD').stdout.strip()
    tag = read_tag(meta) or {'v': 1, 'created': now, 'state': 'active', 'state_since': now, 'base_sha': fork or None}
    tag.update(repo=repo_name, kind=kind, owner=owner, purpose=purpose)
    write_tag(meta, tag)
    return tag


# ---- facts ----------------------------------------------------------------------------------------------------

def norm_path(p):
    """Resolved, no trailing separators, forward slashes, casefolded on Windows: the one form paths are compared in."""
    text = str(p).strip().strip('"\'')
    try:
        text = str(Path(text).resolve())
    except (OSError, RuntimeError):
        pass
    text = text.replace('\\', '/').rstrip('/') or '/'
    return text.casefold() if os.name == 'nt' else text


def _cim_rows():
    """(cwd, command line) for every process via CIM; raises on failure or timeout. CIM exposes no cwd."""
    out = subprocess.run(
        ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
         'Get-CimInstance Win32_Process | Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress'],
        capture_output=True, text=True, timeout=30, errors='replace', **NO_WINDOW)
    if out.returncode or not out.stdout.strip():
        raise OSError('CIM query failed: ' + out.stderr.strip()[:200])
    data = json.loads(out.stdout)
    data = [data] if isinstance(data, dict) else data
    return [(None, ' '.join(filter(None, [d.get('ExecutablePath'), d.get('CommandLine')]))) for d in data
            if d.get('ProcessId') != os.getpid()]


class Proc:
    """One snapshot of running processes per run. A worktree is busy when any process has its cwd there or names its
    path on its command line. If the snapshot cannot be taken (no /proc, CIM failed or timed out) everything is
    busy: unknown fails safe, and keeps the worktree.

    `windows` forces the CIM path (tests inject `cim`); the default follows os.name.
    """

    def __init__(self, root=None, windows=None, cim=None):
        self.rows, self.unknown = [], False
        windows = (os.name == 'nt') if windows is None else windows
        self.windows = windows
        if windows:
            try:
                self.rows = (cim or _cim_rows)()
            except (OSError, ValueError, subprocess.SubprocessError):
                self.unknown = True
            return
        self.root = Path(root or os.environ.get('DROVER_GARDEN_PROC') or '/proc')
        if not self.root.is_dir():
            self.unknown = True
            return
        for entry in self.root.iterdir():
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            try:
                cwd = str((entry / 'cwd').resolve(strict=True))
            except FileNotFoundError:
                continue
            except OSError:
                cwd = None
            try:
                cmd = (entry / 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
            except FileNotFoundError:
                continue
            except OSError:
                cmd = None
            self.rows.append((cwd, cmd))

    def busy(self, worktree):
        if self.unknown:
            return True
        worktree = norm_path(worktree)
        for cwd, cmd in self.rows:
            if cwd is not None and (norm_path(cwd) == worktree or norm_path(cwd).startswith(worktree + '/')):
                return True
            if cmd is None and cwd is None:
                return True  # cannot prove safety
            if cmd:
                text = cmd.replace('\\', '/')
                text = text.casefold() if os.name == 'nt' or self.windows else text
                target = worktree.casefold() if self.windows and os.name != 'nt' else worktree
                if re.search(re.escape(target) + r'(?=$|[/\s"\'])', text):
                    return True
        return False


class Entry:
    def __init__(self, meta):
        self.meta = Path(meta)
        self.id = self.meta.name
        raw = (self.meta / 'gitdir').read_text().strip()
        self.side = side_of(raw)
        self.path = Path(to_local(raw)).parent
        if not self.path.is_absolute() and not re.match(r'^[A-Za-z]:', str(self.path)):
            self.path = (self.meta / raw).resolve().parent                    # --relative-paths: relative to the admin dir
        lock = self.meta / 'locked'
        self.lock = lock.read_text().strip() if lock.exists() else None
        head = (self.meta / 'HEAD').read_text().strip()
        self.branch = head[len('ref: refs/heads/'):] if head.startswith('ref: refs/heads/') else None
        self.tag = read_tag(self.meta)
        self.created = _mtime(self.meta / 'commondir') or _mtime(self.meta)
        self.facts = {}

    @property
    def keep_note(self):
        return self.lock not in (None, '') and self.lock not in SIDE_LOCKS


def entries(repo):
    common = common_dir(repo)
    found = []
    for meta in sorted((common / 'worktrees').glob('*')):
        if (meta / 'gitdir').is_file() and (meta / 'HEAD').is_file():
            found.append(Entry(meta))
    return common, found


def _fresh(meta, head):
    """True when HEAD never moved off `head` in this worktree's reflog: nothing was ever committed or checked out."""
    try:
        lines = (Path(meta) / 'logs' / 'HEAD').read_text().splitlines()
    except OSError:
        return True
    return all(line.split()[1:2] == [head] for line in lines if line.strip())


def merge_state(wt, head, base, tag=None, meta=None):
    """unchanged | merged | patch-merged | unmerged. Cheapest test first: ancestry, `git cherry`, one tree merge."""
    base_sha = git(wt, 'rev-parse', base).stdout.strip()
    if (tag and tag.get('base_sha') and head == tag['base_sha']) or (not tag and head == base_sha and (meta is None or _fresh(meta, head))):
        return 'unchanged'  # nothing committed since creation: an ancestor of base, but not "merged"
    if git(wt, 'merge-base', '--is-ancestor', head, base).returncode == 0:
        return 'merged'
    cherry = git(wt, 'cherry', base, head).stdout.splitlines()
    if cherry and not any(line.startswith('+') for line in cherry):
        return 'patch-merged'
    # A multi-commit branch squash-merged has no per-commit match; merging it into base then adds nothing.
    merged = git(wt, 'merge-tree', '--write-tree', base, head)
    if merged.returncode == 0 and merged.stdout.split()[:1] == [git(wt, 'rev-parse', base + '^{tree}').stdout.strip()]:
        return 'patch-merged'
    return 'unmerged'


def inspect(entry, base, proc, now=None, min_age_days=1.0):
    """Fill entry.facts: status (missing|busy|uninspectable|dirty|clean), dirty count, head, merge."""
    f = entry.facts
    now = time.time() if now is None else now
    if not entry.path.is_dir():
        f['status'] = 'missing'
        return
    if proc.busy(entry.path):
        f['status'] = 'busy'
        return
    status = git(entry.path, 'status', '--porcelain', '--untracked-files=normal')
    if status.returncode:
        f['status'] = 'uninspectable'
        return
    f['dirty'] = len(status.stdout.splitlines())
    f['status'] = 'dirty' if f['dirty'] else 'clean'
    f['head'] = git(entry.path, 'rev-parse', 'HEAD').stdout.strip()
    if now - entry.created < min_age_days * DAY:
        f['merge'] = 'young'      # a young worktree is kept whatever its merge state
        return
    f['merge'] = merge_state(entry.path, f['head'], base, entry.tag, entry.meta)


def classify(entry):
    """removable | dirty | unmerged | keep, with the reason."""
    f = entry.facts
    s = f.get('status')
    if s in ('missing', 'busy', 'uninspectable'):
        return 'keep', {'missing': 'directory missing', 'busy': 'a live process uses it',
                        'uninspectable': 'git cannot inspect it'}[s]
    if entry.side != this_side():
        return 'keep', "the other side's worktree"
    if entry.keep_note:
        return 'keep', 'lock note %r' % entry.lock
    if (entry.tag or {}).get('kind') == 'station':
        return 'keep', 'station'
    if f['merge'] in ('young', 'unchanged'):
        return 'keep', 'young or unchanged since creation'
    if s == 'dirty':
        return 'dirty', '%d uncommitted file(s)' % f['dirty']
    if f['merge'] in ('merged', 'patch-merged'):
        return 'removable', f['merge']
    return 'unmerged', 'commits not in base'


# ---- decisions on unmerged branches ----------------------------------------------------------------------------

def branch_state(entry, base, limit=30):
    """Text for the decider: commit messages, diffstat and the main commits that look like this branch's work."""
    wt, head = entry.path, entry.facts['head']
    commits = git(wt, 'log', '--no-merges', '--format=%h %s', '%s..%s' % (base, head)).stdout.splitlines()[:limit]
    stat = git(wt, 'diff', '--stat', '%s...%s' % (base, head)).stdout.splitlines()[-40:]
    fork = git(wt, 'merge-base', base, head).stdout.strip()
    files = git(wt, 'diff', '--name-only', '%s...%s' % (base, head)).stdout.split()[:20]
    seen, matches = set(), []
    for line in commits[:8]:
        subject = line.split(' ', 1)[-1]
        for hit in git(wt, 'log', base, '--fixed-strings', '--grep=' + subject, '-3', '--format=%h %s').stdout.splitlines():
            if hit not in seen:
                seen.add(hit)
                matches.append('same subject: ' + hit)
    if fork and files:
        for hit in git(wt, 'log', '%s..%s' % (fork, base), '-15', '--format=%h %s', '--', *files).stdout.splitlines():
            if hit not in seen:
                seen.add(hit)
                matches.append('touches same files: ' + hit)
    return ('branch: %s\ncommits not in base:\n%s\n\ndiffstat:\n%s\n\nmain commits that may match:\n%s\n'
            % (entry.branch or head[:12], '\n'.join(commits), '\n'.join(stat), '\n'.join(matches) or '(none)'))


def verdict_for(entry, base, decider=None, recorder=None):
    """superseded | valuable | needs-haiku-verdict, from one `branch_superseded` question."""
    if decider is None:
        from . import decide
        decider = decide.decide
    try:
        r = decider('branch_superseded', branch_state(entry, base), recorder=recorder)
    except Exception:
        return 'needs-haiku-verdict'
    if r.get('acted') and isinstance(r.get('answer'), bool):
        return 'superseded' if r['answer'] else 'valuable'
    return 'needs-haiku-verdict'


def _db_recorder(conn):
    from .ledger import ops

    def record(r):
        answer = r.get('answer') if r.get('answer') is not None else r.get('shadow_answer')
        ops.record_decision(conn, r['name'], str(r.get('criteria_version')), r.get('model') or 'none',
                            r.get('state_hash') or '', answer, r.get('probabilities'), r.get('confidence'),
                            shadow=not r.get('acted'), rule_answer=r.get('rule_answer'))
    return record


# ---- the proposal -----------------------------------------------------------------------------------------------

MAX_UNTRACKED_BYTES = 200 * 1024 * 1024


def _untracked_digest(worktree, names):
    """(sha256 over name, size and streamed content of every untracked file, total bytes, error or None)."""
    outer, total = hashlib.sha256(), 0
    for name in sorted(names):
        path = Path(worktree) / name
        try:
            st = os.lstat(path)
            total += st.st_size
            if total > MAX_UNTRACKED_BYTES:
                return None, total, 'untracked files exceed %d MB' % (MAX_UNTRACKED_BYTES // 1024 // 1024)
            inner = hashlib.sha256()
            if os.path.islink(path):
                inner.update(os.readlink(path).encode('utf-8', 'replace'))
            elif os.path.isdir(path):
                inner.update(b'dir')          # a nested repository: its own contents are not ours to hash
            else:
                with open(path, 'rb') as f:
                    for chunk in iter(lambda: f.read(1 << 20), b''):
                        inner.update(chunk)
        except OSError as e:
            return None, total, 'cannot read untracked file %s: %s' % (name, e.strerror or e)
        outer.update(('%s\0%d\0%s\n' % (name, st.st_size, inner.hexdigest())).encode('utf-8', 'replace'))
    return outer.hexdigest(), total, None


def dirt_snapshot(worktree):
    """What is uncommitted right now: porcelain lines, a digest of every untracked file (name, size, content) and a
    hash of the tracked diff. `error` is set when the digest could not be taken; such a snapshot proves nothing."""
    porcelain = git(worktree, 'status', '--porcelain', '--untracked-files=normal').stdout.splitlines()
    untracked = git(worktree, 'ls-files', '-o', '--exclude-standard').stdout.splitlines()
    tracked = git(worktree, 'diff', 'HEAD').stdout
    digest, total, error = _untracked_digest(worktree, untracked)
    return {'porcelain': porcelain, 'untracked_hash': digest, 'untracked_bytes': total, 'error': error,
            'diff_hash': hashlib.sha256(tracked.encode('utf-8', 'replace')).hexdigest()}


def item_of(entry, kind, reason, verdict=None):
    f = entry.facts
    return {'path': str(entry.path), 'branch': entry.branch, 'head': f.get('head'), 'kind': kind, 'reason': reason,
            'verdict': verdict, 'dirty': f.get('dirty', 0),
            'dirt': dirt_snapshot(entry.path) if entry.path.is_dir() else None}


def propose(repo, base='origin/main', decider=None, recorder=None, proc=None, now=None, min_age_days=1.0,
            mark=True, repo_name=None):
    """Survey this side's linked worktrees. -> {removable: [...], owner: [...], valuable: [...], keep: [...]}.

    owner items need one owner approval (dirty, or unmerged with any verdict). `mark` writes the
    needs-haiku-verdict state into the worktree's tag; nothing else is written.
    """
    common, found = entries(repo)
    proc = proc or Proc()
    out = {'removable': [], 'owner': [], 'valuable': [], 'keep': []}
    for entry in found:
        if entry.side != this_side() or (repo_name and (entry.tag or {}).get('repo') != repo_name):
            continue
        inspect(entry, base, proc, now, min_age_days)
        kind, reason = classify(entry)
        if kind == 'keep':
            out['keep'].append(item_of(entry, kind, reason))
        elif kind == 'removable':
            out['removable'].append(item_of(entry, kind, reason))
        elif kind == 'dirty':
            out['owner'].append(item_of(entry, kind, reason, 'dirty'))
        else:
            v = verdict_for(entry, base, decider, recorder)
            it = item_of(entry, kind, reason, v)
            (out['valuable'] if v == 'valuable' else out['owner']).append(it)
            if mark and v == 'needs-haiku-verdict' and entry.tag is not None:
                write_tag(entry.meta, dict(entry.tag, state='needs-haiku-verdict'))
    return out


def _ensure_epic(conn):
    from .ledger import ops
    for e in ops.list_epics(conn):
        if e['title'] == EPIC_TITLE:
            return e['id']
    return ops.create_epic(conn, 'drover', EPIC_TITLE, plan_md='Worktree and branch clean-up batches.')['id']


def ask_owner(conn, items, repo):
    """One ledger question listing every owner item; returns its id (None when there is nothing to ask)."""
    from .ledger import ops
    if not items:
        return None
    lines = ['%s  [%s]  %s%s' % (i['path'], i['verdict'], i['reason'], '  branch ' + i['branch'] if i['branch'] else '')
             for i in items]
    body = ('Remove these worktrees (by path). Dirty ones lose their uncommitted files; unmerged ones keep a rescue '
            'tag.\n\n' + '\n'.join(lines) + '\n\nTo spare one, approve and reply "keep <path>".\n\n'
            + ITEMS_MARK + json.dumps({'repo': str(repo), 'items': items}) + ' -->')
    return ops.ask_question(conn, 'epic', _ensure_epic(conn), 'Garden: remove %d worktree(s)?' % len(items), body,
                            options=[{'label': APPROVE, 'description': 'remove every listed item', 'recommended': True},
                                     {'label': DENY, 'description': 'remove nothing'}], actor='garden')


def make_tasks_for_valuable(conn, repo, valuable):
    """Branches judged valuable become proposed tasks whose acceptance is 'landed on main'."""
    from .ledger import ops
    made = []
    for it in valuable:
        if any(t['branch'] == it['branch'] for t in ops.list_tasks(conn, include_archived=True)):
            continue
        t = ops.create_task(conn, 'Land branch %s' % it['branch'], 'drover', repo_path=str(repo),
                            goal='This unmerged branch was judged valuable: %s' % it['reason'],
                            acceptance=['landed on main'], actor='garden')
        ops.update_task(conn, t['id'], actor='garden', branch=it['branch'])
        made.append(t['id'])
    return made


# ---- removal ------------------------------------------------------------------------------------------------------

def manifest(record):
    path = state_dir() / 'garden-manifest.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        stream.write(json.dumps(record, sort_keys=True) + '\n')


def checked_out_elsewhere(common, branch, skip_meta):
    if (common / 'HEAD').read_text().strip() == 'ref: refs/heads/' + branch:
        return True
    for meta in (common / 'worktrees').glob('*'):
        if meta != skip_meta and (meta / 'HEAD').is_file() and \
                (meta / 'HEAD').read_text().strip() == 'ref: refs/heads/' + branch:
            return True
    return False


def remove_worktree(common, entry, base, proc, approved_dirt=None, delete_branch=False, expect_head=None):
    """Remove one worktree by path after rechecking it. Never prunes. Returns {ok, reason|record}."""
    repo = common.parent
    if not entry.path.is_dir():
        return {'ok': False, 'reason': 'directory missing'}
    if proc.busy(entry.path):
        return {'ok': False, 'reason': 'became busy'}
    dirty = git(entry.path, 'status', '--porcelain', '--untracked-files=normal').stdout.strip()
    if dirty and approved_dirt is None:
        return {'ok': False, 'reason': 'has uncommitted changes that were not in the approved list'}
    now_dirt = dirt_snapshot(entry.path) if approved_dirt is not None else None
    if approved_dirt is not None and (approved_dirt.get('error') or now_dirt['error'] or now_dirt != approved_dirt):
        # The owner approved a different pile of uncommitted work: keep it all and ask again.
        return {'ok': False, 'repropose': True, 'reason': (approved_dirt.get('error') or now_dirt['error'] or
                   'uncommitted changes differ from the approved snapshot') + '; re-propose'}
    head = git(entry.path, 'rev-parse', 'HEAD').stdout.strip()
    if expect_head and head != expect_head:
        return {'ok': False, 'reason': 'HEAD moved since the proposal'}
    lock_file = entry.meta / 'locked'
    entry.lock = lock_file.read_text().strip() if lock_file.exists() else None
    if entry.keep_note:
        return {'ok': False, 'reason': 'lock note %r' % entry.lock}
    merged = git(repo, 'merge-base', '--is-ancestor', head, base).returncode == 0
    rescue = None
    if not merged:   # commits that no base holds: a rescue tag keeps them reachable and the manifest says how to restore
        rescue = 'refs/tags/drover-rescue/%s-%s' % (entry.id, head[:12])
        git(repo, 'update-ref', rescue, head, check=True)
    if entry.lock is not None:
        git(repo, 'worktree', 'unlock', str(entry.path))
    removed = git(repo, 'worktree', 'remove', '--force', str(entry.path)) if (dirty or (entry.path / '.gitmodules').exists()) \
        else git(repo, 'worktree', 'remove', str(entry.path))
    if removed.returncode:
        if entry.lock is not None:
            git(repo, 'worktree', 'lock', '--reason', entry.lock, str(entry.path))
        return {'ok': False, 'reason': removed.stderr.strip()}
    branch_deleted = False
    if delete_branch and entry.branch and not checked_out_elsewhere(common, entry.branch, entry.meta):
        branch_deleted = git(repo, 'update-ref', '-d', 'refs/heads/' + entry.branch, head).returncode == 0
    restore = 'git -C %s worktree add %s %s' % (repo, entry.path, entry.branch or head)
    if branch_deleted:
        restore = 'git -C %s branch %s %s && %s' % (repo, entry.branch, head, restore)
    record = dict(ts=time.time(), repo=str(repo), path=str(entry.path), branch=entry.branch, head=head,
                  dirty_removed=bool(dirty), branch_deleted=branch_deleted, rescue_ref=rescue, restore=restore)
    manifest(record)
    return dict(ok=True, **record)


DELETE_DISPOSITIONS = ('script-delete', 'propose-delete', 'owner-review')  # rows the owner can approve as a delete


class GitError(RuntimeError):
    pass


def _git_ok(repo, *args):
    """stdout of a git command that must succeed; anything else is a GitError (callers fail closed)."""
    r = git(repo, *args)
    if r.returncode:
        raise GitError('git %s: rc %d %s' % (' '.join(args[:2]), r.returncode, r.stderr.strip()))
    return r.stdout


def default_branch(repo):
    """The remote default branch name (origin/HEAD), else main or master when one exists on origin; None if unknown.
    The name is only a claim: delete_branch verifies refs/remotes/origin/<name> resolves to a commit."""
    r = git(repo, 'symbolic-ref', '--short', 'refs/remotes/origin/HEAD')
    if r.returncode == 0 and r.stdout.strip().startswith('origin/'):
        return r.stdout.strip()[len('origin/'):]
    for name in ('main', 'master'):
        if git(repo, 'rev-parse', '--verify', '-q', 'refs/remotes/origin/' + name).returncode == 0:
            return name
    return None


def checked_out_anywhere(repo, branch):
    out = _git_ok(repo, 'worktree', 'list', '--porcelain').splitlines()
    return ('branch refs/heads/' + branch) in [ln.strip() for ln in out]


def valid_branch_name(repo, branch):
    if not isinstance(branch, str) or not branch or branch.startswith('-') or '@{' in branch or '..' in branch \
            or branch.startswith('refs/') or branch.startswith('/') or '\x00' in branch:
        return False
    return git(repo, 'check-ref-format', '--branch', branch).returncode == 0


ZERO = '0' * 40


def is_merged(repo, sha, upstream):
    """True only on positive evidence: merge-base --is-ancestor rc 0, or `git cherry` rc 0 with output and no '+'.
    rc 1 from merge-base is a clean "no"; any other rc, a cherry error or empty cherry output is NOT merged.
    Raises GitError when merge-base itself fails (rc other than 0 or 1)."""
    rc = git(repo, 'merge-base', '--is-ancestor', sha, upstream).returncode
    if rc == 0:
        return True
    if rc != 1:
        raise GitError('git merge-base: rc %d' % rc)
    r = git(repo, 'cherry', upstream, sha)
    lines = r.stdout.splitlines()
    return r.returncode == 0 and bool(lines) and not any(ln.startswith('+') for ln in lines)


def delete_branch(repo, branch, approved_row, log=None, now=None, rescue_root=None, bundle=False):
    """Delete one local branch that has no worktree, after rechecking it with argv git. Local only: never a remote.

    Fails closed: any git error, an unresolvable origin/<default>, a hostile name, a row that does not name this
    branch, the default branch, a branch checked out anywhere, or one that is gone refuses. Merged (see is_merged):
    delete. Otherwise only a row approved as an unmerged delete proceeds: a never-overwritten
    refs/drover-rescue/<branch>-<date>-<sha> ref (verified) first, and with `bundle` a `git bundle` at
    <rescue_root>/<repo name>/<branch>.bundle (a failed bundle stops the delete). The delete is
    `update-ref -d refs/heads/<b> <sha seen at check time>`, so a branch that moved is kept. `log(step, **data)` is
    called for every step. Returns {ok, reason|..., steps}."""
    steps = []

    def step(name, **data):
        steps.append(dict(step=name, **data))
        if log:
            log(name, branch=branch, **data)

    def refuse(reason):
        step('refused', reason=reason)
        return {'ok': False, 'reason': reason, 'steps': steps}

    try:
        return _delete_branch(repo, branch, approved_row, step, refuse, steps, now, rescue_root, bundle)
    except (GitError, OSError) as e:
        return refuse('git error, nothing deleted: %s' % e)


def _delete_branch(repo, branch, approved_row, step, refuse, steps, now, rescue_root, bundle):
    if not valid_branch_name(repo, branch):
        return refuse('invalid branch name')
    if not approved_row or approved_row.get('branch') != branch:
        return refuse('the row does not name this branch')
    base = default_branch(repo)
    if base is None:
        return refuse('cannot determine the default branch')
    if branch == base or branch in ('main', 'master'):
        return refuse('default branch')
    upstream = 'refs/remotes/origin/' + base
    if git(repo, 'rev-parse', '--verify', '-q', upstream + '^{commit}').returncode:
        return refuse('origin/%s does not resolve to a commit' % base)
    r = git(repo, 'rev-parse', '--verify', '-q', 'refs/heads/' + branch + '^{commit}')
    sha = r.stdout.strip()
    if r.returncode or not re.fullmatch(r'[0-9a-f]{40,64}', sha):
        return refuse('no such local branch')
    if checked_out_anywhere(repo, branch):
        return refuse('checked out in a worktree')
    merged = is_merged(repo, sha, upstream)
    step('checked', head=sha, merged=merged, base='origin/' + base)
    rescue = bundle_path = None
    if not merged:
        if approved_row.get('disposition') not in DELETE_DISPOSITIONS:
            return refuse('unmerged and not approved as a delete (%s)' % approved_row.get('disposition'))
        date = time.strftime('%Y%m%d', time.gmtime(now or time.time()))
        rescue = 'refs/drover-rescue/%s-%s-%s' % (branch, date, sha[:8])
        if git(repo, 'update-ref', rescue, sha, ZERO).returncode:  # old value zero: never overwrite
            return refuse('rescue ref already exists or could not be created')
        if git(repo, 'rev-parse', '--verify', '-q', rescue + '^{commit}').stdout.strip() != sha:
            return refuse('rescue ref does not resolve to the branch tip')
        step('rescue-ref', ref=rescue)
        if bundle:
            root = Path(rescue_root) / Path(repo).name
            root.mkdir(parents=True, exist_ok=True)
            bundle_path = root / (branch.replace('/', '__') + '.bundle')
            if git(repo, 'bundle', 'create', str(bundle_path), 'refs/heads/' + branch).returncode:
                return refuse('could not write the bundle')
            step('bundle', path=str(bundle_path))
    if git(repo, 'update-ref', '-d', 'refs/heads/' + branch, sha).returncode:
        return refuse('branch moved or could not be deleted')
    step('deleted', head=sha, rescue_ref=rescue, bundle=str(bundle_path) if bundle_path else None)
    return {'ok': True, 'head': sha, 'rescue_ref': rescue, 'bundle': str(bundle_path) if bundle_path else None,
            'steps': steps}


def remove_merged(repo, base='origin/main', apply=False, proc=None, now=None, min_age_days=1.0, repo_name=None):
    """The script path: remove clean worktrees whose branch is merged or patch-equivalent. Dry run unless apply."""
    common, found = entries(repo)
    proc = proc or Proc()
    results = []
    for entry in found:
        if entry.side != this_side() or (repo_name and (entry.tag or {}).get('repo') != repo_name):
            continue
        inspect(entry, base, proc, now, min_age_days)
        kind, reason = classify(entry)
        if kind != 'removable':
            continue
        row = {'path': str(entry.path), 'reason': reason}
        if apply:
            row['result'] = remove_worktree(common, entry, base, proc, delete_branch=entry.branch is not None
                                            and reason == 'merged')
        results.append(row)
    return results


def approved_items(conn, qid):
    """(repo, approved items, warnings) from an answered garden question; no items unless the answer was Approve all."""
    from .ledger import ops
    q = ops.get_question(conn, qid)
    if q['status'] != 'answered':
        raise ValueError('question %s is not answered yet' % qid)
    m = re.search(re.escape(ITEMS_MARK) + r'(\{.*\}) -->', q['body'], re.S)
    if not m:
        raise ValueError('question %s is not a garden batch' % qid)
    data = json.loads(m.group(1))
    if q['answer_choice'] != APPROVE:
        return data['repo'], [], []
    spared = [line.split(None, 1)[1].strip() for line in (q['answer_text'] or '').splitlines()
              if line.lower().startswith('keep ') and len(line.split(None, 1)) == 2]
    spared_norm = {norm_path(x) for x in spared}
    listed = {norm_path(i['path']) for i in data['items']}
    warnings = ['keep line matches no listed item: %s' % x for x in spared if norm_path(x) not in listed]
    return data['repo'], [i for i in data['items'] if norm_path(i['path']) not in spared_norm], warnings


def execute_approved(conn, qid, base='origin/main', apply=False, proc=None):
    """Remove exactly the approved items, by path. Each is rechecked; anything that changed is kept (and, when its
    uncommitted work differs from the approved snapshot, marked `repropose`)."""
    from .ledger import ops
    repo, items, warnings = approved_items(conn, qid)
    for w in warnings:
        print('drover.garden: warning: ' + w, file=sys.stderr)
    common, found = entries(repo)
    by_path = {norm_path(e.path): e for e in found}
    proc = proc or Proc()
    results = []
    for it in items:
        entry = by_path.get(norm_path(it['path']))
        if entry is None:
            results.append({'path': it['path'], 'ok': False, 'reason': 'no such linked worktree'})
            continue
        if entry.side != this_side():
            results.append({'path': it['path'], 'ok': False, 'reason': "the other side's worktree"})
            continue
        if not apply:
            results.append({'path': it['path'], 'ok': True, 'dry_run': True})
            continue
        if it.get('dirt') is None:
            results.append({'path': it['path'], 'ok': False, 'repropose': True, 'reason': 'no dirt snapshot; re-propose'})
            continue
        r = remove_worktree(common, entry, base, proc, approved_dirt=it['dirt'],
                            delete_branch=bool(entry.branch) and it.get('kind') == 'unmerged', expect_head=it.get('head'))
        results.append(dict(r, path=it['path']))
    if apply:
        ops.event(conn, 'garden-executed', 'garden', {'question': qid, 'removed': [r['path'] for r in results if r.get('ok')],
                                                      'kept': [r['path'] for r in results if not r.get('ok')],
                                                      'warnings': warnings})
    return results


# ---- build-output purge ---------------------------------------------------------------------------------------------

def purgeable(worktree):
    """Ignored, untracked build output: obj/ and artifacts/ whole, bin/Debug and bin/Release. Never bin/.cache."""
    listed = git(worktree, 'ls-files', '-o', '-i', '--exclude-standard', '--directory').stdout.splitlines()
    out, kept = [], []
    for rel in listed:
        rel = rel.rstrip('/')
        name = rel.rsplit('/', 1)[-1]
        if name in BUILD_DIRS:
            candidates = [rel]
        elif name == 'bin':
            candidates = ['%s/%s' % (rel, c) for c in BIN_CHILDREN]
            kept += [p for p in ('%s/.cache' % rel,) if (Path(worktree) / p).exists()]
        else:
            continue
        for c in candidates:
            if (Path(worktree) / c).is_dir() and not git(worktree, 'ls-files', '--', c).stdout.strip():
                out.append(c)
    return out, kept


def du_mb(path):
    total = 0
    for root, _, names in os.walk(path):
        for n in names:
            try:
                total += os.lstat(os.path.join(root, n)).st_size
            except OSError:
                pass
    return total // (1024 * 1024)


def purge(worktree, apply=False):
    """-> (freed MB, purged dirs, kept dirs). Dry run unless apply."""
    dirs, kept = purgeable(worktree)
    freed = 0
    for rel in dirs:
        target = Path(worktree) / rel
        freed += du_mb(target)
        if apply:
            shutil.rmtree(target, ignore_errors=True)
    return freed, dirs, kept


# ---- CLI --------------------------------------------------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(prog='python -m drover.garden', description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest='command', required=True)
    t = sub.add_parser('tag')
    t.add_argument('worktree')
    t.add_argument('--repo-name', required=True)
    t.add_argument('--kind', choices=('slice', 'station'), required=True)
    t.add_argument('--owner')
    t.add_argument('--purpose')
    t.add_argument('--base', default='origin/main')
    for name in ('propose', 'execute', 'remove-merged'):
        s = sub.add_parser(name)
        s.add_argument('--repo')
        s.add_argument('--base', default='origin/main')
        s.add_argument('--json', action='store_true')
        s.add_argument('--min-age-days', type=float, default=1.0)
        if name != 'remove-merged':
            s.add_argument('--db')
        if name == 'propose':
            s.add_argument('--repo-name')
        else:
            s.add_argument('--apply', action='store_true')
        if name == 'execute':
            s.add_argument('--question', type=int, required=True)
    u = sub.add_parser('purge')
    u.add_argument('worktree')
    u.add_argument('--apply', action='store_true')
    a = p.parse_args(argv)
    try:
        return _dispatch(a)
    except (RuntimeError, ValueError, OSError) as e:
        print('drover.garden: %s' % e, file=sys.stderr)
        return 2


def _conn(db):
    from .ledger.db import connect, default_path
    return connect(db or default_path())


def _dispatch(a):
    if a.command == 'tag':
        print(json.dumps(tag_worktree(a.worktree, a.repo_name, a.kind, a.owner, a.purpose, a.base)))
        return 0
    if a.command == 'purge':
        freed, dirs, kept = purge(a.worktree, a.apply)
        print('%s %d MB from %s; left %s' % ('freed' if a.apply else 'would free', freed, dirs or '-', kept or '-'))
        return 0
    if a.command == 'remove-merged':
        rows = remove_merged(a.repo, a.base, a.apply, min_age_days=a.min_age_days)
        print(json.dumps(rows) if a.json else '\n'.join('%s %s' % (r['path'], r.get('result', 'dry run')) for r in rows))
        return 0
    conn = _conn(a.db)
    if a.command == 'execute':
        rows = execute_approved(conn, a.question, a.base, a.apply)
        print(json.dumps(rows) if a.json else '\n'.join(
            '%s %s' % (r['path'], 'ok' if r.get('ok') else 'kept: ' + r.get('reason', '')) for r in rows))
        return 0
    batch = propose(a.repo, a.base, recorder=_db_recorder(conn), min_age_days=a.min_age_days, repo_name=a.repo_name)
    qid = ask_owner(conn, batch['owner'], a.repo)
    made = make_tasks_for_valuable(conn, a.repo, batch['valuable'])
    batch.update(question=qid, tasks=made)
    print(json.dumps(batch) if a.json else 'removable by script: %d; owner batch: %d (question %s); valuable tasks: %s'
          % (len(batch['removable']), len(batch['owner']), qid, made or '-'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
