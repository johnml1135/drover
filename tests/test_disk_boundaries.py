"""The standalone tracker/cleanup boundary excludes ledger and platform policy."""
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
from unittest.mock import patch

from tests.disk_fixture import DiskCase


class Boundaries(DiskCase):
    def test_no_platform_forks_sql_or_legacy_disk_imports(self):
        from drover import disk, disk_cleanup, disk_side, disk_paths, disk_worktrees, disk_state, ownership, disk_schedule
        root = Path(__file__).resolve().parents[1]
        modules = (disk, disk_cleanup, disk_side, disk_paths, disk_worktrees, disk_state, ownership, disk_schedule)
        for module in modules:
            source = Path(module.__file__).read_text()
            tree = ast.parse(source)
            with self.subTest(module=module.__name__):
                for node in ast.walk(tree):
                    if isinstance(node, (ast.Import, ast.ImportFrom)):
                        names = [alias.name for alias in node.names]
                        owner = node.module if isinstance(node, ast.ImportFrom) else ''
                        self.assertFalse(any('garden' in name or 'sqlite3' in name or 'ledger' in name
                                             or name == 'hub' or name.startswith('hub.') for name in [owner or '', *names]))
                    if isinstance(node, ast.Attribute):
                        self.assertNotIn(ast.unparse(node), ('os.name', 'sys.platform', 'platform.system', 'placement.IS_WINDOWS'))
                    if isinstance(node, ast.Name):
                        self.assertNotEqual(node.id, 'IS_WINDOWS')
                    if isinstance(node, ast.Constant) and isinstance(node.value, str):
                        text = node.value.lower()
                        for forbidden in ('dotnet', 'nuget', 'motif', 'fieldworks', 'ollama', 'vssadmin', 'optimize-vhd'):
                            self.assertNotIn(forbidden, text)
                        self.assertFalse(text.lstrip().startswith(('select ', 'insert into ', 'delete from ', 'update ', 'create table ')))
        # Ordinary callers consume the deep module; legacy disk internals are deleted.
        from drover import resources
        for name in ('_free_gb', 'disk_path', 'read_disks', '_disk_label', 'VHDX_WARNING'):
            self.assertFalse(hasattr(resources, name), name)
        runtime = ast.parse((root / 'hub/runtime.py').read_text())
        for node in ast.walk(runtime):
            if isinstance(node, ast.ImportFrom):
                self.assertFalse((node.module or '').startswith(('drover', 'sqlite3')))
        from hub import runtime as bootstrap
        self.assertEqual(bootstrap.DISK_RESERVE, 256 * 1024 ** 2)

    def test_standalone_reader_opens_no_ledger_and_healthy_tick_is_cheap(self):
        from drover import disk
        # A fresh interpreter catches transitive imports hidden by the test process cache.
        program = textwrap.dedent('''
            import importlib.abc, json, sys
            class Guard(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname == 'sqlite3' or fullname == 'hub' or fullname.startswith(('sqlite3.', 'hub.', 'drover.ledger')):
                        raise AssertionError('standalone reader imported ' + fullname)
            sys.meta_path.insert(0, Guard())
            from drover import disk
            import subprocess
            def forbid_spawn(*args, **kwargs):
                raise AssertionError('healthy tracker spawned a process')
            subprocess.Popen = forbid_spawn
            subprocess.run = forbid_spawn
            cfg = json.loads(sys.argv[1])
            paths = []
            def reader(path):
                paths.append(path)
                return 180.0
            def enqueue(*args, **kwargs):
                raise AssertionError('healthy reader launched cleanup')
            rows = disk.read(cfg, reader=reader)
            out = disk.tick(cfg, now=2000000000., reader=reader, enqueue=enqueue)
            assert rows[0]['free_gb'] == 180.0, rows
            assert rows[0]['level'] == 'ok', rows
            assert disk.admission(rows)['verdict'] == 'go'
            assert len(paths) <= 2, paths
            assert all('vhdx' not in path.lower() for path in paths)
            print('healthy standalone read and tick')
        ''')
        env = {**os.environ, 'DROVER_HOME': str(self.state), 'DROVER_PATHS_STATE_DIR': str(self.state),
               'PYTHONPATH': str(Path(__file__).resolve().parents[1])}
        result = subprocess.run([sys.executable, '-c', program, json.dumps(self.cfg)],
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('healthy standalone read and tick', result.stdout)
        with patch.object(subprocess, 'run', side_effect=AssertionError('healthy tick performed process/WSL inventory')), \
             patch.object(subprocess, 'Popen', side_effect=AssertionError('healthy tick detached a side worker')):
            result = disk.tick(self.cfg, now=self.now, reader=lambda p: 180,
                               enqueue=lambda *a, **kw: self.fail('healthy cleanup queued'))
        self.assertFalse(result['pause'])
