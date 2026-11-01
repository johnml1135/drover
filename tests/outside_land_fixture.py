"""Fixtures for outside-landing tests: real git for the proof, a small fake for what sits above it.

`GitCase` and everything built on it is integration tier. `LedgerCase` starts no process, and `FakeGitCase` stands in
for git at `outside_land`'s own seams, so tests of what happens after git has answered stay in the default tier.
"""
import itertools
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from drover import outside_land, repos
from drover.ledger import connect, ops, scope, states
from tests.gitfix import Repo
from tests.tiers import integration


class Ledger:
    """Tasks and recorded runs, as the dispatcher would have left them."""
    def open_ledger(self, root):
        self.conn = connect(Path(root) / 'ledger.db')
        self.addCleanup(self.conn.close)

    def use_config(self, push=False):
        prog = repos.normalize({'name': 'acme', 'repo': self.repo_path(), 'landing': {'push': push}})
        loaded = patch('drover.repos.load_all', return_value={'acme': prog})
        loaded.start()
        self.addCleanup(loaded.stop)

    def task(self, status='needs-owner', branch='feat', **kw):
        kw.setdefault('scope_files', ['f.txt'])
        t = ops.create_task(self.conn, 'T', 'acme', branch=branch, **kw)
        ops.approve(self.conn, t['id'])
        if status == 'needs-owner':
            ops.set_status(self.conn, t['id'], states.NEEDS_OWNER, 'worker', reason='stuck')
        elif status == 'yanked':
            ops.yank(self.conn, t['id'])
        return t['id']

    def worker_done(self, tid, sha, run=1, base=None):
        self.launched(tid, run, base or self.fork)
        ops.event(self.conn, 'worker-done', 'dispatcher', {'run': run, 'sha': sha}, task_id=tid)

    def launched(self, tid, run, base):
        ops.event(self.conn, 'run-started', 'dispatcher', {'run': run, 'role': 'worker', 'backend': 'claude'}, task_id=tid)
        ops.event(self.conn, 'run-launching', 'dispatcher',
                  {'run': run, 'output_snapshot': base and {'base': base}}, task_id=tid)

    def scan(self):
        return outside_land.tick(self.conn, {}, decide=lambda *a, **kw: {'name': 'landed_outside', 'acted': False})

    def status(self, tid):
        return ops.get_task(self.conn, tid)['status']


class LedgerCase(Ledger, unittest.TestCase):
    """A real ledger and no repository. Nothing here starts a process."""
    fork = 'e' * 40

    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix='drover-outside-', ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.open_ledger(self.root)
        self.use_config()

    def repo_path(self):
        return str(self.root / 'acme')


@integration
class GitCase(unittest.TestCase):
    def setUp(self):
        self.r = Repo()
        self.addCleanup(self.r.cleanup)
        self.r.git('checkout', '-q', 'main')
        self.fork = self.main()

    def proof(self, commits=(), branch='feat', base='main'):
        return outside_land.git_proof(self.r.repo, branch, base, commits, self.fork)

    def main(self):
        return self.r.git('rev-parse', 'main')


class ScanCase(Ledger, GitCase):
    def setUp(self):
        super().setUp()
        self.open_ledger(self.r.root)
        self.use_config()

    def repo_path(self):
        return str(self.r.repo)

    def landed_by_hand(self):
        tip = self.r.branch('feat', {'f.txt': 'x'})
        self.r.git('merge', '-q', '--ff-only', 'feat')
        return tip


class Decider:
    """A fake `decide` that also checks no ledger transaction is held across the call."""

    def __init__(self, conn, *, acted=False, answer=None, shadow=None, confidence=0.9, fallback='timeout'):
        self.conn, self.calls = conn, []
        self.result = {'name': 'landed_outside', 'answer': answer if acted else None, 'shadow_answer': shadow,
                       'acted': acted, 'confidence': confidence, 'fallback': None if acted else fallback,
                       'model': 'fake', 'state_hash': 'hash', 'criteria_version': 1, 'probabilities': None}

    def __call__(self, name, state, rule_answer=None, dynamic_criteria=None, **kw):
        assert not self.conn.in_transaction
        self.calls.append(SimpleNamespace(name=name, state=state, rule=rule_answer, criteria=dynamic_criteria,
                                          timeout=kw.get('timeout')))
        return self.result


class Questions:
    """A task git cannot prove, the scan that asks about it, and the owner's side of the question."""
    def scan(self, decide=None):
        self.decider = decide or getattr(self, 'decider', None) or Decider(self.conn)
        return outside_land.tick(self.conn, {}, decide=self.decider)

    def unproved(self, status='needs-owner', hand='g.txt'):
        """A task whose branch never landed, and a hand commit on main that touches its scope."""
        tid = self.task(status, scope_files=['f.txt', 'g.txt'])
        tip = self.r.branch('feat', {'f.txt': 'x'})
        self.worker_done(tid, tip)
        return tid, tip, self.r.commit(hand, msg='hand work')

    def question(self, position=0):
        return ops.open_questions(self.conn)[position]

    def labels(self, question=None):
        return [o['label'] for o in (question or self.question())['options']]

    def answer(self, choice=None, text=None):
        ops.answer_question(self.conn, self.question()['id'], choice=choice, text=text)

    def external(self, tid):
        return ops.events(self.conn, task_id=tid, kind='status')[-1]['data']['external_landing']


class DeciderCase(Questions, ScanCase):
    pass


class FakeRepo:
    """What `outside_land` asks of git, answered from a list: the base's commits, oldest first, and the unlanded rest."""
    def __init__(self):
        self.commits, self.base, self.numbers = {}, [], itertools.count(1)

    def new(self, files, msg):
        sha = f'{next(self.numbers):012x}' + 'a' * 28
        self.commits[sha] = (msg, list(files))
        return sha

    def commit(self, name='c.txt', content='', msg='c'):
        self.base.append(self.new([name], msg))
        return self.base[-1]

    def branch(self, name, files):
        self.tip = self.new(files, f'work on {name}')
        return self.tip

    def merge(self, sha=None):
        self.base.append(sha or self.tip)

    def proof(self, repo, branch, ref, commits, start):
        return {'sha': commits[-1], 'proof': 'ancestor'} if commits and commits[-1] in self.base else None

    def resolve(self, repo, name):
        return name if name in self.commits or name.startswith('refs/') else None

    def overlapping(self, repo, task, ref):
        return [sha for sha in reversed(self.base) if scope.any_overlap(task['scope_files'] or [], self.commits[sha][1])]

    def described(self, repo, shas):
        return {sha: self.commits[sha] for sha in shas}

    def holds(self, repo, task, sha, ref, name):
        return sha in self.base and bool(set(self.commits[sha][1]) & set(self.commits[task['commits'][-1]][1]))

    def out(self, repo, *args):
        assert args[:2] == ('merge-base', '--is-ancestor'), args
        sha, ref = args[2], args[3]                  # git answers for the ref it is asked about, not for any ref
        history = self.base[:self.base.index(ref) + 1] if ref in self.base else [ref] if ref in self.commits else self.base
        return '' if sha in history else None

    def reachable(self, task, sha, base_ref):
        if sha not in self.base:
            raise ValueError('commit is not verifiably reachable from the configured base')
        return self.base[-1]


class FakeGitCase(Questions, LedgerCase):
    """The scan above git: what it asks, what it records, and what it does with an answer."""
    def setUp(self):
        super().setUp()
        self.r = FakeRepo()
        seams = dict(_proof=self.r.proof, _commit=self.r.resolve, ambiguous=self.r.overlapping,
                     _described=self.r.described, _holds=self.r.holds, _out=self.r.out)
        patches = [patch.object(outside_land, name, seam) for name, seam in seams.items()]
        patches.append(patch('drover.ledger.ops_outside._reachable_base', self.r.reachable))
        for each in patches:
            each.start()
            self.addCleanup(each.stop)

    def landed_by_hand(self):
        tip = self.r.branch('feat', {'f.txt': 'x'})
        self.r.merge()
        return tip
