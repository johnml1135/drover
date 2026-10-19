"""Hub YAML boundary and parser-free worker/client contracts."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest

from drover.tasktypes import canonical_hash, normalize_type


class TypeYamlTests(unittest.TestCase):
    def test_parser_free_import(self):
        script = textwrap.dedent('''\
            import importlib.abc
            import sys

            class NoYaml(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname == 'yaml' or fullname.startswith('yaml.'):
                        raise ModuleNotFoundError('yaml is unavailable', name=fullname)

            sys.meta_path.insert(0, NoYaml())
            import drover.tasktypes
            import drover.tasktypes.schema
            import drover.task_context
            import drover.port
            import drover.__main__
            import drover.tasktypes.codec
            assert 'yaml' not in sys.modules
            from drover.tasktypes.codec import decode_yaml
            try:
                decode_yaml('name: example')
            except RuntimeError as exc:
                message = str(exc)
                assert 'hub' in message.lower(), message
                assert 'PyYAML==6.0.3' in message, message
            else:
                raise AssertionError('missing parser must explain the hub prerequisite')
        ''')
        root = Path(__file__).resolve().parents[1]
        env = {**os.environ, 'PYTHONPATH': str(root), 'PYTHONDONTWRITEBYTECODE': '1'}
        result = subprocess.run([sys.executable, '-c', script], cwd=root, env=env,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_yaml_scalar_rules(self):
        from drover.tasktypes.codec import decode_yaml

        decoded = decode_yaml('values: [true, false, null, on, yes, off, no, 2026-10-19, '
                              'TRUE, False, Null, ~, 01, 0x10, 1_000, +1, .5, 1., '
                              '0, -12, 1.25, 1e3, -2.5E-2, "true", ".inf"]')
        self.assertEqual(decoded['values'], [True, False, None, 'on', 'yes', 'off', 'no',
                                            '2026-10-19', 'TRUE', 'False', 'Null', '~',
                                            '01', '0x10', '1_000', '+1', '.5', '1.',
                                            0, -12, 1.25, 1000.0, -0.025, 'true', '.inf'])
        self.assertIs(type(decoded['values'][0]), bool)
        self.assertIs(type(decoded['values'][1]), bool)
        self.assertIs(type(decoded['values'][21]), float)
        self.assertEqual(decode_yaml('on: yes\n2026-10-19: date'),
                         {'on': 'yes', '2026-10-19': 'date'})
        for key in ('true', 'false', 'null', '12', '1.5', '[key]', '{key: value}'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                decode_yaml(f'{key}: value')
        for number in ('.inf', '-.Inf', '+.INF', '.NaN', 'NaN', 'Infinity', '-Infinity',
                       '1e999', '-1.0e999'):
            with self.subTest(number=number), self.assertRaises(ValueError):
                decode_yaml(f'value: [{number}]')

    def test_yaml_limits(self):
        from drover.tasktypes.codec import decode_yaml

        invalid = {
            'duplicate': 'name: one\nname: two\n',
            'nested duplicate': 'fields: {x: {type: string, type: integer}}',
            'anchor': 'value: &saved [1, 2]',
            'alias expansion': 'a: &a [1, 2]\nb: &b [*a, *a]\nc: [*b, *b]',
            'undefined alias': 'value: *missing',
            'merge': 'value: {<<: {name: merged}}',
            'quoted merge': 'value: {"<<": {name: merged}}',
            'tag': 'value: !custom data',
            'standard tag': 'value: !!str 123',
            'non-specific tag': 'value: ! 123',
            'collection tag': 'value: !!map {a: b}',
            'object constructor': 'value: !!python/object/apply:os.system [false]',
            'multiple documents': 'name: one\n---\nname: two',
            'trailing document': 'name: one\n---',
            'empty': '',
            'sequence root': '[1, 2]',
            'scalar root': 'name',
            'malformed': 'value: [',
            'control character': 'value: \x00',
            'non-text': b'name: example',
            'depth': 'value: ' + '[' * 32 + '0' + ']' * 32,
            'extreme depth': 'value: ' + '[' * 2000 + '0' + ']' * 2000,
            'size': 'value: ' + 'x' * (256 * 1024),
            'UTF-8 size': 'value: ' + 'é' * (128 * 1024),
            'UTF-8 encoding': 'value: \ud800',
            'escaped surrogate': 'value: "\\uD800"',
            'field count': 'fields: {' + ', '.join(f'f{i}: {{}}' for i in range(1001)) + '}',
            'state count': 'states: {' + ', '.join(f's{i}: {{}}' for i in range(1001)) + '}',
            'combined definitions': 'fields: {' + ', '.join(f'f{i}: {{}}' for i in range(500))
                                    + '}\nstates: [' + ', '.join(f's{i}' for i in range(501)) + ']',
            'inline lifecycle': 'fields: {' + ', '.join(f'f{i}: {{}}' for i in range(500))
                                + '}\nlifecycle:\n  states: ['
                                + ', '.join(f's{i}' for i in range(501)) + ']',
        }
        for label, text in invalid.items():
            with self.subTest(label=label), self.assertRaises(ValueError):
                decode_yaml(text)

        leaf = decode_yaml('value: ' + '[' * 31 + '0' + ']' * 31)['value']
        for _ in range(31):
            self.assertIsInstance(leaf, list)
            leaf, = leaf
        self.assertEqual(leaf, 0)
        self.assertEqual(len(decode_yaml('value: ' + 'x' * (256 * 1024 - 7))['value']),
                         256 * 1024 - 7)
        allowed = 'fields: {' + ', '.join(f'f{i}: {{}}' for i in range(500)) + '}\nstates: ['
        allowed += ', '.join(f's{i}' for i in range(500)) + ']'
        self.assertEqual(len(decode_yaml(allowed)['states']), 500)

    def test_yaml_contract(self):
        from drover.tasktypes.codec import decode_yaml

        raw = decode_yaml('schema_version: 1\nname: example\nrevision: 1\n'
                          'fields:\n  enabled: {type: boolean, default: true}\n')
        self.assertEqual(raw['fields']['enabled'], {'type': 'boolean', 'default': True})
        equivalent = decode_yaml('{revision: 1, name: example, schema_version: 1, '
                                 'fields: {enabled: {default: true, type: boolean}}} # comment\n')
        self.assertEqual(canonical_hash(normalize_type(raw)),
                         canonical_hash(normalize_type(equivalent)))


if __name__ == '__main__':
    unittest.main()
