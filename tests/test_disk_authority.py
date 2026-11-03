"""disk_authority: pure selection and grace; its one write is quarantine state, through CleanupState."""
import ast
import copy
import tempfile
import unittest
from contextlib import ExitStack
from inspect import signature as inspect_signature
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


class Worktree:
    """A StageEvidence fake for the reads `inspect` asks of one listed worktree; `head` is never read on a missing row."""
    def __init__(self, *, age_s=10, snapshot=None, terminal=True):
        self.age_s, self.terminal, self.heads = age_s, terminal, []
        self.snapshot = snapshot if snapshot is not None else dict(tasks=[dict(id=7, state='yanked')])

    def task_facts(self, path, tag):
        return (False, self.terminal if tag else False)

    def tag(self, path):
        return dict(v=2, repo='alpha', owner='drover:7', lease_until=NOW + 100)

    def status(self, path, *, expand=True):
        return [], []

    def head(self, path):
        self.heads.append(path)
        return 'head'

    def gitdir(self, path):
        return str(path) + '/.git'

    def measure(self, path, **_):
        return dict(files=1, bytes=1, age_s=self.age_s)

    def merged(self, machine, head):
        return False

    def fresh_snapshot(self):
        return self.snapshot


IDLE = SimpleNamespace(busy=lambda path: False)


class WorktreeRules(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.tree = Path(folder.name).absolute()
        self.machine = dict(name='alpha', repo=str(self.tree.parent / 'repo'))
        self.entry = dict(worktree=str(self.tree), HEAD='listed', branch='refs/heads/b', locked='windows')

    def inspected(self, evidence, entry=None, **options):
        run = SimpleNamespace(request=stage(), evidence=evidence)
        with patch.object(placement, 'cleanup_side', return_value='windows'), \
                patch.object(placement, 'worktree_side', return_value='windows'), \
                patch.object(placement, 'cleanup_contains', return_value=True), \
                patch.object(disk_authority, 'safe_path', return_value=True):
            return disk_authority.inspect(run, self.machine, entry or self.entry, **options)

    def test_inspect_requires_processes(self):
        with self.assertRaises(TypeError):
            self.inspected(Worktree())
        self.assertEqual(self.inspected(Worktree(), processes=SimpleNamespace(busy=lambda path: True))['reason_code'],
                         'busy')

    def test_a_lazy_proof_is_taken_only_past_the_path_checks(self):
        taken = []
        def take():
            taken.append('proof')
            return IDLE
        run = SimpleNamespace(request=stage(), evidence=Worktree())
        with patch.object(placement, 'cleanup_side', return_value='windows'), \
                patch.object(placement, 'worktree_side', return_value='wsl'):
            row = disk_authority.inspect(run, self.machine, self.entry, processes=disk_authority.LazyProof(take))
        self.assertEqual((row['reason_code'], taken), ('other_side_or_lock_note', []))
        self.inspected(Worktree(), processes=disk_authority.LazyProof(take))
        self.assertEqual(taken, ['proof'])

    def test_stage_4_takes_no_process_proof_for_rows_its_path_checks_keep(self):
        """No first-pass proof means no process scan and no process report rows, as before the move."""
        from drover import disk_worktrees
        from tests.disk_fixture import listing_answers, staged
        with tempfile.TemporaryDirectory() as state, listing_answers([dict(worktree=str(self.tree))]), \
                patch.object(disk_worktrees, 'registrations', return_value={}), \
                patch.object(disk_worktrees, 'unlisted', return_value=[]), \
                patch.object(placement, 'worktree_side', return_value='elsewhere'), \
                patch.object(placement, 'cleanup_processes', side_effect=AssertionError('process proof taken')):
            out = staged(disk_worktrees.settled, dict(apply=False, now=NOW, repos=[self.machine], state_dir=state))
        self.assertEqual([row['reason_code'] for row in out['items']], ['other_side_or_lock_note'])

    def test_missing_row_keeps_head_from_the_listing_and_needs_no_tip(self):
        evidence = Worktree()
        row = self.inspected(evidence, dict(self.entry, worktree=str(self.tree / 'gone')), processes=IDLE)
        self.assertEqual((row['action'], row['reason_code'], row['head']), ('candidate', 'missing', 'listed'))
        self.assertEqual(evidence.heads, [])

    def test_needs_idle_gate_goes_only_after_three_days_quiet(self):
        self.assertEqual(disk_authority.CLOSED_IDLE_S, 3 * 86400)
        tag = Worktree().tag(None)
        for snapshot, gated in ((dict(tasks=[dict(id=7, state='yanked')]), True),
                                (dict(tasks=[dict(id=7, state='landed')]), False),
                                (dict(tasks=[dict(id=8, state='yanked')]), False)):
            with self.subTest(snapshot=snapshot):
                run = SimpleNamespace(evidence=Worktree(snapshot=snapshot))
                self.assertIs(disk_authority.needs_idle_gate(run, tag), gated)
        self.assertIs(disk_authority.needs_idle_gate(SimpleNamespace(evidence=Worktree(snapshot={})), None), False)
        for age_s, reason in ((disk_authority.CLOSED_IDLE_S - 1, 'unsettled'), (disk_authority.CLOSED_IDLE_S, 'settled')):
            with self.subTest(age_s=age_s):
                self.assertEqual(self.inspected(Worktree(age_s=age_s), processes=IDLE)['reason_code'], reason)

    def test_rule_temp_entry_holds_only_the_static_checks(self):
        """Protected roots, a live owner and a repository above are the rule's; the scan below is disk_temp's."""
        state, temp = self.tree / 'state', self.tree / 'temp'
        (temp / 'entry' / '.git').mkdir(parents=True)        # the scan refuses this descendant, not the rule
        run = SimpleNamespace(request=stage(state_dir=str(state), repos=()),
                              evidence=SimpleNamespace(fresh_snapshot=lambda: None))
        with patch.object(disk_authority, 'repository_root', side_effect=lambda path: path == self.tree):
            self.assertEqual(disk_authority.rule_temp_entry(run, temp / 'entry'), disk_authority.Ruling(
                'keep', 'protected', 'protected subtree'))
        for path, action in ((temp / 'entry', 'candidate'), (state / 'x', 'keep'), (state.parent, 'keep')):
            with self.subTest(path=path), patch.object(disk_authority, 'repository_root', return_value=False):
                self.assertEqual(disk_authority.rule_temp_entry(run, path).action, action)
        with patch.object(disk_authority, 'safe_path', return_value=False):
            self.assertEqual(disk_authority.rule_temp_entry(run, temp / 'entry').action, 'keep')
        live = SimpleNamespace(request=run.request, evidence=SimpleNamespace(
            fresh_snapshot=lambda: dict(active_worktrees=[str(temp)]), task_facts=lambda path, tag: (True, False)))
        self.assertEqual(disk_authority.rule_temp_entry(live, temp / 'entry').action, 'keep')     # a live owner above
        self.assertEqual(list(inspect_signature(disk_authority.rule_temp_entry).parameters), ['run', 'path'])

    def test_rule_cache_file_age_and_level(self):
        day = 86400
        def ruled(age_days, *, mode='daily', level=None, max_age_days=None):
            drive = dict(path='/drive', **(dict(level=level) if level else {}))
            run = SimpleNamespace(request=stage(mode=mode, drive=drive))
            info = SimpleNamespace(st_atime=NOW - age_days * day, st_mtime=NOW - age_days * day - 5)
            entry = dict(path='/cache', **(dict(max_age_days=max_age_days) if max_age_days else {}))
            return disk_authority.rule_cache_file(run, Path('/cache/f'), info, entry=entry)
        for options, action in ((dict(age_days=31), 'candidate'), (dict(age_days=29), 'keep'),
                                (dict(age_days=3, max_age_days=2), 'candidate'), (dict(age_days=1, max_age_days=2), 'keep'),
                                (dict(age_days=1, mode='on-demand', level='warn'), 'candidate'),
                                (dict(age_days=1, mode='on-demand', level='critical'), 'candidate'),
                                (dict(age_days=1, mode='daily', level='critical'), 'keep'),
                                (dict(age_days=1, mode='on-demand'), 'keep'),
                                (dict(age_days=1, mode='on-demand', level='ok'), 'keep'),     # ok is not warn or worse
                                (dict(age_days=30), 'keep')):       # exactly the limit is not older than it
            with self.subTest(**options):
                ruling = ruled(**options)
                self.assertEqual(ruling.action, action)
                self.assertEqual(ruling.facts['activity'], NOW - options['age_days'] * day)     # the later of atime, mtime


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

    def test_a_live_owner_never_ripens_into_quarantine_elapsed(self):
        # Seen live from the first look, the observation carries no identity; a record of that None must not elapse.
        observed = dict(row=dict(ROW), quarantine=dict(key='/w/alpha', identity=None, excluded=[], live=True))
        previous = {'quarantine': {'/w/alpha': dict(identity=None, since=NOW - 4 * 86400)}}
        answer = disk_authority.decide(dict(apply=True, now=NOW), [observed], previous, NOW)
        self.assertEqual((answer['decisions'][0]['action'], answer['decisions'][0]['reason_code']), ('keep', 'live_owner'))
        self.assertEqual(answer['state']['quarantine'], {})
        self.assertEqual(answer['events'], [dict(kind='quarantine_reset', path='/w/alpha')])

    def unlisted(self, *, live=False, tag=None):
        reads = []
        evidence = SimpleNamespace(tag=lambda path: tag, task_facts=lambda path, tag: (live, False),
                                   declarations=lambda machine, path: reads.append(path) or ['bin'])
        return SimpleNamespace(request=stage(), evidence=evidence), reads

    def test_an_unlisted_folder_under_a_current_lease_is_live(self):
        for tag, live in ((dict(v=2, lease_until=NOW + 1), True), (dict(v=2, lease_until=NOW), False),
                          (dict(v=1, lease_until=NOW + 1), False), (None, False)):
            with self.subTest(tag=tag):
                run, _ = self.unlisted(tag=tag)
                self.assertIs(disk_authority.unlisted_live(run, Path('/w/stray')), live)

    def test_a_live_unlisted_folder_is_observed_without_identity_or_declarations(self):
        # Defence in depth beside decide's own live check: a live tree is never fingerprinted for grace.
        run, reads = self.unlisted(live=True)
        row = dict(ROW, path=str(Path('/w/stray').absolute()), unlisted=True, ignored=[])
        with patch.object(disk_authority.disk_evidence, 'identity', return_value='id'):
            observed = disk_authority.quarantine_observation(run, dict(repo='/r', name='alpha'), row)
        self.assertEqual({k: observed['quarantine'][k] for k in ('identity', 'excluded', 'live')},
                         dict(identity=None, excluded=[], live=True))
        self.assertEqual(reads, [], 'a live folder reads no declarations')

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
