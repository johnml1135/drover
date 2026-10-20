"""The retired garden command and production imports disappear completely."""
import ast
import importlib.util
import unittest
from pathlib import Path


class Retirement(unittest.TestCase):
    def test_old_entry_point_is_absent(self):
        root = Path(__file__).resolve().parents[1]
        self.assertIsNone(importlib.util.find_spec('drover.garden'))
        self.assertFalse((root / 'drover' / 'garden.py').exists())
        imports = []
        for package in ('drover', 'hub'):
            for path in (root / package).rglob('*.py'):
                for node in ast.walk(ast.parse(path.read_text(encoding='utf-8-sig'))):
                    if isinstance(node, ast.Import):
                        imports.extend((str(path.relative_to(root)), alias.name) for alias in node.names
                                       if alias.name == 'drover.garden' or alias.name.startswith('drover.garden.'))
                    elif isinstance(node, ast.ImportFrom):
                        if node.module == 'drover.garden' or (node.module in (None, 'drover') and
                                                             any(a.name == 'garden' for a in node.names)):
                            imports.append((str(path.relative_to(root)), node.module))
        self.assertEqual(imports, [])
