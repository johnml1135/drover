"""Machine facts and transient owner answers through the repository loader."""
from drover import config, repos
from tests.test_repo_entry import RepoCase, ENTRY, SCRIPT


class Scopes(RepoCase):
    def test_adopted_machine_contract(self):
        self.adopt(run={'env': {'SAME': 'same', 'REPO_ONLY': 'repo'}})
        machine_file = (self.fix.root / 'private' / 'Mixed.toml').resolve()
        facts = {**self.machine, 'worktrees': str(self.fix.root / 'trees'),
                 'remote': 'upstream', 'default_branch': 'trunk', 'run': {'env': {'SAME': 'same', 'LOCAL': 'local'}}}
        machine_file.parent.mkdir()
        machine_file.write_text(config.dumps(facts), encoding='utf-8')
        loaded = self.call(repos.load, facts, machine_path=machine_file)
        self.assertEqual(loaded['remote'], 'upstream')
        self.assertEqual(loaded['default_branch'], 'trunk')
        self.assertEqual(loaded['run']['env'], {'SAME': 'same', 'REPO_ONLY': 'repo', 'LOCAL': 'local'})
        self.assertEqual(loaded['worktrees'], facts['worktrees'])
        self.assertEqual(loaded['_machine_path'], str(machine_file))
        all_repos = repos.load_all(machine_file.parent)
        self.assertEqual(all_repos['alpha']['_machine_path'], str(machine_file))
        forbidden = [({'scripts': {'test': 'scripts/bypass.ps1'}}, 'scripts'),
                     ({'brief': 'override'}, 'brief'), ({'schema_version': 1}, 'schema_version'),
                     ({'gate': {'docs_only': False}}, 'gate'), ({'review': {'model': 'x'}}, 'review'),
                     ({'landing': {'push': False}}, 'landing'), ({'land': {'policy': 'single'}}, 'land'),
                     ({'landing': {'remote': 'x'}}, 'landing.remote'),
                     ({'landing': {'main': 'x'}}, 'landing.main'),
                     ({'run': {'platforms': ['wsl']}}, 'run.platforms'),
                     ({'run': {'default': 'wsl'}}, 'run.default'),
                     ({'run': {'sandbox_writable_roots': []}}, 'run.sandbox_writable_roots'),
                     ({'run': {'unknown': True}}, 'run.unknown'), ({'unknown': 1}, 'unknown')]
        for policy, key in forbidden:
            with self.subTest(key=key):
                with self.assertRaises(repos.RepoError) as caught:
                    self.call(repos.load, {**facts, **policy}, machine_path=machine_file)
                self.assertIn(str(machine_file), str(caught.exception))
                self.assertIn(key, str(caught.exception))

    def test_transient_precedence_across_reloads(self):
        fallback = {**self.machine, 'scripts': {'test': 'scripts/fallback.ps1'}}
        prog = repos.load(fallback)
        answered = repos.with_test_script(prog, 'scripts/answer.ps1')
        self.assertEqual(answered['_machine'], fallback, 'owner answer must not mutate machine policy')
        for _ in range(2):
            answered = repos.load(answered)
            self.assertEqual(answered['scripts']['test'], 'scripts/answer.ps1')
        sha = self.adopt()
        # Reload the same resolved envelope against a newly adopted commit.
        clean = repos.with_test_script(repos.load(self.machine, self.fix.git('rev-parse', f'{sha}^')),
                                       'scripts/answer.ps1')
        for _ in range(2):
            clean = repos.load(clean, sha)
            self.assertEqual(clean['scripts']['test'], SCRIPT)
        no_script = self.adopt(scripts={})
        clean = repos.load(clean, no_script)
        self.assertEqual(clean['scripts']['test'], 'scripts/answer.ps1')
        self.assertFalse(clean['gate']['unknown'])
        old = repos.load(clean, self.fix.git('rev-parse', f'{sha}^'))
        self.assertEqual(old['scripts']['test'], 'scripts/answer.ps1')
        for bad in ('../escape.ps1', '/escape.ps1', 'bad.txt'):
            with self.subTest(path=bad), self.assertRaises(repos.RepoError):
                repos.with_test_script(old, bad)

    def test_serialized_metadata_rejected(self):
        path = self.fix.root / 'machine.toml'
        for adopted in (False, True):
            if adopted:
                self.adopt()
            for key, value in (('_machine', {}), ('_machine_path', 'elsewhere'),
                               ('_adopted', True), ('_test_script', 'scripts/answer.ps1')):
                raw = {**self.machine, key: value}
                path.write_text(config.dumps(raw), encoding='utf-8')
                with self.subTest(adopted=adopted, key=key, api='read_machine'):
                    with self.assertRaises(repos.RepoError) as caught:
                        repos.read_machine(path)
                    self.assertIn(key, str(caught.exception))
                    self.assertIn(str(path), str(caught.exception))
                with self.subTest(adopted=adopted, key=key, api='load'):
                    with self.assertRaises(repos.RepoError) as caught:
                        repos.load(raw)
                    self.assertIn(key, str(caught.exception))
