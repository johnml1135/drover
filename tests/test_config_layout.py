"""The shipped repository adopts the same entry layout as every other repo."""
import hashlib
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
        self.assertEqual(hashlib.sha256(script).hexdigest(),
                         '223b34a10daa2ee6156c2353658bd47a14bc975c3d79728941afcaf53c11d0a2')
        text = (root / ENTRY).read_text(encoding='utf-8')
        raw = tomllib.loads(text)
        self.assertEqual(raw['schema_version'], 1)
        self.assertEqual(raw['scripts']['test'], script_path)
        for path in (ENTRY, script_path, '.drover/workflows/future.yaml'):
            result = sh('git', 'check-ignore', '--no-index', path, cwd=root, check=False)
            self.assertEqual(result.returncode, 1, f'{path} must be trackable: {result.stdout} {result.stderr}')
        self.fix.commit(script_path, script.decode('utf-8'))
        sha = self.fix.commit(ENTRY, text)
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
