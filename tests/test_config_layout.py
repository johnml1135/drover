"""The shipped repository adopts the same entry layout as every other repo."""
from pathlib import Path
import tomllib
from drover import repos
from tests.gitfix import sh
from tests.test_repo_entry import RepoCase, ENTRY


class Layout(RepoCase):
    def test_shipped_layout(self):
        root = Path(__file__).resolve().parents[1]
        script_path = '.drover/scripts/test.ps1'
        self.assertTrue((root / ENTRY).is_file(), 'Drover must ship .drover/config.toml')
        self.assertTrue((root / script_path).is_file())
        self.assertFalse((root / 'drover.toml').exists())
        self.assertFalse((root / 'scripts/drover/test.ps1').exists())
        script = (root / script_path).read_bytes()
        text = (root / ENTRY).read_text(encoding='utf-8')
        raw = tomllib.loads(text)
        self.assertEqual(raw['schema_version'], 1)
        self.assertEqual(raw['scripts']['test'], script_path)
        self.fix.commit(".gitignore", (root / ".gitignore").read_text(encoding="utf-8"))
        self.fix.commit(script_path, script.decode("utf-8"))
        self.fix.commit(ENTRY, text)
        for path in (ENTRY, script_path, '.drover/workflows/future.yaml'):
            result = sh('git', 'check-ignore', '--no-index', path, cwd=self.fix.repo, check=False)
            self.assertEqual(result.returncode, 1, f'{path} must be trackable: {result.stdout} {result.stderr}')
        sha = self.fix.git('rev-parse', 'HEAD')
        prog = repos.load(self.machine, sha)
        self.assertEqual(prog['scripts']['test'], script_path)
        argv = repos.script_argv(prog)
        self.assertEqual(argv[1:], ['-NoLogo', '-NoProfile', '-NonInteractive', '-File', script_path])
        self.assertTrue(argv[0])
        guide = root / 'docs/configuration.md'
        self.assertTrue(guide.is_file(), 'The two configuration scopes need shipped documentation')
        docs = guide.read_text(encoding='utf-8')
        for term in ('.drover/config.toml', 'schema_version', '.drover/scripts/', 'config.toml',
                     'repos/', 'config explain', 'resources.toml', '.drover/workflows/*.yaml', 'YAML'):
            self.assertIn(term, docs)
        self.assertRegex(docs.lower(), r'(?s)workflow.{0,300}(not (?:yet )?implemented|not supported|not executed)')
        for filename in ('README.md', 'drover.example.toml', 'docs/design.md', 'docs/dispatcher.md'):
            self.assertIn('.drover/config.toml', (root / filename).read_text(encoding='utf-8'), filename)

    def test_malformed_platforms_name_the_entry_commit_and_key(self):
        for value in ({'foo': 'wsl'}, True, 'wsl', 1):
            sha = self.adopt(run={'platforms': value})
            with self.subTest(value=value):
                try:
                    repos.load(self.machine, sha)
                except repos.RepoError as error:
                    for detail in (ENTRY, sha, 'run.platforms'):
                        self.assertIn(detail, str(error))
                except (KeyError, TypeError) as error:
                    self.fail(f'malformed run.platforms escaped RepoError: {error}')
                else:
                    self.fail('malformed run.platforms must be rejected')

    def test_configuration_reference_covers_keys_defaults_and_precedence(self):
        docs = (Path(__file__).resolve().parents[1] / 'docs/configuration.md').read_text(encoding='utf-8')
        for term in ('run.platforms', 'run.default', 'run.env', 'run.sandbox_writable_roots',
                     'gate.platform', 'gate.timeout_s', 'gate.idle_s', 'gate.docs_only', 'gate.docs_only_globs',
                     'review.backend', 'review.seat', 'review.model', 'review.effort', 'review.high_risk_model',
                     'landing.mode', 'landing.push', 'land.policy', 'land.push', '3600', '1200',
                     'committed test script > transient owner answer > machine fallback script'):
            with self.subTest(term=term):
                self.assertIn(term, docs)
