"""Private repo discovery, compatibility and explicit examples."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from drover import config, repos


class Discovery(unittest.TestCase):
    def test_private_configs_take_precedence_and_examples_require_opt_in(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {'DROVER_EXAMPLES': '0'}):
            state = Path(d)
            with patch.object(config, 'repos_dir', return_value=state / 'repos'):
                self.assertEqual(repos.load_all(), {})
                with patch.dict(os.environ, {'DROVER_EXAMPLES': '1'}):
                    self.assertEqual(set(repos.load_all()), {'acme-lib', 'acme-app'})
                legacy = state / 'programs'
                legacy.mkdir()
                text = 'name="mine"\nrepo="~/repos/mine"\n'
                (legacy / 'mine.toml').write_text(text)
                with self.assertLogs('drover.repos', 'WARNING') as logs:
                    self.assertEqual(set(repos.load_all()), {'mine'})
                self.assertIn('deprecated', logs.output[0])
                self.assertFalse((state / 'repos').exists())
                (state / 'repos').mkdir()
                self.assertEqual(repos.load_all(), {})
