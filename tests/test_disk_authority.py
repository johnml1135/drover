"""disk_authority: pure selection and grace; its one write is quarantine state, through CleanupState."""
import ast
import copy
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from drover import disk_authority, disk_state, placement
from drover.disk_evidence import StageEvidence
from drover.disk_stage import StageClock, StageRequest
from drover.git_worktrees import Proof, Refusal

SOURCE = Path(__file__).resolve().parent.parent / 'drover' / 'disk_authority.py'
NOW = 1_000_000
ROW = dict(name='alpha', path='/w/alpha', action='keep', reason_code='orphan', dirty=False)
NOT_ELIGIBLE = 'not an eligible owning-side worktree: '


def observation(identity='one'):
    return dict(row=dict(ROW), quarantine=dict(key='/w/alpha', identity=identity))


def stage(**changes):
    value = dict(stage=5, side='windows', mode=None, apply=True, now=NOW, drive=dict(path='/drive'), repos=(),
                 policy={}, state_dir='/state', tasks={})
    value.update(changes)
    return StageRequest(**value)


class Evidence:
    """A StageEvidence fake for the first-pass reads `rule_disposable` asks."""
    def __init__(self, *, busy=False, age_s=10 ** 6):
        self.busy, self.age_s, self.ignored = busy, age_s, []

    def processes(self):
        return SimpleNamespace(busy=lambda path: self.busy)

    def measure(self, path, *, ignore_activity=(), strict=True):
        self.ignored.append(ignore_activity)
        return dict(files=1, bytes=1, age_s=self.age_s)

    def head(self, path):
        return 'head'

    def tag(self, path):
        return dict(owner='drover:7')

    def declarations(self, machine, path):
        return ['bin']


class DisposableRules(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.tree = Path(folder.name).absolute()
        self.machine = dict(repo=str(self.tree.parent / 'repo'))
        self.entry = dict(worktree=str(self.tree), locked='windows')

    def judged(self, *, request=None, evidence=None, machine=None, entry=None, side='windows', safe=True,
               contains=True, fresh=None, touched=()):
        run = SimpleNamespace(request=request or stage(), evidence=evidence or Evidence())
        with ExitStack() as stack:
            stack.enter_context(patch.object(placement, 'cleanup_side', return_value='windows'))
            stack.enter_context(patch.object(placement, 'worktree_side', return_value=side))
            stack.enter_context(patch.object(placement, 'cleanup_contains', return_value=contains))
            stack.enter_context(patch.object(placement, 'physical_disk_path', return_value=None))
            stack.enter_context(patch.object(disk_authority, 'safe_path', return_value=safe))
            if fresh is not None:
                return disk_authority.rule_disposable_fresh(run, machine or self.machine, entry or self.entry, fresh,
                                                            touched=touched)
            return disk_authority.rule_disposable(run, machine or self.machine, entry or self.entry, touched=touched)

    def refusals(self):
        """(name, judged keywords, the exact error text today's rows carry) for each output_evidence rejection."""
        missing = self.tree / 'missing'
        return [('main_checkout', dict(machine=dict(repo=str(self.tree))),
                 f'{NOT_ELIGIBLE}main_checkout (repo={str(self.tree)!r})'),
                ('not_directory', dict(entry=dict(self.entry, worktree=str(missing))),
                 f'{NOT_ELIGIBLE}not_directory (path={str(missing)!r})'),
                ('unsafe_path', dict(safe=False), f'{NOT_ELIGIBLE}unsafe_path (path={str(self.tree)!r})'),
                ('owner_side', dict(side='wsl'),
                 f"{NOT_ELIGIBLE}owner_side (owner='wsl', worker='windows', lock='windows')"),
                ('physical_drive', dict(contains=False), f'{NOT_ELIGIBLE}physical_drive (path_side='),
                ('process', dict(evidence=Evidence(busy=True)), 'process using worktree'),
                ('windows_process', dict(request=stage(budget=dict(windows_paths=[str(self.tree / 'bin')]))),
                 'windows process using worktree'),
                ('idle', dict(evidence=Evidence(age_s=60)), 'worktree idle less than 7200 seconds')]

    def test_a_rule_returns_a_ruling_and_never_raises_to_mean_no(self):
        for name, options, _ in self.refusals():
            with self.subTest(refusal=name):
                ruling = self.judged(**options)
                self.assertIsInstance(ruling, disk_authority.Ruling)
                self.assertEqual((ruling.action, ruling.facts), ('keep', None))
        self.assertEqual(self.judged().action, 'candidate')

    def test_reason_code_and_error_text_are_stable(self):
        """The reason and error a row carries today, which the decision trace reads back as each rejection's words."""
        for name, options, text in self.refusals():
            with self.subTest(refusal=name):
                ruling = self.judged(**options)
                self.assertEqual(ruling.reason_code, 'output_evidence_unavailable')
                self.assertTrue(ruling.detail.startswith(text), ruling.detail)
        ruling = self.judged(request=stage(policy=dict(worktree_output_idle_s=90)), evidence=Evidence(age_s=60))
        self.assertEqual(ruling.detail, 'worktree idle less than 90 seconds')

    def test_idle_waiver_moved_verbatim(self):
        """A drive at hold or critical waives the idle rule; a lesser level never does, nor a budget at any level."""
        young = dict(evidence=Evidence(age_s=60))
        for level, budget, action in (('hold', None, 'candidate'), ('critical', None, 'candidate'),
                                      (None, None, 'keep'), ('warn', None, 'keep'), ('queue', None, 'keep'),
                                      ('critical', dict(windows_paths=[]), 'keep'), ('hold', dict(windows_paths=[]), 'keep')):
            with self.subTest(level=level, budget=budget):
                drive = dict(path='/drive', **(dict(level=level) if level else {}))
                self.assertEqual(self.judged(request=stage(drive=drive, budget=budget), **young).action, action)

    def test_facts_carry_what_disposable_rows_unpack(self):
        evidence, touched = Evidence(), {self.tree / 'bin'}
        ruling = self.judged(evidence=evidence, touched=touched)
        self.assertEqual((ruling.action, ruling.reason_code), ('candidate', 'declared_disposable'))
        self.assertEqual(ruling.facts, dict(path=self.tree, head='head', tag=dict(owner='drover:7'), declarations=['bin']))
        self.assertEqual(evidence.ignored, [touched])      # idle is measured ignoring this worktree's own removals

    def test_fresh_rule_refuses_a_first_pass_proof(self):
        busy = SimpleNamespace(busy=lambda path: True)
        with self.assertRaises(TypeError):
            self.judged(fresh=busy)
        with self.assertRaises(TypeError):
            self.judged(fresh=Evidence().processes())
        fresh = StageEvidence(stage(), StageClock(1800), processes=lambda **_: busy).fresh_processes()
        ruling = self.judged(fresh=fresh)         # the first pass's proof (Evidence) is idle; the fresh one is busy
        self.assertEqual((ruling.action, ruling.detail), ('keep', 'process using worktree'))

    def test_rule_unit_fresh_reads_overlaps_and_refused(self):
        target = self.tree / 'bin'
        unit = disk_authority.DisposableUnit(machine=self.machine, registration=self.entry,
                                             evidence=(self.tree, 'head', None, ['bin']), touched=set(), target=target,
                                             measured={str(target / 'x'): dict(bytes=1)}, row={})
        run = SimpleNamespace(request=stage(), evidence=StageEvidence(stage(), StageClock(1800), processes=None))

        def proof(refused=(), reach=()):
            evidence = StageEvidence(stage(), StageClock(1800), processes=None,
                                     prove=lambda *_, **__: Proof(refused, reach))
            return evidence.fresh_registrations('/repo', target, self.tree, pointer=True)
        nested = (placement.cleanup_parts(target / 'x' / 'inner'),)
        cases = (('unknown', proof((Refusal('/repo', None, 'unknown', 'gitdir'),)),
                  ('keep', 'registration_unknown', 'disposable registration changed (gitdir)')),
                 ('changed', proof((Refusal('/repo', str(self.tree), 'changed', 'HEAD'),)),
                  ('keep', 'output_evidence_unavailable', 'disposable registration changed (HEAD)')),
                 ('overlaps', proof(reach=nested), ('keep', 'output_evidence_unavailable', 'disposable side marker changed')),
                 ('clean', proof(), ('candidate', 'declared_disposable', None)))
        for name, fresh, expected in cases:
            with self.subTest(proof=name), patch.object(placement, 'cleanup_contains', return_value=True), \
                    patch.object(disk_authority, 'repository_tree', return_value=False):
                ruling = disk_authority.rule_unit_fresh(run, unit, fresh)
                self.assertEqual((ruling.action, ruling.reason_code, ruling.detail), expected)
        with self.assertRaises(TypeError):
            disk_authority.rule_unit_fresh(run, unit, SimpleNamespace(refused=(), overlaps=False))


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
