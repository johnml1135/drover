"""Setup honors repository adoption and preflights before changing state."""
import io
from pathlib import Path
import tempfile
import unittest
from drover import config, repos, wizard
from tests.gitfix import Repo
from tests.test_repo_entry import ENTRY, entry
from tests.test_wizard import fake


class WizardScopes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.fix = Repo()
        self.addCleanup(self.fix.cleanup)
        self.addCleanup(config.reset)
        config.use(config.validate({}))

    def save(self, raw):
        path = self.state / 'repos' / (raw['name'] + '.toml')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(config.dumps(raw), encoding='utf-8')
        return path

    def setup(self, names=('alpha',), *, interactive=False, **answers):
        out = io.StringIO()
        found = [{'name': name, 'path': str(self.fix.repo)} for name in names]
        chosen = {'repos_pick': 'all'} if interactive else {'repos': [{'name': n} for n in names]}
        code = wizard.run(fake(repos=lambda root: found), out=out, defaults=True,
                          state_dir=str(self.state), now=lambda: 1_800_000_000,
                          answers={**chosen, 'state_git': False, **answers})
        return code, out.getvalue()

    def snapshot(self, directory):
        return {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob('*') if p.is_file()}

    def test_adopted_paths(self):
        self.fix.commit(ENTRY, entry(run={'env': {'REPO_ONLY': 'repo'}}, landing={'push': True}))
        before = self.fix.git('status', '--porcelain')
        checkout_bytes = (self.fix.repo / ENTRY).read_bytes()
        for interactive in (False, True):
            with self.subTest(interactive=interactive):
                path = self.save({'name': 'alpha', 'repo': str(self.fix.repo),
                                  'worktrees': str(self.root / 'saved-trees'),
                                  'remote': 'upstream', 'default_branch': 'trunk',
                                  'run': {'env': {'MACHINE_ONLY': 'local'}}})
                for _ in range(2):
                    code, output = self.setup(interactive=interactive)
                    self.assertEqual(code, 0)
                    saved = repos.read_machine(path)
                    self.assertEqual(set(saved), {'name', 'repo', 'worktrees', 'remote', 'default_branch', 'run'})
                    self.assertEqual(saved['worktrees'], str(self.root / 'saved-trees'))
                    self.assertEqual(saved['run'], {'env': {'MACHINE_ONLY': 'local'}})
                    self.assertEqual((saved['remote'], saved['default_branch']), ('upstream', 'trunk'))
                    self.assertTrue(repos.load(saved)['landing']['push'])
                    self.assertNotIn('push landed work', output)
                    self.assertIn('.drover/config.toml', output)
                    self.assertNotIn('versioned drover.toml', output)
                    self.assertNotIn('live in drover.toml', output)
                snapshot = self.snapshot(self.state)
                explicit = {'repo_alpha_push': False} if interactive else {'repos': [{'name': 'alpha', 'push': False}]}
                with self.assertRaises((wizard.WizardError, repos.RepoError)):
                    self.setup(interactive=interactive, **explicit)
                self.assertEqual(self.snapshot(self.state), snapshot)
        # No run table should be manufactured from repository-only env.
        path = self.save({'name': 'alpha', 'repo': str(self.fix.repo)})
        self.setup()
        self.assertNotIn('run', repos.read_machine(path))
        self.assertEqual(self.fix.git('status', '--porcelain'), before)
        self.assertEqual((self.fix.repo / ENTRY).read_bytes(), checkout_bytes)

    def test_fallback_policy_survives_twice(self):
        machine = {'name': 'alpha', 'repo': str(self.root / 'unreachable'),
                   'brief': 'Keep the repository rules.',
                   'review': {'backend': 'codex', 'model': 'gpt-6.1-sol', 'effort': 'high',
                              'high_risk_model': 'gpt-6.1-sol'},
                   'land': {'policy': 'single'}, 'landing': {'mode': 'local', 'push': True},
                   'scripts': {'test': 'scripts/check.ps1'},
                   'run': {'platforms': ['wsl'], 'default': 'wsl', 'env': {'MODE': 'all'}},
                   'gate': {'platform': 'wsl', 'docs_only': False, 'timeout_s': 123, 'idle_s': 45}}
        before = repos.load(machine)
        path = self.save(machine)
        for interactive in (False, True):
            for _ in range(2):
                self.assertEqual(self.setup(interactive=interactive)[0], 0)
                saved = repos.read_machine(path)
                effective = repos.load(saved)
                for key in ('brief', 'review', 'scripts', 'run', 'gate'):
                    with self.subTest(interactive=interactive, key=key):
                        self.assertEqual(saved.get(key), machine[key])
                        self.assertEqual(effective[key], before[key])
                self.assertEqual(saved.get('land', {}).get('policy'), 'single')
                self.assertEqual(saved.get('landing', {}).get('mode'), 'local')
                self.assertEqual(effective['landing'], before['landing'])

    def test_all_selected_preflight(self):
        self.fix.commit(ENTRY, entry())
        for interactive in (False, True):
            for existing_state in (False, True):
                with self.subTest(interactive=interactive, existing_state=existing_state):
                    self.state = self.root / f'state-{interactive}-{existing_state}'
                    self.save({'name': 'alpha', 'repo': str(self.fix.repo)})
                    invalid = self.save({'name': 'beta', 'repo': str(self.fix.repo), 'brief': 'illegal machine policy'})
                    if existing_state:
                        (self.state / 'config.toml').write_text('[capacity]\nparallel_cap=5\n', encoding='utf-8')
                        (self.state / 'config.toml.bak-old').write_text('keep backup', encoding='utf-8')
                    snapshot = self.snapshot(self.state)
                    dirs = {str(p.relative_to(self.state)) for p in self.state.rglob('*') if p.is_dir()}
                    with self.assertRaises((wizard.WizardError, repos.RepoError)) as caught:
                        self.setup(('alpha', 'beta'), interactive=interactive)
                    self.assertIn(str(invalid.resolve()), str(caught.exception))
                    self.assertIn('brief', str(caught.exception))
                    self.assertEqual(self.snapshot(self.state), snapshot)
                    self.assertEqual({str(p.relative_to(self.state)) for p in self.state.rglob('*') if p.is_dir()}, dirs)

    def test_fallback_names_and_push(self):
        path = self.save({'name': 'alpha', 'repo': str(self.fix.repo),
                          'remote': 'upstream', 'default_branch': 'trunk', 'landing': {'push': True}})
        for _ in range(2):
            self.assertEqual(self.setup()[0], 0)
            saved = repos.read_machine(path)
            effective = repos.load(saved)
            self.assertEqual((saved['remote'], saved['default_branch']), ('upstream', 'trunk'))
            self.assertIs(saved['landing']['push'], True)
            self.assertEqual(repos.landing_config(effective)['push'], True)
        self.assertEqual(self.setup(repos=[{'name': 'alpha', 'push': False}])[0], 0)
        saved = repos.read_machine(path)
        self.assertEqual((saved['remote'], saved['default_branch']), ('upstream', 'trunk'))
        self.assertIs(saved['landing']['push'], False)
        self.assertIs(repos.landing_config(repos.load(saved))['push'], False)
