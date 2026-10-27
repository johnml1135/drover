"""Landing policy fixtures: a commit graph and receipts, with no child processes.

Repository operations are semantic (branch, replay, sync, advance). The narrow
landing.git stub only answers observations and acknowledges scratch cleanup; it
cannot execute or emulate Git mutations. Real Git contracts live in test_landing.
"""
import copy
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from drover import ci, config, landing, origin, ownership
from drover.ledger import db, ops


class CommitGraph:
    def __init__(self, root):
        self.root = root
        self.repo = root / 'repo'
        self.work = root / 'batch-wt'
        self.nodes = {}
        self.heads = {'main': self.commit(None, {'base.txt': 'base\n'})}
        self.owner_files = dict(self.nodes[self.heads['main']][1])
        self.remote = self.tracking = self.heads['main']
        self.work_head = None
        self.work_active = False
        self.changes = {}
        self.replays = {}
        self.conflicts = set()
        self.offline = self.push_failed = False
        self.pushes = []

    def commit(self, parent, files):
        sha = f'{len(self.nodes) + 1:040x}'
        self.nodes[sha] = (parent, {**(self.nodes[parent][1] if parent else {}), **files})
        return sha

    def branch(self, name, files, base=None):
        self.heads[name] = self.commit(base or self.tracking, files)
        self.changes[name] = files
        return self.heads[name]

    def owner_commit(self, files):
        self.heads['main'] = self.commit(self.heads['main'], files)
        self.owner_files = dict(self.nodes[self.heads['main']][1])
        return self.heads['main']

    def ancestor(self, older, newer):
        while newer:
            if newer == older:
                return True
            newer = self.nodes[newer][0]
        return False

    def branch_head(self, repo, branch):
        return self.heads.get(branch)

    def task_files(self, repo, base, branch):
        return list(self.changes[branch])

    def replay(self, repo, work, base, tasks):
        # Script replay conflicts explicitly: policy tests do not implement Git's
        # three-way merge algorithm. The real replay contract covers that adapter.
        tip, applied, conflicts = base, [], []
        for task in tasks:
            if task['id'] in self.conflicts:
                conflicts.append(task['id'])
                continue
            key = (tip, task['head'])
            if key not in self.replays:
                self.replays[key] = self.commit(tip, self.changes[task['branch']])
            tip = self.replays[key]
            applied.append({**task, 'last_sha': tip})
        self.work_head = tip
        self.work_active = True
        return tip, applied, conflicts

    def observe(self, repo, *args, **kwargs):
        # These are the only low-level calls left in the policy driver. Refuse
        # any new command, particularly a mutation, rather than fake its success.
        observations = {
            ('rev-parse', 'main'): self.heads['main'],
            ('rev-parse', 'refs/heads/main'): self.heads['main'],
            ('rev-parse', '--path-format=absolute', '--git-common-dir'):
                str(Path(repo) / '.git'),
            ('worktree', 'remove', '--force', str(self.work)): '',
        }
        if args[:2] == ('merge-base', '--is-ancestor') and len(args) == 4:
            return subprocess.CompletedProcess(args, 0 if self.ancestor(*args[2:]) else 1, '', '')
        if args == ('worktree', 'remove', '--force', str(self.work)):
            self.work_active = False
        if args not in observations:
            raise AssertionError(f'unexpected landing repository observation: {args}')
        return subprocess.CompletedProcess(args, 0, observations[args], '')

    def sync(self, repo, remote, branch, **kwargs):
        if self.offline:
            raise origin.FetchFailed('fetch failed: origin/main; repo paused until owner retries')
        self.tracking = self.remote
        local = self.heads[branch]
        if self.ancestor(self.remote, local):
            return local
        if not self.ancestor(local, self.remote):
            raise origin.SyncError('main diverged from origin')
        self.advance(repo, branch, self.remote, local)
        return self.remote

    def advance(self, repo, branch, new, old, **kwargs):
        if self.owner_files != self.nodes[self.heads[branch]][1]:
            raise origin.DirtyCheckout('main checkout dirty; owner intervention required')
        if self.heads[branch] != old or not self.ancestor(old, new):
            raise origin.MainMoved('main moved during fast-forward; retry')
        self.heads[branch] = new
        self.owner_files = dict(self.nodes[new][1])

    def push(self, repo, remote, branch, guard=None):
        if guard:
            guard()
        self.pushes.append(self.heads[branch])
        if self.push_failed:
            raise origin.SyncError('push failed: origin/main; local landing is retained')
        self.remote = self.tracking = self.heads[branch]

    def gate(self, checkout, commit, tasks):
        files = self.nodes[commit][1]
        bad = [name for name, text in files.items() if 'BAD' in text]
        log = [f'bad content in {name}' for name in bad]
        if 'x.txt' in files and 'y.txt' in files:
            bad.append('shared')
            log.append('x and y clash')
        return dict(version=ci.VERSION, sha=commit, passed=not bad,
                    status='failed' if bad else 'passed', exit_code=int(bool(bad)),
                    tree_unchanged=True, log_tail=log, log='memory gate')


class ProcessFreeCase(unittest.TestCase):
    def setUp(self):
        for context in (
            patch('subprocess.Popen', side_effect=AssertionError('policy tests must not start a process')),
            patch('time.sleep', side_effect=AssertionError('policy tests must not sleep')),
        ):
            context.start()
            self.addCleanup(context.stop)


class MemoryLandingCase(ProcessFreeCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix='drover-landing-policy-')
        self.addCleanup(temporary.cleanup)
        self.r = CommitGraph(Path(temporary.name))
        self.work = self.r.work
        self.conn = sqlite3.connect(':memory:', isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA foreign_keys=ON')
        db.migrate(self.conn)
        self.addCleanup(self.conn.close)
        original = config.get()
        self.addCleanup(config.use, original)
        cfg = copy.deepcopy(original)
        cfg['paths']['state_dir'] = str(self.r.root / 'state')
        config.use(cfg)
        self.gate = self.r.gate
        for context in (
            patch.object(landing, 'git', side_effect=self.r.observe),
            patch.object(landing, 'branch_head', side_effect=self.r.branch_head),
            patch.object(landing, 'task_files', side_effect=self.r.task_files),
            patch.object(landing, 'build_tip', side_effect=self.r.replay),
            patch.object(origin, 'sync', side_effect=self.r.sync),
            patch.object(origin, 'fast_forward', side_effect=self.r.advance),
            patch.object(origin, 'push_main', side_effect=self.r.push),
            patch.object(ci, 'head', side_effect=lambda repo: self.r.work_head),
            patch.object(ci, 'dirty', return_value=''),
            patch.object(ci, 'contains', side_effect=lambda repo, ref, sha:
                         (self.r.heads['main'], self.r.ancestor(self.r.heads['main'], sha))),
            patch.object(ownership, 'owned_by', return_value=False),
        ):
            context.start()
            self.addCleanup(context.stop)

    def task(self, name, files, priority='P2', review='LAND', repo_path=None):
        head = self.r.branch(name, files)
        task = ops.create_task(self.conn, name, 'p', repo_path=str(repo_path or self.r.repo), priority=priority)
        ops.update_task(self.conn, task['id'], branch=name)
        ops.approve(self.conn, task['id'])
        for status in ('claimed', 'running', 'gated', 'reviewed'):
            ops.set_status(self.conn, task['id'], status, actor='system')
        if review:
            run = ops.start_run(self.conn, task['id'], 'reviewer', 'claude')
            ops.finish_run(self.conn, run, 'ok', review_verdict=f'LAND@{head}' if review == 'LAND' else review)
        return task['id']

    def status(self, tid):
        return ops.get_task(self.conn, tid)['status']

    def land(self, **kwargs):
        # Import lazily to keep this fixture independent of test discovery.
        from tests.test_landing import fake_decider
        kwargs.setdefault('push', True)
        kwargs.setdefault('decider', fake_decider())
        return landing.run_batch(self.conn, self.r.repo, self.gate, workdir=self.work, **kwargs)
