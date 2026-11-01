"""Brief assembly: matching ADR files get their own section and drop first when the brief is too large."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from drover import brief
from tests.tiers import integration

LIMITS = {'context': {'file_limit_bytes': 6144, 'total_limit_bytes': 12288}}


def adr(n, body):
    return f'---\nid: ADR-{n:04d}\nstatus: accepted\napplies_to: ["drover/**"]\n---\n# Title {n}\n{body}\n'


@integration
class ContextSectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'docs' / 'adr').mkdir(parents=True)
        cfg = patch('drover.config.get', return_value=LIMITS)
        cfg.start()
        self.addCleanup(cfg.stop)
        hold = patch('drover.compaction_hold.active', return_value=None)   # the patched config has no state dir
        hold.start()
        self.addCleanup(hold.stop)

    def put(self, n, body):
        (self.root / 'docs' / 'adr' / f'{n:04d}-x.md').write_bytes(adr(n, body).encode())

    def task(self, **extra):
        return dict(id='T-a', title='Do', size='S', worktree=str(self.root), repo_brief='Repo rules.', **extra)

    def test_matching_files_appear_as_their_own_section(self):
        self.put(1, 'Decide once.')
        out = brief.assemble(self.task(), scope=['drover/x.py:1-9'], pinned='P')['user_prompt']
        self.assertIn('## Decisions that apply\n### docs/adr/0001-x.md', out)
        self.assertIn('Decide once.', out)
        self.assertLess(out.index('## Repo instructions'), out.index('## Decisions that apply'))
        self.assertNotIn('## Decisions that apply', brief.assemble(self.task(), scope=[], pinned='P')['user_prompt'])
        self.assertNotIn('## Decisions that apply', brief.assemble(self.task(), scope=['hub/a.py'], pinned='P')['user_prompt'])

    def test_context_files_drop_before_the_brief_is_refused(self):
        for n in (1, 2, 3):
            self.put(n, 'w ' * 1500)
        bare = brief.assemble(self.task(), scope=['hub/a.py'], pinned='P')
        full = brief.assemble(self.task(), scope=['drover/a.py'], pinned='P')
        self.assertGreater(full['tokens'], bare['tokens'])
        self.assertLessEqual(full['tokens'], full['budget'])
        # Squeeze the budget so only some files fit: the last ones go first, whole.
        with patch.dict(brief.BUDGET_TOKENS, {'S': full['tokens'] - 1500}):
            part = brief.assemble(self.task(), scope=['drover/a.py'], pinned='P')
        self.assertIn('0003-x.md', part['user_prompt'])
        self.assertNotIn('0001-x.md', part['user_prompt'])
        # With no room for any, the section vanishes and the brief is still built.
        with patch.dict(brief.BUDGET_TOKENS, {'S': bare['tokens'] + 5}):
            none = brief.assemble(self.task(), scope=['drover/a.py'], pinned='P')
        self.assertNotIn('Decisions that apply', none['user_prompt'])
        with patch.dict(brief.BUDGET_TOKENS, {'S': bare['tokens'] - 1}):
            with self.assertRaises(brief.BriefTooLarge):
                brief.assemble(self.task(), scope=['drover/a.py'], pinned='P')


if __name__ == '__main__':
    unittest.main()
