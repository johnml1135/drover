"""Seat-home resolution has one launch-spec site; validation and usage probes never resolve a launch spec."""
import ast
import unittest

from tests.test_architecture import alias_map, call_name, trees

EXPECTED = {
    ('drover/launch.py', 'RunnerLauncher.launch', 'resolve_spec'),
    ('drover/launch.py', 'RunnerLauncher.launch', 'missing_executable'),   # one missing-executable check (Task 18)
    ('hub/dispatch.py', 'DispatcherService._validate_seat_homes', 'validate'),
}


def scopes(tree):
    """(node, qualified enclosing function) for every node, resolved with the stdlib AST."""
    def walk(node, scope):
        for child in ast.iter_child_nodes(node):
            inner = scope
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                inner = scope + [child.name]
            yield child, '.'.join(scope)
            yield from walk(child, inner)
    return walk(tree, [])


class SeatPathContract(unittest.TestCase):
    def test_resolve_spec_only_in_runner_launch(self):
        used, strings = set(), []
        for path, tree in trees().items():
            if path.startswith('tests/') or path == 'drover/seat_paths.py':
                continue
            aliases = alias_map(path, tree)
            for node, scope in scopes(tree):
                if isinstance(node, ast.Attribute):
                    name = call_name(node, aliases)
                    if name.startswith('drover.seat_paths.'):
                        used.add((path, scope, name.rpartition('.')[2]))
                elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                    name = aliases.get(node.id, '')
                    if name.startswith('drover.seat_paths.'):
                        used.add((path, scope, name.rpartition('.')[2]))
                elif isinstance(node, ast.Constant) and node.value in ('resolve_spec', 'drover.seat_paths'):
                    strings.append(f'{path}:{node.lineno} {node.value!r}')
        self.assertEqual(used, EXPECTED)
        self.assertEqual(strings, [], 'seat_paths must not be reached by name or dynamic import')
