"""Fresh agents load only short, accepted ADRs whose applies_to matches their scope."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from drover import config_defaults, context_files
from tests.tiers import integration

REPO = Path(__file__).resolve().parent.parent


def adr(n, globs='["drover/**"]', status='accepted', body='Decision.\n'):
    return f'---\nschema_version: 1\nid: ADR-{n:04d}\nstatus: {status}\napplies_to: {globs}\n---\n# Title\n{body}'


@integration
class SelectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.dir = self.root / 'docs' / 'adr'
        self.dir.mkdir(parents=True)

    def put(self, name, text):
        (self.dir / name).write_bytes(text.encode('utf-8'))

    def select(self, scope=('drover/x.py',), file_limit=6144, total_limit=12288):
        return context_files.select(str(self.root), list(scope), file_limit=file_limit, total_limit=total_limit)

    def paths(self, result):
        return [f['path'].rsplit('/', 1)[-1] for f in result['files']]

    def test_empty_scope_loads_nothing(self):
        self.put('0001-a.md', adr(1))
        self.assertEqual(self.select(scope=[]), {'files': [], 'skipped': []})

    def test_only_accepted_adrs_with_a_matching_glob_load(self):
        self.put('0001-a.md', adr(1))
        self.put('0002-b.md', adr(2, status='proposed'))
        self.put('0003-c.md', adr(3, globs='["hub/**"]'))
        self.put('0004-d.md', adr(4, globs='[]'))
        self.assertEqual(self.paths(self.select()), ['0001-a.md'])

    def test_line_ranges_in_scope_are_ignored(self):
        self.put('0001-a.md', adr(1))
        self.assertEqual(self.paths(self.select(scope=['drover/x.py:10-20'])), ['0001-a.md'])

    def test_malformed_front_matter_is_skipped_not_fatal(self):
        self.put('0001-a.md', adr(1))
        self.put('0002-b.md', adr(2, globs='[drover/**]'))
        self.put('0003-c.md', 'no front matter at all\n')
        self.put('0004-d.md', '---\nstatus: accepted\napplies_to: ["x"]\n')
        result = self.select()
        self.assertEqual(self.paths(result), ['0001-a.md'])
        self.assertEqual({s['path'][-9:] for s in result['skipped']}, {'0002-b.md', '0003-c.md', '0004-d.md'})
        with self.assertRaises(ValueError):
            context_files.applies_to('---\napplies_to: nope\n---\n')

    def test_a_file_over_the_limit_is_skipped_whole(self):
        self.put('0001-a.md', adr(1, body='x' * 500))
        self.put('0002-b.md', adr(2))
        result = self.select(file_limit=300)
        self.assertEqual(self.paths(result), ['0002-b.md'])
        self.assertIn('file limit', result['skipped'][0]['reason'])

    def test_total_limit_drops_whole_files_in_a_stable_order(self):
        for n in (1, 2, 3):
            self.put(f'000{n}-a.md', adr(n, body='y' * 100))
        size = len(adr(1, body='y' * 100).encode())
        first = self.select(total_limit=size * 2 + 5)
        self.assertEqual(self.paths(first), ['0003-a.md', '0002-a.md'])
        self.assertEqual([s['path'][-9:] for s in first['skipped']], ['0001-a.md'])
        self.assertEqual(first, self.select(total_limit=size * 2 + 5))
        for f in first['files']:
            self.assertEqual(f['text'].encode('utf-8'), (self.root / f['path']).read_bytes())

    def test_narrower_glob_wins_over_a_broad_one(self):
        self.put('0009-broad.md', adr(9, globs='["**"]'))
        self.put('0002-mid.md', adr(2, globs='["drover/**"]'))
        self.put('0001-narrow.md', adr(1, globs='["drover/x.py"]'))
        self.assertEqual(self.paths(self.select()), ['0001-narrow.md', '0002-mid.md', '0009-broad.md'])

    def test_newest_adr_wins_a_tie(self):
        self.put('0003-a.md', adr(3))
        self.put('0010-b.md', adr(10))
        self.put('0007-c.md', adr(7))
        self.assertEqual(self.paths(self.select()), ['0010-b.md', '0007-c.md', '0003-a.md'])

    def test_no_glossary_readme_or_design_doc_is_ever_loaded(self):
        self.put('0001-a.md', adr(1))
        self.put('README.md', adr(2))
        self.put('template.md', adr(3))
        (self.root / 'GLOSSARY.md').write_text(adr(4, globs='["**"]'), encoding='utf-8')
        (self.root / 'README.md').write_text(adr(5, globs='["**"]'), encoding='utf-8')
        (self.root / 'docs' / 'design.md').write_text(adr(6, globs='["**"]'), encoding='utf-8')
        self.assertEqual(self.paths(self.select()), ['0001-a.md'])

    def test_a_missing_adr_folder_loads_nothing(self):
        (self.root / 'docs' / 'adr').rmdir()
        self.assertEqual(self.select(), {'files': [], 'skipped': []})

    def test_a_read_failure_never_stops_the_brief(self):
        self.put('0001-a.md', adr(1))
        self.put('0002-b.md', adr(2))
        real = Path.read_bytes

        def flaky(path):
            if path.name.startswith('0001'):
                raise PermissionError('locked')
            return real(path)
        with patch.object(Path, 'read_bytes', flaky):
            result = self.select()
        self.assertEqual(self.paths(result), ['0002-b.md'])
        self.assertIn('unreadable', result['skipped'][0]['reason'])
        with patch('drover.placement.native', side_effect=RuntimeError('boom')):
            self.assertEqual(self.select()['files'], [])

    def test_a_compaction_hold_reads_nothing(self):
        self.put('0001-a.md', adr(1))
        with patch('drover.compaction_hold.active', return_value={'phase': 'draining'}):
            result = self.select()
        self.assertEqual(result['files'], [])
        self.assertIn('compaction', result['skipped'][0]['reason'])

    def test_a_wsl_worktree_reads_committed_files_through_git(self):
        calls = []

        def git(root, *args, **kw):
            calls.append(args)
            if args[0] == 'ls-tree':
                return subprocess.CompletedProcess(args, 0, 'docs/adr/0001-a.md\ndocs/adr/README.md\n', '')
            return subprocess.CompletedProcess(args, 0, adr(1), '')
        with patch('drover.placement.routes_through_wsl', return_value=True), patch('drover.placement.git', git):
            result = context_files.select('/home/s/w', ['drover/x.py'], file_limit=6144, total_limit=12288)
        self.assertEqual(self.paths(result), ['0001-a.md'])
        self.assertEqual([c[0] for c in calls], ['ls-tree', 'show'])
        self.assertEqual(calls[1][-1], 'HEAD:docs/adr/0001-a.md')

    def test_this_repo_stays_under_the_limit_for_a_drover_scope(self):
        limits = config_defaults.DEFAULTS['context']
        result = context_files.select(str(REPO), ['drover/brief.py'], file_limit=limits['file_limit_bytes'],
                                      total_limit=limits['total_limit_bytes'])
        self.assertTrue(result['files'])
        self.assertLessEqual(sum(len(f['text'].encode()) for f in result['files']), limits['total_limit_bytes'])
        self.assertFalse([s for s in result['skipped'] if 'file limit' in s['reason']])


if __name__ == '__main__':
    unittest.main()
