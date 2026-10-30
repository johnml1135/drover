"""Shrink-only architecture debt at the B-1 entry pin; no product modules are imported."""
import ast
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import json
import os
from pathlib import Path
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
ALLOW = ROOT / 'tests/architecture_import_allowlist.json'
REMOVED = ROOT / 'tests/architecture_removed_symbols.json'
OLD_MODULES = tuple('drover.ledger.' + name for name in (
    'ops', 'ops_common', 'ops_discussion', 'ops_execution', 'ops_outside', 'ops_stages', 'ops_tasks',
    'scope', 'states', 'waiting', 'waiting_common', 'waiting_core', 'waiting_events', 'waiting_replan',
    'waiting_replan_runs')) + ('drover.notifications.controls',)


def old_module(symbol):
    return any(symbol == name or symbol.startswith(name + '.') for name in OLD_MODULES)


def manifest(path):
    return json.loads(path.read_text(encoding='utf-8'))


@lru_cache(maxsize=1)
def trees():
    paths = []
    for top in ('drover', 'hub', 'tests'):
        for folder, dirs, files in os.walk(ROOT / top):
            dirs[:] = [d for d in dirs if not d.startswith('.') and d != '__pycache__'
                       and not (Path(folder, d) / 'pyvenv.cfg').exists()]
            paths.extend(Path(folder, name) for name in files if name.endswith('.py'))
    with ThreadPoolExecutor(max_workers=8) as pool:
        texts = list(pool.map(lambda path: path.read_text(encoding='utf-8'), paths))
    # Only old Drover modules can be debt in tests; skip files that cannot name or load one.
    markers = ('ledger', 'notifications', '__import__', 'import_module', 'getattr')
    return {path.relative_to(ROOT).as_posix(): ast.parse(text, filename=str(path))
            for path, text in zip(paths, texts)
            if path.parts[-2] != 'tests' or any(marker in text for marker in markers)}


def module(path):
    parts = path.removesuffix('.py').split('/')
    return '.'.join(parts[:-1] if parts[-1] == '__init__' else parts)


@lru_cache(maxsize=1)
def nodes():
    return {path: list(ast.walk(tree)) for path, tree in trees().items()}


@lru_cache(maxsize=None)
def imports(path, tree):
    result = []
    package = module(path).split('.')
    if not path.endswith('/__init__.py'):
        package.pop()
    for node in nodes()[path]:
        if isinstance(node, ast.Import):
            for alias in node.names:
                result.append((alias.name, alias.asname or alias.name.split('.')[0]))
        elif isinstance(node, ast.ImportFrom):
            base = (package[:len(package) - node.level + 1] if node.level else [])
            if node.module:
                base += node.module.split('.')
            for alias in node.names:
                result.append(('.'.join(base + [alias.name]), alias.asname or alias.name))
    return result


@lru_cache(maxsize=None)
def alias_map(path, tree):
    result = {bound: symbol for symbol, bound in imports(path, tree)}
    for node in nodes()[path]:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not alias.asname:
                    root = alias.name.split('.')[0]
                    result[root] = root
    return result


def is_debt(path, symbol):
    # ledger is storage-only; its CLI is an upper-layer entry point.
    ledger = path.startswith('drover/ledger/') and path != 'drover/ledger/cli.py'
    project = symbol in ('hub', 'drover') or symbol.startswith(('hub.', 'drover.'))
    if ledger and project and not (symbol == 'drover.ledger' or symbol.startswith('drover.ledger.')):
        return True
    # B-2b removes these old modules and rewires every binding.
    if old_module(symbol):
        return True
    # Rules also apply as the new contract modules are introduced.
    mod = module(path)
    if mod.startswith('drover.lifecycle'):
        return symbol == 'hub' or symbol.startswith(('hub.', 'drover.dispatcher', 'drover.notifications', 'drover.stages_view'))
    if mod.startswith('drover.recovery'):
        return symbol.startswith(('drover.lifecycle.transitions', 'drover.lifecycle._write', 'hub.', 'drover.dispatcher'))
    if mod == 'drover.outcomes':
        return project and not symbol.startswith('drover.ledger.codec')
    if mod == 'drover.workspace':
        return (symbol.startswith(('hub.', 'drover.dispatcher')) or
                (symbol.startswith('drover.lifecycle.') and not symbol.startswith('drover.lifecycle.types')))
    if mod == 'drover.notifications.subscriber':
        return symbol.startswith(('drover.lifecycle._write', 'drover.lifecycle.transitions.advance'))
    if mod in ('drover.stages', 'drover.stages_config'):
        return project and not symbol.startswith(('drover.outcomes', 'drover.lifecycle.types'))
    return False


def debt():
    result = Counter()
    for path, tree in trees().items():
        bindings = list(imports(path, tree))
        result.update((path, symbol) for symbol, _ in bindings if is_debt(path, symbol))
        aliases = alias_map(path, tree)
        old_aliases = {bound: name for bound, name in aliases.items() if old_module(name)}
        roots = {bound for bound, name in aliases.items() if old_module(name) or
                 any(old.startswith(name + '.') for old in OLD_MODULES)}
        for node in nodes()[path]:
            if isinstance(node, ast.Attribute):
                root = node.value
                while isinstance(root, ast.Attribute):
                    root = root.value
                if isinstance(root, ast.Name) and root.id in roots:
                    name = call_name(node, aliases)
                    if old_module(name) and any(name.startswith(base + '.') for base in OLD_MODULES):
                        result[(path, name)] += 1
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in old_aliases:
                result[(path, old_aliases[node.id])] += 1
            elif isinstance(node, ast.Call) and call_name(node.func, aliases) in (
                    '__import__', 'builtins.__import__', 'importlib.import_module'):
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    name = node.args[0].value
                    if is_debt(path, name):
                        result[(path, name)] += 1

    return result


def owner_for(path, symbol):
    if old_module(symbol):
        return 'B-2b'
    if '/waiting_migration' in path:
        return 'B-4a'
    if path.endswith('/db.py'):
        return 'B-4b'
    if path.endswith('/actions.py'):
        return 'B-5'
    if path.endswith('/landing.py'):
        return 'B-6a1'
    return 'B-2b'


def call_name(node, aliases):
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        return call_name(node.value, aliases) + '.' + node.attr
    return ast.unparse(node)


def dynamic():
    result = Counter()
    for path, tree in trees().items():
        if path.startswith('tests/'):
            continue
        aliases = alias_map(path, tree)
        for node in nodes()[path]:
            if not isinstance(node, ast.Call):
                continue
            name = call_name(node.func, aliases)
            if name in ('__import__', 'importlib.import_module', 'getattr', 'builtins.__import__', 'builtins.getattr'):
                args = [ast.unparse(arg) for arg in node.args[:2 if name.endswith('getattr') else 1]]
                result[(path, name, ', '.join(args))] += 1
    return result


def mixins():
    result = Counter()
    for path, tree in trees().items():
        if path.startswith('tests/'):
            continue
        for node in nodes()[path]:
            if isinstance(node, ast.ClassDef):
                if node.name.endswith(('Mixin', 'Steps')):
                    result[(path, node.name, 'definition')] += 1
                for base in node.bases:
                    name = ast.unparse(base)
                    if len(node.bases) > 1 or name.endswith(('Mixin', 'Steps')):
                        result[(path, node.name, name)] += 1
    return result


def ports():
    result = Counter()
    for path, tree in trees().items():
        if path.startswith('tests/'):
            continue
        for node in nodes()[path]:
            if isinstance(node, ast.ClassDef) and node.name in ('LedgerPort', 'DirectPort', 'HttpPort', 'HubPort'):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        result[(path, node.name, item.name)] += 1
    return result


def compare(test, actual, expected, keys, label):
    pairs = [tuple(row[k] for k in keys) for row in expected]
    test.assertEqual(len(pairs), len(set(pairs)), f'{label}: duplicate keys')
    pinned = Counter({key: row['count'] for key, row in zip(pairs, expected)})
    test.assertEqual(actual, pinned, f'{label}: new/increased entries or stale pins; lower/remove debt when it shrinks')


class Architecture(unittest.TestCase):
    def test_import_direction(self):
        data = manifest(ALLOW)
        entries = data['imports']
        self.assertTrue(all(row['owner_slice'] and type(row['count']) is int and row['count'] > 0 for row in entries))
        actual = Counter({(path, symbol, owner_for(path, symbol)): count
                          for (path, symbol), count in debt().items()})
        compare(self, actual, entries, ('importer', 'imported_symbol', 'owner_slice'), 'import debt')

    def test_dynamic_entrypoint_inventory(self):
        data = manifest(ALLOW)
        compare(self, dynamic(), data['dynamic_entrypoints'], ('importer', 'operation', 'arguments'), 'dynamic entry points')
        compare(self, ports(), data['ports'], ('file', 'class', 'method'), 'port implementations')

    def test_removed_names(self):
        data = manifest(REMOVED)
        violations = []
        forbidden = set(data['global_names'])
        for path, tree in trees().items():
            if path.startswith('tests/'):
                continue
            aliases = alias_map(path, tree)
            for symbol, bound in imports(path, tree):
                if symbol in data['qualified_names']:
                    violations.append(f'{path} imports removed {symbol}')
                if any(item['file'] == path and item['binding'] == bound for item in data['scoped_bindings']):
                    violations.append(f'{path} imports removed binding {bound}')
            for node in nodes()[path]:
                name = None
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    name = node.name
                elif isinstance(node, ast.Name):
                    name = node.id
                elif isinstance(node, ast.Attribute):
                    name = node.attr
                elif isinstance(node, ast.alias):
                    name = node.name.split('.')[-1]
                elif isinstance(node, ast.Call) and call_name(node.func, aliases).endswith('getattr') and len(node.args) > 1:
                    if isinstance(node.args[1], ast.Constant):
                        name = node.args[1].value
                        if name in data['removed_receipt_fields']:
                            violations.append(f'{path}:{node.lineno} removed receipt field {name}')
                qualified = call_name(node, aliases) if isinstance(node, ast.Attribute) else ''
                if qualified in data['qualified_names']:
                    violations.append(f'{path}:{node.lineno} {qualified}')
                if isinstance(node, ast.Call) and call_name(node.func, aliases).endswith('getattr') and len(node.args) > 1:
                    attr = node.args[1]
                    if isinstance(attr, ast.Constant) and isinstance(attr.value, str):
                        target = call_name(node.args[0], aliases) + '.' + attr.value
                        if target in data['qualified_names']:
                            violations.append(f'{path}:{node.lineno} {target}')
                if isinstance(node, ast.Name) and any(item['file'] == path and item['binding'] == node.id
                                                      for item in data['scoped_bindings']):
                    violations.append(f'{path}:{node.lineno} removed binding {node.id}')
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                        item['file'] == path and item['name'] == node.name for item in data['scoped_definitions']):
                    violations.append(f'{path}:{node.lineno} removed definition {node.name}')
                if name in forbidden:
                    violations.append(f'{path}:{getattr(node, "lineno", 0)} {name}')
                if isinstance(node, ast.ClassDef):
                    for item in node.body:
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            qualified = f'{module(path)}.{node.name}.{item.name}'
                            if qualified in data['qualified_names']:
                                violations.append(f'{path}:{item.lineno} {qualified}')
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    qualified = f'{module(path)}.{node.name}'
                    if qualified in data['qualified_names'] and node in tree.body:
                        violations.append(f'{path}:{node.lineno} {qualified}')
                if isinstance(node, ast.Attribute) and node.attr in data['removed_receipt_fields']:
                    violations.append(f'{path}:{node.lineno} removed receipt field {node.attr}')
                if isinstance(node, ast.Attribute) and any(item['file'] == path and item['attribute'] == node.attr
                                                          for item in data['scoped_attributes']):
                    violations.append(f'{path}:{node.lineno} removed attribute {node.attr}')
                if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
                    if node.slice.value in data['removed_receipt_fields']:
                        violations.append(f'{path}:{node.lineno} removed receipt field {node.slice.value}')
                if isinstance(node, ast.Dict):
                    for key, value in zip(node.keys, node.values):
                        if isinstance(key, ast.Constant) and key.value in data['removed_receipt_fields']:
                            violations.append(f'{path}:{node.lineno} removed receipt field {key.value}')
                        if (isinstance(key, ast.Constant) and key.value == 'status' and isinstance(value, ast.Constant)
                                and value.value in data['removed_receipt_fields']):
                            violations.append(f'{path}:{node.lineno} removed infra status')
                if isinstance(node, ast.keyword):
                    if node.arg in data['removed_receipt_fields']:
                        violations.append(f'{path}:{node.value.lineno} removed receipt field {node.arg}')
                    if node.arg == 'status' and isinstance(node.value, ast.Constant) and node.value.value in data['removed_receipt_fields']:
                        violations.append(f'{path}:{node.value.lineno} removed infra status')
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'get':
                    if node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value in data['removed_receipt_fields']:
                        violations.append(f'{path}:{node.lineno} removed receipt field {node.args[0].value}')
                if isinstance(node, ast.Compare) and any(isinstance(x, ast.Constant) and x.value in data['removed_receipt_fields']
                                                         for x in [node.left, *node.comparators]):
                    violations.append(f'{path}:{node.lineno} removed infra status')
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == '_spawn':
                    for call in ast.walk(node):
                        if isinstance(call, ast.Call):
                            target = call_name(call.func, aliases)
                            reflected = (target.endswith('getattr') and len(call.args) > 1
                                         and call_name(call.args[0], aliases).endswith('seat_paths')
                                         and isinstance(call.args[1], ast.Constant)
                                         and str(call.args[1].value).startswith('resolve'))
                            if 'seat_paths.resolve' in target or reflected:
                                violations.append(f'{path}:{call.lineno} seat_paths resolve in _spawn')
        for item in data['required_parameters']:
            tree = trees()[item['file']]
            functions = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                         and node.name == item['function']]
            if len(functions) != 1:
                violations.append(f"{item['file']} missing {item['function']}")
                continue
            args = functions[0].args
            positional = args.posonlyargs + args.args
            required = {a.arg for a in positional[:len(positional) - len(args.defaults)]}
            required.update(a.arg for a, default in zip(args.kwonlyargs, args.kw_defaults) if default is None)
            available = {a.arg for a in positional + args.kwonlyargs}
            if (item['parameter'] not in available or item['required'] and item['parameter'] not in required
                    or item['keyword_only'] and item['parameter'] not in {a.arg for a in args.kwonlyargs}):
                violations.append(f"{item['file']} {item['function']} missing required binding {item['parameter']}")
        self.assertFalse(violations, 'removed symbols restored: ' + ', '.join(violations))
        # The old CI path accepted a result without binding it to the saved nonce.
        ci = trees()['drover/ci.py']
        # Check the sole plan construction itself, independent of variable names.
        plans = [n for n in ast.walk(ci) if isinstance(n, ast.Dict) and any(
            isinstance(k, ast.Constant) and k.value == 'commands' for k in n.keys)]
        self.assertTrue(plans and all(any(isinstance(k, ast.Constant) and k.value == 'nonce' for k in p.keys)
                                      for p in plans), 'nonce-less CI plan restored')
        classify = next(n for n in ast.walk(ci) if isinstance(n, ast.FunctionDef) and n.name == '_classify')
        text = ast.unparse(classify)
        self.assertIn("self.result(spec['run_id'], execution, spec)", text, 'nonce-less CI result restored')
        self.assertIn("evaluated.get('nonce') != spec.get('nonce')", text, 'nonce association removed')
        self.assertIn("evaluated.get('run_id') != spec.get('run_id')", text, 'run association removed')

    def test_no_new_mixins_or_dependencies(self):
        data = manifest(ALLOW)
        compare(self, mixins(), data['mixins'], ('file', 'class', 'base'), 'mixins')
        config = tomllib.loads((ROOT / 'hub/pyproject.toml').read_text(encoding='utf-8'))
        deps = {'project': config['project'].get('dependencies', []),
                'optional': config['project'].get('optional-dependencies', {}),
                'groups': config.get('dependency-groups', {})}
        self.assertEqual(deps, data['dependencies'], 'dependency declarations changed; no new dependencies')
        lock = tomllib.loads((ROOT / 'hub/uv.lock').read_text(encoding='utf-8'))
        self.assertEqual(sorted(p['name'] for p in lock['package']), data['locked_packages'], 'locked dependency set changed')
