"""Policy harness using real dispatch/ledger logic and memory repository facts."""
import hashlib
import json
from pathlib import Path

from drover import ci, config, origin, placement, repos
from tests import rcw_repo
from tests.rcw_repo import MemoryRepo, MemoryRepository

class PolicyCase:
    """Real pipeline/dispatcher/ledger, memory repository observations and receipts."""
    def setUp(self):
        import tempfile
        from tests.tiers import MARKER
        if getattr(getattr(type(self), self._testMethodName), MARKER, False):
            from tests.test_pipeline import Base
            Base.setUp(self)
            return
        from unittest.mock import patch
        from drover import pipeline, task_access
        from drover.replan import Replanning
        from drover.ledger import connect, ops
        from drover.port import DirectPort
        from tests.test_pipeline import FakeLauncher, prog_dict
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.fix = MemoryRepo(root / 'source')
        self.source = MemoryRepository(self.fix)
        original = config.get()
        self.addCleanup(config.use, original)
        # Placement is faked; this Linux setting must also validate on Windows.
        cfg = config.validate({'paths': {'state_dir': str(root / 'state'),
                                        'wsl_worktrees': '/home/test/work'}})
        config.use(cfg)
        self.conn = connect(root / 'l.db')
        self.addCleanup(lambda: self.conn.close())
        self.port = DirectPort(self.conn)
        self.runs = root / 'runs'
        self.runs.mkdir()
        self.decisions = []
        # Keep the real _create method: it owns the origin wait/retry protocol.
        def add(pipe, repo, dest, branch, base, remote='origin'):
            self.source.add(repo, dest, branch, base, remote)
        def not_main(path, repo, prog):
            if rcw_repo.key(path) == rcw_repo.key(repo) or self.source.branch(path) == repos.default_branch(prog):
                raise pipeline.WorktreeRefused('main checkout')
        def registered(pipe, repo, branch):
            f = self.source.fixture(repo)
            return next(((p, True) for p, b in f.checkouts.items() if b == branch), (None, False))
        def tag(path, name, owner, expires, **kw):
            self.source.fixture(path).tags[rcw_repo.key(path)] = {'owner': owner, 'repo': name}
        # The fake repository stores both placements on this host's temp filesystem.
        contexts = [patch.object(placement, 'wsl_worktrees_root', return_value=str(root / 'ext4')),
                    patch.object(placement, 'drive', return_value='T'),
                    patch.object(pipeline.Repository, 'start_ref', lambda adapter, repo, branch, base, remote:
                                 'refs/heads/' + branch if branch in self.source.fixture(repo).branches else base),
                    patch.object(repos, 'load', self.source.load),
                    patch.object(placement, 'reachable', self.source.reachable),
                    patch.object(placement, 'prepare_worktree', self.source.prepare_worktree),
                    patch.object(pipeline.Pipeline, '_add', add),
                    patch.object(pipeline.Pipeline, '_not_main', staticmethod(not_main)),
                    patch.object(pipeline.Pipeline, '_worktree_of_branch', registered),
                    patch.object(pipeline.Pipeline, '_owned', staticmethod(self.source.owned)),
                    patch.object(pipeline.ownership, 'tag', tag),
                    patch.object(origin, 'sync', lambda repo, remote='origin', main='main', **kw: self.source.sync(repo, remote, main)),
                    patch.object(ci, 'head', self.source.head),
                    patch.object(ci, 'dirty', lambda repo, **kw: self.source.dirty(repo)),
                    patch.object(pipeline.Pipeline, 'diff_text', lambda pipe, repo, base, tip: self.source.diff_text(repo, base, tip)),
                    patch.object(pipeline.Pipeline, 'diff_lines', lambda pipe, repo, base, tip: self.source.diff_lines(repo, base, tip)),
                    patch.object(ci.CI, 'prepare', self.prepare_ci),
                    patch.object(Replanning, '_inputs', lambda planning, task: self.inputs(planning, task)),
                    patch.object(task_access, 'prepare', self.reads),
                    patch.object(task_access, 'verify', self.verify_reads),
                    patch('subprocess.Popen', side_effect=AssertionError('policy tests must not start a process')),
                    patch('time.sleep', side_effect=AssertionError('policy tests must not sleep'))]
        for context in contexts:
            context.start()
            self.addCleanup(context.stop)
        # Avoid prog_dict's Git script installation: MemoryRepo already has the script.
        self.prog = prog_dict(self.fix.repo, root / 'wts')
        self.pipe = pipeline.Pipeline(self.port, repos_map={'p': self.prog}, runs_dir=self.runs)
        for method in ('resolve', 'ancestor', 'changes', 'trailers', 'commit', 'head', 'changed_paths'):
            setattr(self.pipe.repository, method, getattr(self.source, method))
        self.fl = FakeLauncher(self.runs)
        self.fl.launch = self.launch
        ops.set_setting(self.conn, 'dispatch_enabled', 'true')
        self.d = self.make()

    def new_repo(self):
        f = MemoryRepo(Path(self.tmp.name) / f'source{len(self.source.fixtures)}')
        self.source.fixtures.append(f)
        return f

    def reads(self, entries, configured=None, root=None):
        from drover.task_context import split_read
        configured = configured or self.pipe.all_repos()
        result = []
        for entry in entries:
            name, branch = split_read(entry)
            f = self.source.fixture(repos.repo_path(configured[name.lower()]))
            branch = branch or 'main'
            if branch not in f.branches:
                raise ValueError('unknown read branch ' + branch)
            sha = f.head(branch)
            dest = Path(self.tmp.name) / 'reads' / (name.lower() + '-' + sha[:12])
            if not dest.exists():
                f.checkout(dest, sha, sha)
            read = dict(entry=entry, path=str(dest), sha=sha)
            self.verify_reads([read])
            result.append(read)
        return result

    def verify_reads(self, reads):
        for read in reads:
            if self.source.head(read['path']) != read['sha'] or self.source.dirty(read['path']):
                raise ValueError('read-only checkout changed')

    def inputs(self, planning, task):
        prog = planning.pipeline.repo(task)
        if prog is None:
            from drover.pipeline import WorktreeRefused
            raise WorktreeRefused('replan repo unavailable')
        reads = self.reads(task.get('read_access') or [repos.repo_name(prog)], planning.pipeline.all_repos())
        checkout = task.get('worktree')
        if not checkout or not self.source.reachable(checkout):
            own = self.reads([repos.repo_name(prog) + '@' + (task.get('branch') or repos.default_branch(prog))],
                             planning.pipeline.all_repos())
            checkout = own[0]['path']
            reads = own + [r for r in reads if r not in own]
        return dict(repo=repos.repo_name(prog), branch=self.source.branch(checkout),
                    head=self.source.head(checkout), checkout=str(checkout), read_checkouts=reads,
                    dirty_digest=hashlib.sha256(repr(self.source.fixture(checkout).status(checkout)).encode()).hexdigest())

    def prepare_ci(self, prog, checkout, commit, run_id, *, kind='checks', platform=None, base=None, priority='P2'):
        from tests.dispatch_fixture import FixtureCI
        return FixtureCI.prepare(self.pipe.ci, prog, checkout, commit, run_id, kind=kind, platform=platform,
                                 base=base, priority=priority)

    def launch(self, spec):
        from tests.test_pipeline import FakeHandle
        self.fl.specs.append(spec)
        if spec.get('role') == 'gate':
            receipt, sha = Path(spec['argv'][1]), spec['argv'][2]
            receipt.write_text(json.dumps(dict(version=ci.VERSION, sha=sha, passed=True, status='passed',
                exit_code=0, tree_unchanged=True, kind='checks', log_tail=[])))
            value = dict(status='done', returncode=0, record=None, text='', timeout=None,
                         usage=dict(tokens_in=0, tokens_out=0, tokens_cached=0, context=0))
            handle = FakeHandle(value)
        else:
            handle = FakeHandle()
        self.fl.handles[spec['run_id']] = handle
        return handle

from tests.test_pipeline import Base as RealPipelineBase


class PipelineBase(PolicyCase, RealPipelineBase):
    pass
