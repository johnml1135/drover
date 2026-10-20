"""Trackers are the sole resource configuration interface."""
from pathlib import Path
import tempfile
import unittest
from drover import config, resources
from tests.test_resources import wsl


class Resources(unittest.TestCase):
    def test_legacy_file_ignored(self):
        self.addCleanup(config.reset)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = config.validate({'paths': {'state_dir': str(root)}})
            config.use(cfg)
            before = resources.check(fake=wsl())
            self.assertEqual(before['verdict'], 'go')
            (root / 'resources.toml').write_text('[ram]\nhold_gb=10000\nqueue_base_gb=20000\n', encoding='utf-8')
            after = resources.check(fake=wsl())
            self.assertEqual(after, before, 'resources.toml must not participate in admission')

    def test_legacy_dict_rejected(self):
        with self.assertRaises(config.ConfigError):
            resources.check(fake=wsl(), config={'ram': {'hold_gb': 1}})
