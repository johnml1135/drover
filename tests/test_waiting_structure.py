"""Executable retirement and dependency rules for the Waiting boundary."""
import ast
import inspect
import os
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


SKIPPED_DIRS = {'.venv', 'venv', 'env', 'node_modules', 'build', 'dist', 'site-packages', '__pycache__', '.git'}


def production():
    # Vendored environments and build output are not production source (hub/.venv alone is ~500 files), and
    # walking them is slow on /mnt drives, so they are pruned rather than globbed and filtered.
    found = []
    for top in ('drover', 'hub'):
        for folder, dirs, files in os.walk(ROOT / top):
            dirs[:] = [d for d in dirs if d not in SKIPPED_DIRS]
            found += [Path(folder, f) for f in files if f.endswith('.py')]
    return sorted(found)


class Retired(unittest.TestCase):
    def absent(self, names, literals=(), paths=None):
        hits = []
        for path in production():
            if paths is not None and str(path.relative_to(ROOT)) not in paths:
                continue
            # Migration is the sole permitted reader of legacy payloads/keys.
            if path.name == 'waiting_migration.py':
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                name = (node.name if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else
                        node.attr if isinstance(node, ast.Attribute) else
                        node.id if isinstance(node, ast.Name) else None)
                if name in names:
                    hits.append(f'{path.relative_to(ROOT)}:{node.lineno}: {name}')
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if any(node.value.startswith(value) for value in literals):
                        hits.append(f'{path.relative_to(ROOT)}:{node.lineno}: {node.value!r}')
        self.assertEqual(hits, [], 'Retired runtime paths remain:\n' + '\n'.join(hits))

    def test_stale_and_replan_helpers_absent(self):
        self.absent({'_hold_is_stale', 'release_stale_holds', '_release_stale_holds', '_replan'})

    def test_blocker_storage_absent(self):
        self.absent({'_blocker', '_restore', '_holds_of', 'add_hold', 'remove_hold'})
        hits = []
        for path in production():
            if path.name == 'waiting_migration.py':
                continue
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == 'set_status':
                    if any(arg.arg == 'blocker' for arg in node.args.args + node.args.kwonlyargs):
                        hits.append(str(path.relative_to(ROOT)))
                if isinstance(node, ast.Call):
                    callee = getattr(node.func, 'attr', getattr(node.func, 'id', ''))
                    if callee == 'set_status' and any(k.arg == 'blocker' for k in node.keywords):
                        hits.append(f'{path.relative_to(ROOT)}:{node.lineno}')
        self.assertEqual(hits, [], 'Status interfaces still accept/forward blocker payloads')

    def test_legacy_fanout_absent(self):
        self.absent({'_hold', 'replan_hold', 'replan_release'})

    def test_script_origin_parks_absent(self):
        self.absent({'GATE_Q_KEY', 'GATE_CMD_KEY', 'UNKNOWN_REASON', 'PAUSE_KEY',
                     '_gate_question', '_owner_gate_cmd', '_held_for_gate', 'gate_known', 'sync_gates',
                     'paused', 'pause'}, ('_gate_question_', 'gate_cmd_', '_origin_pause_'),
                    paths={'drover/origin.py', 'drover/pipeline.py', 'drover/landing.py', 'drover/dispatcher.py'})

    def test_landing_borrow_tuner_parks_absent(self):
        self.absent({'HOLD_KEY', 'HOLD_QUESTION_KEY', 'held', '_ask_clean', '_director_borrow_approved', '_propose'},
                    ('landing_hold', '_land_dirty_q_'),
                    paths={'drover/landing.py', 'hub/dispatch.py', 'drover/dispatcher.py', 'drover/tuner.py'})

    def test_backoff_paths_absent(self):
        self.absent({'get_backoffs', 'set_backoff', '_prune_backoffs'}, ('_dispatch_backoff',))
        tree = ast.parse((ROOT / 'drover/dispatcher.py').read_text())
        review = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == '_begin_review')
        self.assertNotIn('PROVIDER_COOLDOWN_S', {n.id for n in ast.walk(review) if isinstance(n, ast.Name)})


class Architecture(unittest.TestCase):
    def test_constants_sql_and_layers(self):
        from drover import contract
        from drover.ledger import states
        for name in ('PROPOSED', 'DRAFT', 'READY', 'CLAIMED', 'RUNNING', 'GATED', 'REVIEWED',
                     'LANDED', 'BLOCKED', 'FAILED', 'NEEDS_OWNER', 'CANCELLED'):
            self.assertIsInstance(getattr(states, name, None), str, f'states.{name} is absent')
        for name in ('DONE', 'ERROR', 'NO_RECORD', 'TIMEOUT', 'PROVIDER_ERROR', 'LAUNCH_ERROR',
                     'ORPHANED', 'UNVERIFIED', 'GATE_FAIL', 'GATED', 'HOLD', 'READ_ONLY_DONE'):
            self.assertIsInstance(getattr(contract, name, None), str, f'contract.{name} is absent')
        for filename in ('waiting.py', 'waiting_migration.py'):
            path = ROOT / 'drover/ledger' / filename
            self.assertTrue(path.is_file(), f'{filename} interface is absent')
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    modules = [a.name for a in node.names] + [getattr(node, 'module', '') or '']
                    self.assertFalse(any(part in {'dispatcher', 'pipeline', 'launch', 'hub'}
                                         for module in modules for part in module.split('.')), modules)
                if isinstance(node, ast.Attribute):
                    self.assertNotIn((getattr(node.value, 'id', ''), node.attr),
                                     {('os', 'name'), ('sys', 'platform')})
        from drover.dispatcher import Dispatcher
        self.assertEqual(list(inspect.signature(Dispatcher).parameters),
                         ['port', 'pipeline', 'runtime', 'usage', 'route', 'decide'])
        state_names = set(states.STATES)
        outcome_names = set(contract.OUTCOMES) | {'read-only-done'}
        for path in production():
            if path.name in {'states.py', 'contract.py', 'schema.py', 'waiting_migration.py'}:
                continue
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    name = getattr(node.func, 'attr', getattr(node.func, 'id', ''))
                    # Only the rewritten Waiting module and application methods in this phase
                    # are covered; unrelated legacy calls belong to later lifecycle extraction.
                    if path.name == 'waiting.py' and name in {'set_status', 'finish_run'}:
                        vocabulary = state_names if name == 'set_status' else outcome_names
                        for arg in node.args:
                            if isinstance(arg, ast.Constant) and arg.value in vocabulary:
                                self.fail(f'{path}:{node.lineno}: literal {arg.value!r} instead of canonical constant')
                if path.name == 'dispatcher.py' and isinstance(node, ast.If):
                    self.assertNotEqual(ast.dump(node.test),
                                        ast.dump(ast.Attribute(value=ast.Name(id='self', ctx=ast.Load()),
                                                               attr='pipeline', ctx=ast.Load())),
                                        'Optional-pipeline branches must not return')

        # The changed application methods may call Waiting, but must not manipulate its tables.
        tables = ('wait_subjects', 'wait_causes', 'waits', 'replan_requests')
        for path in production():
            if path.parent == ROOT / 'drover/ledger':
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    sql = node.value.lower().split()
                    if sql and sql[0] in {'select', 'insert', 'update', 'delete', 'create', 'alter'}:
                        self.assertFalse(any(t in sql for t in tables), f'Waiting SQL outside ledger: {path}')

    def test_all_legacy_paths_removed(self):
        checker = Retired()
        for method in ('test_stale_and_replan_helpers_absent', 'test_blocker_storage_absent',
                       'test_legacy_fanout_absent', 'test_script_origin_parks_absent',
                       'test_landing_borrow_tuner_parks_absent', 'test_backoff_paths_absent'):
            with self.subTest(rule=method):
                getattr(checker, method)()
