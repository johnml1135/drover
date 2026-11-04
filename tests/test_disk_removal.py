"""Removal, revalidation and usable restoration records at the public boundary."""
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from drover import disk_authority, disk_removal, placement, process
from drover.disk_authority import DisposableUnit, Ruling
from drover.disk_evidence import FreshProcesses, StageEvidence
from drover.disk_stage import StageAbandoned, StageClock, StageRequest
from drover.git_worktrees import Proof, Refusal
from tests.fakeci import git_spy
from tests.gitfix import sh
from tests.test_disk_worktrees import WorktreeCase
from tests.disk_fixture import MemoryCleanupCase, listing_answers
from tests.tiers import integration


class Removal(MemoryCleanupCase):
    def test_missing_folder_entry_by_path_other_side_survives(self):
        self.evidence.add('native', checks=dict(missing=True))
        self.evidence.add('foreign', checks=dict(other_side=True, missing=True))['row']['side_lock'] = 'foreign'
        self.cleanup()
        self.assertEqual(self.evidence.operations, [])
        self.cleanup(apply=True)
        self.assertFalse(self.kept('native'))
        self.assertTrue(self.kept('foreign'))
        self.assertEqual(self.evidence.operations, [('remove', 'native')])
        self.assertEqual(self.evidence.paths['foreign']['row']['side_lock'], 'foreign')

    def test_ignored_content_outside_declared_outputs_keeps_worktree(self):
        for name, files, removable in (
            ('environment', ['.venv/keep'], False), ('undeclared', ['data/keep'], False),
            ('mixed', ['generated/disposable', 'data/keep'], False),
            ('safe', ['generated/disposable'], True), ('nested-env', ['generated/.venv/keep'], False)):
            with self.subTest(case=name):
                observation = self.evidence.add(name)
                path = Path(name).absolute()
                observation['row']['ignored'] = [str(path / p) for p in files]
                from drover.disk_authority import ignored_outside
                observation['excluded'] = ignored_outside(
                    path, observation['row']['ignored'], ['generated'])
                result = self.cleanup(apply=True)
                self.assertEqual(self.kept(name), not removable, result)
                if not removable:
                    self.assertEqual(next(r for r in result['items'] if r['path'] == name)['reason_code'], 'ignored_content')
                    self.assertEqual(observation['row']['ignored'], [str(path / p) for p in files])
        approved = self.evidence.add('approved')
        approved_path = Path('approved').absolute()
        approved['row']['ignored'] = [str(approved_path / '.venv/keep')]
        approved['excluded'] = ignored_outside(
            approved_path, approved['row']['ignored'], ['generated'])
        approved['settlement']['merged'] = False
        result = self.cleanup(apply=True, approved_path='approved')
        self.assertTrue(self.kept('approved'), result)
        self.assertIn('approved', [r['path'] for r in result['items']])

    def test_dry_run_then_apply_selected_path_never_prunes(self):
        self.evidence.add('selected')
        self.evidence.add('dirty')['row']['dirty'] = ['dirty/scratch.txt']
        self.evidence.add('unmerged')['settlement']['merged'] = False
        self.evidence.add('ghost', checks=dict(missing=True, other_side=True))
        before = self.evidence.observe(dict(now=self.now, tasks=self.tasks, apply=False))
        self.cleanup()
        self.assertEqual(self.evidence.observe(dict(now=self.now, tasks=self.tasks, apply=False)), before)
        self.assertEqual(self.store.state, {})
        self.cleanup(apply=True)
        self.assertFalse(self.kept('selected'))
        for path in ('dirty', 'unmerged', 'ghost'):
            self.assertTrue(self.kept(path))
        self.assertEqual(self.evidence.operations, [('remove', 'selected')])
        self.assertEqual(self.evidence.paths['dirty']['row']['dirty'], ['dirty/scratch.txt'])

    def test_missing_registration_of_live_process_is_kept(self):
        self.evidence.add('busy', checks=dict(missing=True, busy=True))
        self.cleanup()
        result = self.cleanup(apply=True)
        self.assertTrue(self.kept('busy'))
        self.assertEqual(result['items'][0]['reason_code'], 'busy')
        self.assertEqual(self.evidence.operations, [])

    def test_nested_registered_worktree_survives_outer_settlement(self):
        from drover import disk_evidence
        outer_path, inner_path = 'outer', 'outer/generated/foreign-inner'
        outer = self.evidence.add(outer_path)
        inner = self.evidence.add(inner_path, checks=dict(other_side=True))
        inner['row']['side_lock'] = 'foreign'
        inner['payload'] = {'keep': 'foreign owner data'}
        def registrations(repo):
            return [dict(worktree=o['row']['path'], locked=o['row']['side_lock'])
                    for o in self.evidence.observe(dict(now=self.now, tasks=self.tasks))]
        before = registrations('repo')
        reach = [placement.cleanup_parts(row['worktree']) for row in before]
        with listing_answers(registrations):
            # The real detector sees the nested registration even without a gitfile.
            outer['nested'] = disk_evidence.overlaps(outer_path, outer_path, reach)
            self.assertTrue(outer['nested'])
            self.assertFalse(disk_evidence.overlaps('unrelated', 'unrelated', reach))
            self.cleanup()
            self.cleanup(apply=True)
        self.assertTrue(self.kept(outer_path))
        self.assertTrue(self.kept(inner_path))
        self.assertEqual(inner['payload']['keep'], 'foreign owner data')
        self.assertEqual(registrations('repo'), before)
        self.assertIn(dict(worktree=inner_path, locked='foreign'), registrations('repo'))
        self.assertEqual(self.evidence.operations, [])

    def test_owner_approval_selects_only_named_path_and_keeps_safety_checks(self):
        for path in ('approved', 'unselected', 'dirty'):
            self.evidence.add(path)['settlement']['merged'] = False
        self.evidence.paths['dirty']['row']['dirty'] = ['precious']
        self.cleanup(apply=True, approved_path='dirty')
        self.assertTrue(self.kept('dirty'))
        self.cleanup(apply=True, approved_path='approved')
        self.assertFalse(self.kept('approved'))
        self.assertTrue(self.kept('unselected'))

    def test_decide_is_pure_and_journal_failure_retains_candidate(self):
        import copy
        from drover.disk_authority import decide
        self.evidence.add('orphan')['row']['tag'] = None
        request = dict(apply=True, now=self.now, tasks=self.tasks)
        observations = self.evidence.observe(request)
        state = {'quarantine': {}}
        before = copy.deepcopy((request, observations, state))
        with mock.patch.object(placement, 'cleanup_parts', side_effect=AssertionError('decision probed paths')) as _placement_patch_1, mock.patch('drover.placement_paths.cleanup_parts', _placement_patch_1):
            answer = decide(request, observations, state, self.now)
        self.assertEqual((request, observations, state), before)
        self.assertEqual(answer['events'][0]['kind'], 'quarantine_started')
        self.evidence.add('settled')
        self.store.fail_journal = True
        self.cleanup(apply=True)
        self.assertTrue(self.kept('settled'))
        self.assertEqual(self.evidence.operations, [])

    def test_reports_preserve_grace_and_activity_starts_a_full_new_grace(self):
        observation = self.evidence.add('orphan')
        observation['row']['tag'] = None
        self.cleanup(apply=True)
        since = self.store.state['quarantine']['orphan']['since']
        self.now += 2 * 86400
        self.cleanup()
        self.assertEqual(self.store.state['quarantine']['orphan']['since'], since)
        observation['identity'] = 'new activity'
        self.cleanup(apply=True)
        self.now += 86400
        self.cleanup(apply=True)
        self.assertTrue(self.kept('orphan'))
        self.now += 2 * 86400
        self.cleanup(apply=True)
        self.assertFalse(self.kept('orphan'))


@integration
class RemovalContracts(WorktreeCase):
    def cleanup(self, *, apply=False, mode='on-demand'):
        # Exercise the owning-side adapter, not twelve unrelated coordinator stages.
        from types import SimpleNamespace
        from tests.disk_fixture import served
        request = dict(stage=4, mode=mode, apply=apply, now=self.now,
                       drive=dict(name='test-drive', path=str(self.root), level='queue', warn_gb=150),
                       repos=[dict(name='alpha', repo=str(self.r.repo), worktrees=str(self.trees),
                                   remote='origin', default_branch='main')],
                       paths=self.cfg['disk_cleanup'], state_dir=str(self.state), tasks=self.tasks)
        with mock.patch.object(placement, 'cleanup_processes', return_value=SimpleNamespace(busy=self.busy, reason=getattr(self, 'process_reason', None))), \
             mock.patch.object(placement, 'physical_disk_path', return_value=str(self.root)):
            return served(request)

    def test_missing_folder_entry_by_path_other_side_survives(self):
        native = self.worktree('missing native')
        foreign = self.worktree('missing other', lock=self.other_side)
        shutil.rmtree(native)
        shutil.rmtree(foreign)
        branches = self.branches()
        self.cleanup()
        calls = []
        with git_spy(lambda repo, *args: calls.append(args)):
            self.cleanup(apply=True)
        listed = self.registrations()
        self.assertNotIn(str(native), listed)
        self.assertIn(str(foreign), listed)
        self.assertIn('locked ' + self.other_side, listed)
        self.assertEqual(self.branches(), branches)
        self.assertFalse(any('prune' in call for call in calls), calls)
        self.assertTrue(any('remove' in call and str(native) in tuple(map(str, call))
                            for call in calls), calls)
        self.check_git_syntax_and_uncertain_repository_trees_survive()

    def test_revalidate_dirty_head_lock_and_manifest(self):
        # Mutate after scanning on the final status query, before removal.
        for change in ('dirty', 'head', 'lock', 'status-error', 'ignored'):
            with self.subTest(change=change):
                wt = self.worktree('race-' + change)
                self.r.commit('done-' + change + '.txt', cwd=wt)
                self.merge(wt)
                self.cleanup()
                status_calls = []
                def git(repo, *args, wt=wt, change=change):
                    if Path(repo) == Path(wt) and 'status' in args:
                        status_calls.append(args)
                        if len(status_calls) == 2:
                            if change == 'dirty':
                                (wt / 'new data.txt').write_text('keep')
                            elif change == 'ignored':
                                (wt / 'data').mkdir(exist_ok=True)
                                (wt / 'data' / 'new').write_text('keep')
                            elif change == 'head':
                                self.r.commit('later.txt', cwd=wt)
                            elif change == 'lock':
                                self.r.git('worktree', 'unlock', wt)
                                self.r.git('worktree', 'lock', '--reason', self.other_side, wt)
                            elif change == 'status-error':
                                return process.Result('exited', 128, b'', b'unavailable', 0.0, '')
                    return None
                with git_spy(git):
                    result = self.cleanup(apply=True)
                self.assertGreaterEqual(len(status_calls), 2, 'must revalidate full status')
                self.assertTrue(wt.exists(), change)
                self.assertIn(str(wt), self.registrations())
                self.reported(result, wt)
                journal = self.state / 'disk-cleanup-manifest.jsonl'
                if journal.exists():
                    self.assertFalse(any(r['phase'] == 'completed' and r['path'] == str(wt)
                                         for r in self.journal()))

        wt = self.worktree('fresh unknown process')
        checks = []
        def unknown_after_selection(path):
            if path == str(wt):
                checks.append(path)
                return len(checks) >= 2
            return False
        self.busy = unknown_after_selection
        self.process_reason = 'process_visibility_unavailable'
        result = self.cleanup(apply=True)
        self.assertGreaterEqual(len(checks), 2, 'fresh process visibility must be required')
        self.assertTrue(wt.exists(), result)
        self.busy = lambda path: False
        self.process_reason = None
        self.check_owner_appearing_during_removal_keeps_unregistered_directory()

    def test_manifest_restores_branch_and_detached_head(self):
        from drover import ownership
        for detached in (False, True):
            with self.subTest(detached=detached):
                name = "restore 'quote' " if os.name == 'nt' else 'restore "quote"\n'
                wt = self.worktree(name + str(detached), detached=detached)
                sha = self.r.commit('payload.txt', 'precious\n', cwd=wt)
                # Detached unreferenced commits need terminal task proof, then a rescue ref.
                task = 'restore-' + str(detached)
                from drover.ledger import states
                ownership.tag(str(wt), 'alpha', 'drover:' + task, self.now + 86400, now=self.now)
                self.tasks = {'at': self.now, 'tasks': [{'id': task, 'state': states.LANDED, 'terminal': True, 'live': False,
                              'worktree': str(wt), 'owner': 'drover:' + task}], 'active_worktrees': []}
                branches = self.branches()
                self.cleanup()
                self.cleanup(apply=True)
                self.assertFalse(wt.exists())
                entries = [r for r in self.journal() if r['path'] == str(wt)]
                self.assertEqual([r['phase'] for r in entries], ['planned', 'completed'])
                record = entries[-1]
                self.assertEqual(record['head'], sha)
                self.assertEqual(record['side_lock'], self.side)
                self.assertIsInstance(record['restore_argv'], list)
                self.assertIn(str(wt), record['restore_argv'])
                if detached:
                    self.assertTrue(record['rescue_ref'])
                    self.assertEqual(self.r.git('rev-parse', record['rescue_ref']), sha)
                self.assertEqual(self.branches(), branches)
                sh(*record['restore_argv'])
                if record['lock_argv']:
                    sh(*record['lock_argv'])
                self.assertEqual(self.r.git('rev-parse', 'HEAD', cwd=wt), sha)
                self.assertEqual((wt / 'payload.txt').read_text(), 'precious\n')
                self.assertIn('locked ' + self.side, self.registrations())

    def test_journal_failure_prevents_unlock_and_delete(self):
        from drover import disk_state
        wt = self.worktree('journal-failure')
        from drover import disk_cleanup
        denied = disk_cleanup.remove_approved(self.cfg, str(self.r.repo), str(wt), runtime=self.runtime(real=True))
        self.assertFalse(denied['ok'])
        self.assertTrue(wt.exists(), 'owner deletion requires a matching report scope')
        self.cleanup()
        calls = []
        with mock.patch.object(disk_state, 'append_manifest', side_effect=OSError('cannot sync journal')), \
             git_spy(lambda repo, *args: calls.append(args)):
            result = self.cleanup(apply=True)
        self.assertTrue(wt.exists(), result)
        self.assertIn('locked ' + self.side, self.registrations())
        self.assertFalse(any('unlock' in c or 'remove' in c for c in calls), calls)

    def check_git_syntax_and_uncertain_repository_trees_survive(self):
        from tests import test_disk_fix2 as fix2
        configs = ('[core] bare=true\n', '\ufeff[core]\n bare = true\n',
                   '[CoRe] BaRe = "true" ; comment\n', '[core\n bare=true\n',
                   '[core]\n bare = "unterminated\n')
        for stage in (2, 3):
            for index, config in enumerate(configs):
                with self.subTest(stage=stage, config=config):
                    root = self.temp_root / f'git-{stage}-{index}'
                    self.file(root / 'config', config)
                    valuable = self.file(root / 'unpublished')
                    self.age(root, 40 * 86400)
                    self.cfg['disk_cleanup']['caches'] = [dict(path=str(root), placement=self.side)]
                    result = fix2.PathSafety.execute(self, fix2.PathSafety.request(self, stage))
                    self.assertTrue(valuable.exists(), result)
            for marker in ('HEAD', 'objects', 'refs'):
                with self.subTest(stage=stage, marker=marker):
                    root = self.temp_root / f'partial-{stage}-{marker}'
                    self.file(root / marker, 'incomplete repository')
                    valuable = self.file(root / 'unpublished')
                    self.age(root, 40 * 86400)
                    self.cfg['disk_cleanup']['caches'] = [dict(path=str(root), placement=self.side)]
                    result = fix2.PathSafety.execute(self, fix2.PathSafety.request(self, stage))
                    self.assertTrue(valuable.exists(), result)

    def check_owner_appearing_during_removal_keeps_unregistered_directory(self):
        import shutil
        from types import SimpleNamespace
        from drover import placement
        path = self.trees / 'late owner'
        self.trees.mkdir(exist_ok=True)
        shutil.copytree(self.r.repo, path, ignore=shutil.ignore_patterns('.git'))
        self.cleanup()
        self.cleanup(apply=True)
        self.now += 3 * 86400
        self.tasks = dict(at=self.now, active_worktrees=[], tasks=[])
        checks = []
        live = []
        metadata = placement.worktree_metadata
        def read_owner(worktree, name, *args):
            if str(worktree) == str(path) and live:
                import json
                return json.dumps(dict(v=2, repo='alpha', owner='session:late',
                    created=self.now, lease_until=self.now + 86400))
            return metadata(worktree, name, *args)
        def busy(candidate):
            if candidate == str(path):
                checks.append(candidate)
                if len(checks) == 2:
                    live.append(True)
            return False
        # run_cleanup installs the process adapter; busy is its declared test hook.
        self.busy = busy
        with mock.patch.object(placement, 'worktree_metadata', side_effect=read_owner):
            self.cleanup(apply=True)
            self.assertGreaterEqual(len(checks), 2)
            self.assertTrue(path.exists(), 'live ownership must be checked again before removal')
            live.clear()
            self.busy = lambda candidate: False
            self.cleanup(apply=True)
            self.assertTrue(path.exists(), 'a newly observed owner cancels elapsed quarantine')
            self.now += 3 * 86400
            self.cleanup(apply=True)
            self.assertFalse(path.exists(), 'released owner gets a fresh full grace period')


class RegisteredListing(MemoryCleanupCase):
    def test_overlaps_measures_its_reach_and_never_lists(self):
        from drover import disk_evidence
        rows = ('/w/outer', '/w/outer/generated/inner', '/w/solo')
        reach = [placement.cleanup_parts(path) for path in rows]
        with listing_answers([]) as fresh:
            for target, own, expected in (('/w/outer/generated', '/w/outer', True), ('/w/outer', '/w/outer', True),
                                          ('/w/solo/bin', '/w/solo', False), ('/w/free/bin', '/w/free', False), ('/w', '/w/solo', True),
                                          ('/w/outer/generated/inner/x', '/w/outer', True)):
                with self.subTest(target=target):
                    self.assertEqual(disk_evidence.overlaps(target, own, reach), expected)
        fresh.assert_not_called()


def abandoned(*_, **__):
    raise StageAbandoned('the stage lost its reader')


class Disposable(unittest.TestCase):
    """The remove_* family on fakes: a temp worktree `w` whose declared `bin` holds the unit's three paths. This test is
    the stage's evidence, each read noted in `order`; the fresh proofs are real StageEvidence ones; every run method
    that could checkpoint raises StageAbandoned."""
    SIZES = (('a', 3), ('b', 5), ('c', 7))

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.tree = Path(folder.name).absolute() / 'w'
        (self.tree / 'bin').mkdir(parents=True)
        (self.tree / '.git').write_text('gitdir: x', encoding='utf-8')
        self.names = []
        for name, size in self.SIZES:
            (self.tree / 'bin' / name).write_bytes(b'x' * size)
            self.names.append(str(self.tree / 'bin' / name))
        self.measured = {name: dict(files=1, bytes=size, age_s=10 ** 6) for name, (_, size) in zip(self.names, self.SIZES)}
        self.order, self.pointers, self.mutate = [], [], True
        self.entry = dict(worktree=str(self.tree), HEAD='head', branch='refs/heads/w')
        request = StageRequest(stage=5, side='windows', mode=None, apply=True, now=0, drive=dict(path=folder.name),
                               repos=(dict(name='alpha', repo='/repo'),), policy={}, state_dir='/state', tasks={})
        self.real = StageEvidence(request, StageClock(1800), prove=self.prove,
                                  processes=lambda **_: SimpleNamespace(busy=lambda path: False, reason=None))
        self.run = SimpleNamespace(request=request, evidence=self, clock=SimpleNamespace(can_mutate=self.can_mutate),
                                   reclaimed=lambda size: self.order.append(('reclaimed', size)),
                                   checkpoint=abandoned, add=abandoned, stop=abandoned, error=abandoned)

    # The stage evidence remove_disposable reads.
    def row(self, repo, path):
        self.order.append('row')
        return dict(self.entry)

    def status(self, path):
        self.order.append('status')
        return [], list(self.names)

    def fresh_processes(self):
        self.order.append('fresh_processes')
        return self.real.fresh_processes()

    def tracked(self, path, relative=None):
        self.order.append('tracked')
        return []

    def measure(self, path):
        self.order.append('measure')
        return self.measured[str(path)]

    def fresh_registrations(self, repo, target, own, *, pointer):
        self.order.append('fresh_registrations')
        return self.real.fresh_registrations(repo, target, own, pointer=pointer)

    def fresh_snapshot(self):
        return None

    def prove(self, listings, repo, own, *, pointer, timeout_s):
        """As git_worktrees.prove reads `.git`: present where `pointer` says it should be, or the proof refuses."""
        self.pointers.append(pointer)
        present = (Path(own) / '.git').exists()
        return Proof((Refusal(repo, own, 'changed', '.git'),) if present is not pointer else (), ())

    def can_mutate(self):
        self.order.append('can_mutate')
        return self.mutate

    def remove(self):
        row = dict(path=str(self.tree / 'bin'), action='candidate', reason_code='declared_disposable')
        unit = DisposableUnit(machine=dict(name='alpha', repo='/repo'), registration=dict(self.entry),
                              evidence=(self.tree, 'head', None, ('bin',)), touched=set(), target=self.tree / 'bin',
                              measured=self.measured, row=row)

        def first(run, machine, registration, processes, *, touched=()):
            self.order.append('rule_disposable_fresh')
            self.assertIsInstance(processes, FreshProcesses)
            return Ruling('candidate', 'declared_disposable',
                          facts=dict(path=self.tree, head='head', tag=None, declarations=('bin',)))

        def last(run, unit, proof):
            self.order.append('rule_unit_fresh')
            return disk_authority.rule_unit_fresh(run, unit, proof)
        with mock.patch.object(disk_removal, 'rule_disposable_fresh', first), \
                mock.patch.object(disk_removal, 'rule_unit_fresh', last), \
                mock.patch.object(placement, 'cleanup_contains', return_value=True):
            disk_removal.remove_disposable(self.run, unit)
        return row

    def left(self):
        return [name for name in self.names if Path(name).exists()]

    def test_disposable_ordering_is_section_7_3(self):
        """Listing row, status; fresh processes and the rule, tracked, compare; fresh registrations and the unit's last
        rule; then each path, its bytes counted as it goes."""
        row = self.remove()
        self.assertEqual(self.order, ['row', 'status', 'fresh_processes', 'rule_disposable_fresh', 'tracked',
                                      'measure', 'measure', 'measure', 'fresh_registrations', 'rule_unit_fresh',
                                      ('reclaimed', 3), ('reclaimed', 5), ('reclaimed', 7)])
        self.assertEqual((row['action'], self.left(), (self.tree / 'bin').exists()), ('removed', [], False))
        self.assertEqual(self.pointers, [True])

    def test_bytes_count_as_each_path_goes_on_a_removal_cut_short(self):
        middle = Path(self.names[1])
        middle.unlink()
        (middle / 'inner').mkdir(parents=True)      # rmdir refuses a folder that is not empty: the second path fails
        row = self.remove()
        self.assertEqual([step for step in self.order if isinstance(step, tuple)], [('reclaimed', 3)])
        self.assertEqual(self.left(), self.names[1:])
        self.assertEqual((row['action'], row['reason_code']), ('keep', 'output_evidence_unavailable'))
        self.assertTrue(row['error'])

    def test_a_disposable_with_no_dot_git_is_refused(self):
        (self.tree / '.git').unlink()
        row = self.remove()
        self.assertEqual(self.pointers, [True])
        self.assertEqual((row['action'], row['reason_code'], row['error']),
                         ('keep', 'output_evidence_unavailable', 'disposable registration changed (.git)'))
        self.assertNotIn('can_mutate', self.order)
        self.assertEqual(self.left(), self.names)

    def test_stage_abandoned_never_raised_inside_remove(self):
        """Each remove_* ends in its row, never a checkpoint: removed, cut short, refused or past the clock."""
        outcomes = [self.remove()['action']]
        (self.tree / 'bin').mkdir()
        Path(self.names[1]).mkdir()     # not empty below: the second path fails
        (Path(self.names[1]) / 'inner').mkdir()
        for name, (_, size) in zip(self.names[::2], self.SIZES[::2]):
            Path(name).write_bytes(b'x' * size)
        outcomes.append(self.remove()['reason_code'])
        (self.tree / '.git').unlink()
        outcomes.append(self.remove()['action'])
        leaf = self.tree / 'leaf'
        leaf.write_bytes(b'xx')
        for remove, args in ((disk_removal.remove_temp_entry, (leaf,)), (disk_removal.remove_cache_file, (leaf, leaf.stat()))):
            self.mutate = False
            row = dict(path=str(leaf), action='candidate', reason_code='stale_temp', bytes=2)
            with mock.patch.object(placement, 'cleanup_contains', return_value=True):
                remove(self.run, row, *args)
            outcomes.append(row['reason_code'])
        self.assertEqual(outcomes, ['removed', 'output_evidence_unavailable', 'keep', 'removal_deadline', 'removal_deadline'])

    def test_a_temp_entry_goes_whole_and_counts_its_row(self):
        for path in (Path(self.names[0]), self.tree / 'bin'):
            with self.subTest(path=path.name):
                self.order.clear()
                row = dict(path=str(path), action='candidate', reason_code='stale_temp', bytes=15)
                disk_removal.remove_temp_entry(self.run, row, path)
                self.assertEqual((row['action'], path.exists(), self.order), ('removed', False, ['can_mutate', ('reclaimed', 15)]))
        self.mutate, row = False, dict(path=str(self.tree), action='candidate', reason_code='build_root_idle', bytes=1)
        disk_removal.remove_temp_entry(self.run, row, self.tree)
        self.assertEqual((row['action'], row['reason_code'], self.tree.exists()), ('keep', 'removal_deadline', True))

    def test_a_cache_file_goes_only_as_it_was_measured(self):
        leaf = Path(self.names[0])
        info = leaf.stat()
        row = dict(path=str(leaf), action='candidate', reason_code='folder_cache')
        leaf.write_bytes(b'longer now')
        with mock.patch.object(placement, 'cleanup_contains', return_value=True):
            disk_removal.remove_cache_file(self.run, row, leaf, info)
            self.assertEqual((row['action'], leaf.exists()), ('candidate', True))
            info = leaf.stat()
            disk_removal.remove_cache_file(self.run, row, leaf, info)
        self.assertEqual((row['action'], leaf.exists(), self.order[-1]), ('removed', False, ('reclaimed', info.st_size)))

    def test_a_journal_retry_window_ends_with_the_stage(self):
        """The `completed` or `failed` retries stay inside both the mutation cap and what is left of the stage."""
        for cap, left, pauses_taken in ((60.0, 0.1, disk_removal.JOURNAL_PAUSES_S[:1]), (0.1, 60.0, disk_removal.JOURNAL_PAUSES_S[:1]),
                                        (60.0, 60.0, disk_removal.JOURNAL_PAUSES_S)):
            with self.subTest(cap=cap, left=left):
                tried, pauses, row = [], [], {}

                def append(record, tried=tried):
                    tried.append(record['phase'])
                    raise OSError('journal gone')
                clock = SimpleNamespace(mutation_cap=lambda cap=cap: cap, left=lambda left=left: left)
                with mock.patch.object(disk_removal, 'time', SimpleNamespace(monotonic=time.monotonic, sleep=pauses.append)):
                    note = disk_removal.close_journal(SimpleNamespace(append=append), dict(phase='planned'), 'completed',
                                                      clock, row)
                self.assertEqual((pauses, len(tried)), (list(pauses_taken), len(pauses_taken) + 1))
                self.assertEqual((row['error'], note), ('journal completed failed: journal gone',) * 2)
