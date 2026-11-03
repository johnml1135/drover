"""Seam guards R1 to R6, R8 and R9: an AST walk of `drover/` and `hub/`, pinned shrink-only by seam_exceptions.json."""
import ast
from collections import Counter
from functools import lru_cache
import sys
import unittest

from tests.test_architecture import ROOT, call_name, compare, manifest, module, parsed

EXCEPTIONS = ROOT / 'tests/seam_exceptions.json'
KEYS = ('rule', 'module', 'where')
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
READ = (ast.Import, ast.ImportFrom, ast.Call, ast.Attribute, ast.Name, ast.List, ast.Tuple)
DYNAMIC = ('__import__', 'builtins.__import__', 'importlib.import_module')
MOVES = ('os.replace', 'os.rename')
DELETIONS = ('rmtree', 'unlink', 'rmdir')
R4_MODULES = tuple(f'drover/{name}.py' for name in (
    'disk_worktrees', 'disk_paths', 'disk_temp', 'disk_budget', 'disk_registrations', 'disk_cleanup'))
# Section 4's `disk_*` modules, as drawn: disk, disk_state, disk_schedule* and disk_cleanup_notices stand outside.
R6_CLUSTER = tuple(f'drover/{name}.py' for name in (
    'disk_authority', 'disk_budget', 'disk_cleanup', 'disk_cleanup_decisions', 'disk_evidence', 'disk_inventory',
    'disk_paths', 'disk_preflight', 'disk_registrations', 'disk_removal', 'disk_runtime', 'disk_side', 'disk_stage',
    'disk_temp', 'disk_worktrees'))
R9_NAMED = ('drover.git_worktrees.parse', 'drover.git_worktrees.AdminFiles')
R9_CALLERS = tuple(f'drover/{name}.py' for name in ('disk_worktrees', 'disk_paths'))    # `disk_evidence` alone from 6a


def only(*allowed):
    return lambda name: name not in allowed


# R5, section 4. A rule is live once its module exists; each entry answers whether an import is a finding.
DIRECTIONS = {
    'drover/durable.py': only(),
    'drover/run_output.py': only(),
    'drover/process.py': only('placement_paths', 'compaction_hold'),
    'drover/git_client.py': only('process', 'placement_git', 'placement_paths'),
    'drover/git_worktrees.py': only('placement_paths', 'git_client'),
    'drover/disk_stage.py': only('placement_paths'),
    'drover/disk_authority.py': lambda name: name in ('process', 'git_client'),
    'drover/disk_inventory.py': lambda name: (name == 'disk' or name.startswith('disk_')) and name != 'disk_state',
    'drover/placement_git.py': lambda name: name == 'git_client',
}


@lru_cache(maxsize=None)
def scoped(tree):
    """Each node a rule reads, with its qualified scope and whether a function encloses it; a `def` carries its name."""
    found = []

    def visit(node, where, inside):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (*FUNCTIONS, ast.ClassDef)):
                name = where + (child.name,)
                is_function = isinstance(child, FUNCTIONS)
                if is_function:
                    found.append((child, name, is_function))
                visit(child, name, inside or is_function)
            else:
                if isinstance(child, READ):
                    found.append((child, where, inside))
                visit(child, where, inside)

    visit(tree, (), False)
    return [(node, '.'.join(where) or '<module>', inside) for node, where, inside in found]


def bound(path, node):
    """The full names one import statement binds, a relative one resolved against its module."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    package = module(path).split('.')
    if not path.endswith('/__init__.py'):
        package.pop()
    base = package[:len(package) - node.level + 1] if node.level else []
    base = base + (node.module.split('.') if node.module else [])
    return ['.'.join(base + [alias.name]) for alias in node.names]


@lru_cache(maxsize=None)
def aliases_of(path, tree):
    result = {}
    for node, _, _ in scoped(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias, name in zip(node.names, bound(path, node)):
                if alias.asname or isinstance(node, ast.ImportFrom):
                    result[alias.asname or alias.name] = name
                else:
                    result[name.split('.')[0]] = name.split('.')[0]
    return result


def is_named(node, names, aliases):
    """Whether a name or attribute resolves to one of `names`; the cheap test first, as most nodes are neither."""
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id) in names
    return isinstance(node, ast.Attribute) and any(name.endswith('.' + node.attr) for name in names) \
        and call_name(node, aliases) in names


def imported(path, node, aliases):
    """The names a statement or a dynamic import call loads, with the `__import__` handling of `debt()`."""
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return bound(path, node)
    if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) \
            and isinstance(node.args[0].value, str) and is_named(node.func, DYNAMIC, aliases):
        return [node.args[0].value]
    return []


def project_module(name):
    """`drover.a.b` is module `a`; a hub or bare drover import keeps its name; anything else is not ours."""
    parts = name.split('.')
    if parts[0] == 'drover':
        return parts[1] if len(parts) > 1 else name
    return name if parts[0] == 'hub' else None


def r1(path, node, aliases, inside):
    if path == 'drover/process.py':
        return 0
    if isinstance(node, ast.Attribute):
        return int(node.attr == 'subprocess')
    return sum(name.split('.')[0] == 'subprocess' or name.endswith('.subprocess')
               for name in imported(path, node, aliases))


def r2(path, node, aliases, inside):
    if path == 'drover/durable.py':
        return 0
    if is_named(node, MOVES, aliases):
        return 1
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return 0
    if node.func.attr not in ('replace', 'rename') or is_named(node.func, (*MOVES, 'dataclasses.replace'), aliases):
        return 0
    return int(node.func.attr == 'rename' or len(node.args) == 1 and not node.keywords)


def answer_member(node, aliases, member):
    return isinstance(node, ast.Attribute) and node.attr == member \
        and call_name(node, aliases).split('.')[-2:] == ['Answer', member]


def own_nodes(function):
    """A function's body without the bodies of the functions inside it."""
    pending = list(ast.iter_child_nodes(function))
    while pending:
        node = pending.pop()
        yield node
        if not isinstance(node, FUNCTIONS):
            pending.extend(ast.iter_child_nodes(node))


def compares_no_alone(function, aliases):
    nodes = list(own_nodes(function))
    no = any(answer_member(part, aliases, 'NO') for node in nodes if isinstance(node, (ast.Compare, ast.MatchValue))
             for part in ast.walk(node))
    return no and not any(answer_member(node, aliases, 'UNKNOWN') for node in nodes)


def r3(path, node, aliases, inside):
    if isinstance(node, FUNCTIONS):
        return int(compares_no_alone(node, aliases))
    if path in ('drover/placement_git.py', 'drover/git_client.py') or not isinstance(node, (ast.List, ast.Tuple)):
        return 0
    return int(any(isinstance(part, ast.Constant) and part.value == 'git' for part in node.elts))


def r4(path, node, aliases, inside):
    if path not in R4_MODULES:
        return 0
    if isinstance(node, ast.Attribute):
        return int(node.attr in DELETIONS)
    return int(isinstance(node, ast.Name) and call_name(node, aliases).split('.')[-1] in DELETIONS)


def r5(path, node, aliases, inside):
    forbidden = DIRECTIONS.get(path)
    if forbidden is None:
        return 0
    names = [project_module(name) for name in imported(path, node, aliases)]
    return sum(name is not None and forbidden(name) for name in names)


def r6(path, node, aliases, inside):
    if path not in R6_CLUSTER or not inside:
        return 0
    return int(any(project_module(name) for name in imported(path, node, aliases)))


def r8(path, node, aliases, inside):
    if path == 'drover/git_worktrees.py' or not isinstance(node, (ast.Call, ast.List, ast.Tuple)):
        return 0
    parts = node.args if isinstance(node, ast.Call) else node.elts
    values = [part.value if isinstance(part, ast.Constant) else None for part in parts]
    return int(('worktree', 'list') in zip(values, values[1:]))


def r9(path, node, aliases, inside):
    if path == 'drover/git_worktrees.py':
        return 0
    named = R9_NAMED if path in R9_CALLERS else (*R9_NAMED, 'drover.git_worktrees.prove')
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return sum(name in named for name in imported(path, node, aliases))
    return int(is_named(node, named, aliases))


RULES = {'R1': r1, 'R2': r2, 'R3': r3, 'R4': r4, 'R5': r5, 'R6': r6, 'R8': r8, 'R9': r9}


def rule_violations(rule, sources):
    """One row per module and scope that breaks `rule`; `sources` maps a path to its text or its parsed tree."""
    rows = Counter()
    for path, source in sources.items():
        tree = ast.parse(source) if isinstance(source, str) else source
        aliases = aliases_of(path, tree)
        for node, where, inside in scoped(tree):
            rows[(path, where)] += RULES[rule](path, node, aliases, inside)
    return [{'rule': rule, 'module': path, 'where': where, 'count': count}
            for (path, where), count in sorted(rows.items()) if count]


@lru_cache(maxsize=1)
def found_rows():
    sources = {**parsed('drover'), **parsed('hub')}
    return Counter({tuple(row[key] for key in KEYS): row['count']
                    for rule in RULES for row in rule_violations(rule, sources)})


class Rules(unittest.TestCase):
    def found(self, rule, path, text):
        return [(row['where'], row['count']) for row in rule_violations(rule, {path: text})]

    def test_r1_sees_import_dunder_import_and_placement_subprocess(self):
        for text in ('import subprocess', "__import__('subprocess')", 'placement.subprocess.run([])'):
            self.assertEqual(rule_violations('R1', {'drover/x.py': text})[0]['module'], 'drover/x.py')

    def test_r1_keys_by_function_and_leaves_process_alone(self):
        text = 'import subprocess\nclass C:\n    def f(self):\n        from subprocess import run\n'
        self.assertEqual(self.found('R1', 'hub/x.py', text), [('<module>', 1), ('C.f', 1)])
        self.assertEqual(self.found('R1', 'hub/x.py', 'from .placement import subprocess'), [('<module>', 1)])
        self.assertEqual(self.found('R1', 'drover/process.py', 'import subprocess'), [])

    def test_r2_sees_replace_rename_and_one_argument_path_replace(self):
        for text in ('os.replace(a, b)', 'os.rename(a, b)', 'Path(a).rename(b)', 'target.replace(b)',
                     'from os import replace as swap\nswap(a, b)'):
            self.assertEqual(self.found('R2', 'drover/x.py', text), [('<module>', 1)], text)
        self.assertEqual(self.found('R2', 'drover/durable.py', 'os.replace(a, b)'), [])

    def test_r2_ignores_two_argument_str_replace(self):
        for text in ("text.replace('a', 'b')", 'when.replace(tzinfo=None)', 'dataclasses.replace(row)'):
            self.assertEqual(self.found('R2', 'drover/x.py', text), [], text)

    def test_r3_sees_git_argv_outside_placement_git(self):
        text = "def f():\n    return ['git', '-C', repo]\ndef g():\n    return ('wsl.exe', 'git', 'status')\n"
        self.assertEqual(self.found('R3', 'drover/x.py', text), [('f', 1), ('g', 1)])
        for path in ('drover/placement_git.py', 'drover/git_client.py'):
            self.assertEqual(self.found('R3', path, text), [])
        self.assertEqual(self.found('R3', 'drover/x.py', "name = 'git'\nsend(['status'])"), [])

    def test_r3_requires_unknown_beside_answer_no(self):
        alone = 'def f(a):\n    return a is Answer.NO\n'
        self.assertEqual(self.found('R3', 'drover/x.py', alone), [('f', 1)])
        self.assertEqual(self.found('R3', 'drover/git_client.py', alone), [('f', 1)])
        within = 'def f(a):\n    return a in (Answer.NO, Answer.YES)\n'
        self.assertEqual(self.found('R3', 'drover/x.py', within), [('f', 1)])
        beside = 'def f(a):\n    if a is Answer.UNKNOWN:\n        raise E\n    return a is Answer.NO\n'
        self.assertEqual(self.found('R3', 'drover/x.py', beside), [])
        self.assertEqual(self.found('R3', 'drover/x.py', 'def f(a):\n    return a is Answer.YES\n'), [])

    def test_r4_sees_rmtree_unlink_rmdir_in_listed_modules(self):
        text = 'def f(p):\n    shutil.rmtree(p)\n    p.unlink()\n    p.rmdir()\n'
        for name in ('disk_worktrees', 'disk_paths', 'disk_temp', 'disk_budget', 'disk_registrations', 'disk_cleanup'):
            self.assertEqual(self.found('R4', f'drover/{name}.py', text), [('f', 3)])
        for name in ('disk_removal', 'disk_state', 'disk_cleanup_notices', 'ci'):
            self.assertEqual(self.found('R4', f'drover/{name}.py', text), [])
        self.assertEqual(self.found('R4', 'drover/disk_temp.py', 'from shutil import rmtree as drop\ndrop(p)'), [('<module>', 1)])

    def test_r5_direction_rule_goes_live_with_its_module(self):
        at_load = [('<module>', 1)]
        self.assertEqual(rule_violations('R5', {'drover/x.py': 'from . import config'}), [])
        self.assertEqual(self.found('R5', 'drover/durable.py', 'import json\nimport os'), [])
        self.assertEqual(self.found('R5', 'drover/durable.py', 'from . import config'), at_load)
        self.assertEqual(self.found('R5', 'drover/run_output.py', 'from drover.ledger import codec'), at_load)
        self.assertEqual(self.found('R5', 'drover/process.py', 'from . import compaction_hold, placement_paths'), [])
        self.assertEqual(self.found('R5', 'drover/process.py', 'from .placement import git'), at_load)
        self.assertEqual(self.found('R5', 'drover/git_client.py', 'from . import placement_git, process'), [])
        self.assertEqual(self.found('R5', 'drover/git_client.py', 'from . import config'), at_load)
        self.assertEqual(self.found('R5', 'drover/git_worktrees.py', 'from . import git_client, placement'), at_load)
        self.assertEqual(self.found('R5', 'drover/disk_stage.py', 'from . import durable'), at_load)
        self.assertEqual(self.found('R5', 'drover/disk_authority.py', 'from . import config, disk_state'), [])
        self.assertEqual(self.found('R5', 'drover/disk_authority.py', 'from .git_client import ask'), at_load)
        self.assertEqual(self.found('R5', 'drover/disk_inventory.py', 'from . import disk_state, placement'), [])
        text = 'def f():\n    from .disk_paths import x\n'
        self.assertEqual(self.found('R5', 'drover/disk_inventory.py', text), [('f', 1)])
        text = 'def f():\n    from . import git_client\n'
        self.assertEqual(self.found('R5', 'drover/placement_git.py', text), [('f', 1)])

    def test_r6_counts_sibling_imports_inside_functions(self):
        text = ('from .disk_temp import y\n'
                'def f():\n    import json\n    from .disk_temp import x, y\n    from . import config, repos\n')
        self.assertEqual(self.found('R6', 'drover/disk_paths.py', text), [('f', 2)])
        self.assertEqual(self.found('R6', 'drover/disk_state.py', text), [])
        self.assertEqual(self.found('R6', 'drover/ci.py', text), [])
        self.assertEqual(self.found('R6', 'drover/disk_paths.py', 'class C:\n    from . import repos\n'), [])

    def test_r8_sees_the_literal_in_a_call_a_list_and_a_tuple(self):
        for text in ("git(repo, 'worktree', 'list', '--porcelain')", "args = ['worktree', 'list']",
                     "args = ('-C', repo, 'worktree', 'list')", 'git("worktree", "list")'):
            self.assertEqual(self.found('R8', 'drover/x.py', text), [('<module>', 1)], text)
        for text in ("git(repo, 'worktree', 'add', 'list')", "git('list', 'worktree')", "git('worktree', name)"):
            self.assertEqual(self.found('R8', 'hub/x.py', text), [], text)
        self.assertEqual(self.found('R8', 'drover/git_worktrees.py', "git(repo, 'worktree', 'list')"), [])

    def test_r9_only_named_callers_reach_prove_and_nothing_names_parse(self):
        call = 'from . import git_worktrees\ndef f():\n    return git_worktrees.prove(listings, repo, path)\n'
        self.assertEqual(self.found('R9', 'drover/disk_cleanup.py', call), [('f', 1)])
        self.assertEqual(self.found('R9', 'hub/x.py', 'from drover.git_worktrees import prove as check'), [('<module>', 1)])
        for path in R9_CALLERS:
            self.assertEqual(self.found('R9', path, call), [])
        for text in ('from .git_worktrees import parse', 'from drover.git_worktrees import AdminFiles',
                     'from . import git_worktrees\ngit_worktrees.parse(admin, files)'):
            for path in ('drover/disk_paths.py', 'drover/x.py'):
                self.assertEqual(self.found('R9', path, text), [('<module>', 1)], text)
        self.assertEqual(self.found('R9', 'drover/git_worktrees.py', 'from .git_worktrees import parse\nparse(a, b)'), [])
        self.assertEqual(self.found('R9', 'drover/x.py', 'import json\ndef parse(text):\n    return json.parse(text)\n'), [])
        self.assertEqual(rule_violations('R9', {**parsed('drover'), **parsed('hub')}), [])


class Seams(unittest.TestCase):
    def test_durable_imports_no_drover_module(self):
        path, tree = 'drover/durable.py', parsed('drover')['drover/durable.py']
        self.assertEqual(rule_violations('R5', {path: tree}), [])
        names = {name.split('.')[0] for node, _, _ in scoped(tree) for name in imported(path, node, aliases_of(path, tree))}
        self.assertLessEqual(names, set(sys.stdlib_module_names))

    def test_process_imports_only_placement_paths_and_compaction_hold(self):
        path, tree = 'drover/process.py', parsed('drover')['drover/process.py']
        self.assertEqual(rule_violations('R5', {path: tree}), [])
        names = [name for node, _, _ in scoped(tree) for name in imported(path, node, aliases_of(path, tree))]
        self.assertLessEqual({project_module(name) for name in names} - {None}, {'placement_paths', 'compaction_hold'})
        self.assertLessEqual({name.split('.')[0] for name in names if project_module(name) is None},
                             set(sys.stdlib_module_names))

    def test_git_client_imports_only_process_placement_git_and_placement_paths(self):
        path, tree = 'drover/git_client.py', parsed('drover')['drover/git_client.py']
        self.assertEqual(rule_violations('R5', {path: tree}), [])
        names = [name for node, _, _ in scoped(tree) for name in imported(path, node, aliases_of(path, tree))]
        self.assertLessEqual({project_module(name) for name in names} - {None},
                             {'process', 'placement_git', 'placement_paths'})

    def test_r5_placement_git_imports_no_git_client_but_its_stated_rows(self):
        """`git_client` imports `placement_git` at load, so `placement_git` may reach it only inside a function, by a
        stated manifest row (section 13, R5; Task 27 adds them). A load-time import is never allowed to stand."""
        found = {where: count for (rule, path, where), count in found_rows().items()
                 if rule == 'R5' and path == 'drover/placement_git.py'}
        stated = {row['where']: row['count'] for row in manifest(EXCEPTIONS)
                  if row['rule'] == 'R5' and row['module'] == 'drover/placement_git.py'}
        self.assertNotIn('<module>', found)
        self.assertNotIn('<module>', stated)
        self.assertEqual(found, stated)

    def test_the_console_flag_is_defined_only_in_its_three_homes(self):
        """Section 5.1: `process` holds the flag; frozen hub/runtime (ruling 15) and the standalone hook keep a copy."""
        def defines(node):
            if isinstance(node, ast.Constant) and node.value == 0x08000000 and not isinstance(node.value, bool):
                return True
            if isinstance(node, ast.Attribute) and node.attr == 'CREATE_NO_WINDOW':
                return not (isinstance(node.value, ast.Name) and node.value.id == 'process')
            targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
            return any(isinstance(t, ast.Name) and t.id in ('NO_WINDOW', 'CREATE_NO_WINDOW') for t in targets)
        trees = {**parsed('drover'), **parsed('hub'), **parsed('scripts')}
        homes = {path for path, tree in trees.items() if any(defines(node) for node in ast.walk(tree))}
        self.assertEqual(homes, {'drover/process.py', 'hub/runtime.py', 'drover/pane_status_hook.py'})
        self.assertTrue(defines(ast.parse('import subprocess\nf = subprocess.CREATE_NO_WINDOW').body[1].value))

    def test_rows_equal_the_manifest(self):
        compare(self, found_rows(), manifest(EXCEPTIONS), KEYS, 'seam exceptions')

    def test_every_row_has_a_reason(self):
        actual = found_rows()
        reasons = {tuple(row[key] for key in KEYS): row.get('reason') for row in manifest(EXCEPTIONS)}
        for key in actual:
            self.assertTrue(str(reasons.get(key) or '').strip(), key)

    def rows_of(self, rule):
        return {key[1:]: count for key, count in found_rows().items() if key[0] == rule}

    def test_r8_literal_only_in_git_worktrees_and_its_exceptions(self):
        self.assertEqual(self.rows_of('R8'), {('drover/origin.py', 'fast_forward'): 1})     # its section 13 end row

    def test_r5_git_worktrees_reaches_only_git_client_and_placement_paths(self):
        rows = {where: count for where, count in self.rows_of('R5').items() if where[0] == 'drover/git_worktrees.py'}
        self.assertEqual(rows, {})
