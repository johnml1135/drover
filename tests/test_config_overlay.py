"""Retired app overlay inputs do not affect machine configuration or children."""
import importlib.util
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from drover import config


class Overlay(unittest.TestCase):
    def test_obsolete_inputs_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / 'drover'
            package.mkdir()
            shutil.copyfile(config.__file__, package / 'config.py')
            (root / '.drover').mkdir()
            (root / '.drover/config.toml').write_text('[capacity]\nparallel_cap=63\n', encoding='utf-8')
            home = root / 'home'
            home.mkdir()
            (home / 'config.toml').write_text('[capacity]\nparallel_cap=7\n', encoding='utf-8')
            poison = root / 'selected.toml'
            poison.write_text('[capacity]\nparallel_cap=62\n', encoding='utf-8')
            spec = importlib.util.spec_from_file_location('drover._overlay_probe', package / 'config.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            for env in ({}, {'DROVER_OVERLAY': str(poison)}):
                with self.subTest(env=env):
                    self.assertEqual(module.load(home_path=home, env=env)['capacity']['parallel_cap'], 7)

    def test_child_exports_absent(self):
        # Reuse the retained-release fixture, without collecting its test methods here.
        from tests.test_hub_runtime import RuntimeTests, INSTANCE, setUpModule
        from hub.runtime import child_command
        setUpModule()
        fixture = RuntimeTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        release = fixture.baseline()
        argv, env, cwd = child_command(fixture.enrolled, release, 12, INSTANCE, env={
            'DROVER_OVERLAY': 'obsolete', 'drover_overlay': 'obsolete lowercase',
            'Drover_Reload_Overlay': 'obsolete mixed case', 'DROVER_RELOAD_OVERLAY': 'obsolete',
            'PYTHONPATH': 'foreign source', 'PythonHome': 'foreign interpreter', 'OWNER_SETTING': 'keep'})
        self.assertTrue({'DROVER_OVERLAY', 'DROVER_RELOAD_OVERLAY'}.isdisjoint(k.upper() for k in env), env)
        self.assertEqual(argv, [str(fixture.python), '-P', '-m', 'hub'])
        self.assertEqual(cwd, release['release_path'])
        self.assertEqual(env['PYTHONPATH'], cwd)
        self.assertNotIn('PythonHome', env)
        self.assertEqual(env['OWNER_SETTING'], 'keep')
        self.assertEqual(env['DROVER_HOME'], fixture.enrolled['paths']['config_home'])
        self.assertEqual(env['DROVER_REPO_CONFIGS_DIR'], fixture.enrolled['paths']['repo_config'])
        for name, value in fixture.enrolled['paths'].items():
            if name != 'overlay':
                self.assertEqual(env['DROVER_RELOAD_' + name.upper()], value)
