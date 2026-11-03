"""disk_authority: pure selection and grace; its one write is quarantine state, through CleanupState."""
import ast
import copy
import unittest
from pathlib import Path

from drover import disk_authority, disk_state

SOURCE = Path(__file__).resolve().parent.parent / 'drover' / 'disk_authority.py'
NOW = 1_000_000
ROW = dict(name='alpha', path='/w/alpha', action='keep', reason_code='orphan', dirty=False)


def observation(identity='one'):
    return dict(row=dict(ROW), quarantine=dict(key='/w/alpha', identity=identity))


class Authority(unittest.TestCase):
    def test_decide_is_pure_and_changes_neither_argument(self):
        request = dict(apply=True, now=NOW)
        observations = [observation()]
        previous = {'quarantine': {'/w/other': dict(identity='x', since=5)}}
        frozen = copy.deepcopy((request, observations, previous))
        answer = disk_authority.decide(request, observations, previous, NOW)
        self.assertEqual((request, observations, previous), frozen)
        self.assertEqual(answer['events'], [dict(kind='quarantine_started', path='/w/alpha')])
        self.assertEqual(answer['state']['quarantine']['/w/alpha'], dict(identity='one', since=NOW))
        self.assertIsNot(answer['state'], previous)

    def test_report_leaves_the_store_untouched(self):
        class Store:
            def update(self, transform):
                raise AssertionError('a report must not write state')
        answer = disk_authority.decide_and_store(dict(apply=False, now=NOW), [observation()], Store())
        self.assertEqual(answer['events'], [])

    def test_apply_stores_the_new_state_through_the_adapter(self):
        class Store:
            state = {}

            def update(self, transform):
                return transform(self.state)
        store = Store()
        disk_authority.decide_and_store(dict(apply=True, now=NOW), [observation()], store)
        self.assertEqual(store.state['quarantine']['/w/alpha']['since'], NOW)

    def test_cleanup_state_is_the_disk_state_adapter(self):
        self.assertEqual(disk_state.CleanupState.__module__, 'drover.disk_state')

    def test_authority_writes_only_quarantine_state(self):
        tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
        banned = {'write', 'write_text', 'write_bytes', 'unlink', 'rmtree', 'rmdir', 'remove', 'replace',
                  'rename', 'open', 'append_manifest', 'save_file'}
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, 'id', '')
                if name in banned:
                    found.append((node.lineno, name))
        self.assertEqual(found, [])
        stored = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                  and n.func.attr == 'update' and isinstance(n.func.value, ast.Name) and n.func.value.id == 'store']
        self.assertEqual(len(stored), 1, 'decide_and_store is the one place state is stored')


if __name__ == '__main__':
    unittest.main()
