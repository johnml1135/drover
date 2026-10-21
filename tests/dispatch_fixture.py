"""Build dispatchers through the production collaborators, with isolated test state."""
import time

from drover import runner, ci, config

from drover import dispatcher, repos, usage

WORKTREE = r'R:\repos\wt'
GO = {'verdict': 'go', 'slots': 10, 'reasons': []}


class FixtureCI(ci.CI):
    def prepare(self, prog, checkout, commit, run_id, *, kind='checks', platform=None, base=None, priority='P2'):
        out = runner.run_dir(self.runs_dir, run_id) / 'receipt.json'
        out.parent.mkdir(parents=True, exist_ok=True)
        return {'kind': kind, 'diff_lines': 0, 'scope': None, 'files': [], 'sha': commit, 'receipt': str(out),
                'spec': {'run_id': run_id, 'role': 'gate', 'backend': 'local', 'model': 'gate',
                         'placement': platform, 'argv': ['fixture-ci', str(out), commit], 'cwd': checkout}}


class FakePipeline:
    """A configured repo interface; its test runner supplies check receipts and review capacity is unavailable."""
    def __init__(self, port, runs_dir):
        self.port, self.runs_dir = port, runs_dir or str(config.state_path("runs"))
        self.prog = repos.normalize({'name': 'p', 'repo': WORKTREE,
                                     'scripts': {'test': '.drover/scripts/test.ps1'}})
        self.prog['review'].update(backend='codex', model='gpt-6.1-sol', high_risk_model='gpt-6.1-sol', seat=None)
        self.ci = FixtureCI(self.runs_dir)
        self.current_head = lambda: 'abc1234'

    def all_repos(self):
        return {'p': self.prog}

    def repo(self, task):
        return self.prog

    def worktree(self, task):
        self.port.update_task(task['id'], worktree=WORKTREE)
        task['worktree'] = WORKTREE
        return WORKTREE

    def prepare_dispatch(self, task) -> bool:
        return True

    def head(self, task):
        return self.current_head()

    def base_ref(self, prog):
        return prog["default_branch"]

    def diff_text(self, repo, base, sha):
        return "fixture diff"


class Observations:
    """Feed measured readings through the same ledger interface used by production polling."""
    def __init__(self, conn, now, headroom, allow):
        self.conn, self.now, self.headroom, self.allow = conn, now, headroom, allow

    def poll(self, seats, active):
        for seat in seats:
            self.refresh(seat)

    def refresh(self, seat):
        h = self.headroom(seat['backend'])
        windows = (h or {}).get('windows') or {'seven_day': h or {}}
        for name, window in windows.items():
            remaining = window.get('remaining_pct', 90) if self.allow(seat['backend']) else 0
            usage.manual(self.conn, seat['name'], name, 100 - remaining, self.now() + 3600 * 100, now=self.now())
        return usage.headroom(self.conn, seat['name'], self.now())


def make_dispatcher(port, *, pipeline=None, launcher, resources=lambda **kw: GO, now=time.time,
                    usage_poller=None, decide=None, route=None, scan=None, wsl_up=None,
                    scan_every_s=None, worktree=None):
    if pipeline is None:
        pipeline = FakePipeline(port, None)
    if scan is not None:
        launcher.scan = scan
    if wsl_up is not None:
        launcher.wsl_up = wsl_up
    if worktree is not None:
        pipeline.worktree = worktree
    kw = {}
    if decide is not None:
        kw['decide'] = decide
    if route is not None:
        kw['route'] = route
    d = dispatcher.Dispatcher(port, pipeline, dispatcher.DispatchRuntime(launcher, resources, now), usage_poller, **kw)
    if scan_every_s is not None:
        d.scan_every_s = scan_every_s
    return d
