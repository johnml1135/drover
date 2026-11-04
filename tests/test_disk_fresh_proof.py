"""A removal takes its row from the stage listing and one fresh proof, through `disk_side.serve` over a scene's tree."""
import subprocess
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from drover import disk_cleanup, disk_removal, disk_state, git_worktrees
from drover.disk_evidence import StageEvidence
from drover.disk_stage import StageClock
from drover.git_worktrees import Listings
from tests.disk_fixture import DAY, empty_result
from tests.disk_trace import Scene, lost_registration, missing, quarantine_unlisted_registered
from tests.disk_trace_scene import Failed, quick_state
from tests.disk_trace_stages import budget, output

ROOT = 'worktrees/alpha.worktrees'
TREE = f'<TMP>/{ROOT}/w'
BETA = '<TMP>/worktrees/beta.worktrees/b'
LIST = ('worktree', 'list')
TIMED_OUT = 'git worktree failed: timeout'       # a timed-out `worktree` step's error: stderr never shows


def row_at(trace, *paths):
    return next(row for row in trace['rows'] if row['path'] in (paths or (TREE, TREE + '/bin/x')))


def admin(scene, name='w', repo='alpha'):
    return scene.common(repo) / 'worktrees' / name


def stick(scene, *stuck):
    """Each git command starting with one of `stuck` times out."""
    refused = scene.check_refused

    def timing_out(where, command):
        if stuck and command.startswith(stuck):
            raise subprocess.TimeoutExpired(['git', command], 60)
        refused(where, command)
    scene.check_refused = timing_out


def steps(scene, **stage):
    """An applied trace, and in order each git command with its timeout and each journal phase appended."""
    events, run, append = [], scene.git.run, disk_state.append_manifest

    def spawn(argv, **options):
        events.append((' '.join(map(str, argv[3:])), options['timeout']))
        return run(argv, **options)

    def journal(state_dir, record):
        events.append(record['phase'])
        return append(state_dir, record)
    scene.git.run = spawn
    with patch.object(disk_state, 'append_manifest', journal):
        return scene.record(apply=True, **stage), events


def between(events):
    """The git steps after journal `planned` and before the next `completed` or `failed`."""
    start = events.index('planned')
    end = next(at for at in range(start, len(events)) if events[at] in ('completed', 'failed'))
    return events[start + 1:end]


def named(command):
    words = command.split()
    return ' '.join(words[:2]) if words[0] == 'worktree' else words[0]


def pausing(pauses):
    """The journal's retry pauses noted in `pauses`, not slept."""
    return patch.object(disk_removal, 'time', SimpleNamespace(monotonic=time.monotonic, sleep=pauses.append))


class Noted:
    """A fresh proof that notes in `order` when its nesting answer is read."""
    def __init__(self, proof, order):
        self.refused, self._proof, self._order = proof.refused, proof, order

    @property
    def overlaps(self):
        self._order.append('nesting')
        return self._proof.overlaps


class FreshProof(unittest.TestCase):
    def scene(self):
        scene = Scene()
        self.addCleanup(scene.doCleanups)
        return scene

    def beta(self, scene):
        scene.repo('beta')
        return scene.worktree('b', repo='beta')

    def assert_kept(self, trace, reason):
        row = row_at(trace)
        self.assertEqual((row['action'], row['reason_code'], trace['deleted'], trace['journal']), ('keep', reason, [], []))

    def stamped(self, scene):
        """A whole dry run with the scene answering its stages, and whether the run wrote its stamp."""
        def stage(side, request):
            if side != scene.side or request['stage'] == 1:
                return empty_result()
            return scene.serve(False, scene.now, None, 1800, {}, stage=request['stage'])
        lock, fsync = quick_state()         # as the scenes: no lock or fsync, real state files
        with patch.object(disk_cleanup, '_repos', return_value=scene.machines), patch.object(disk_cleanup, 'scope_key', return_value='scope'), \
                lock, fsync:
            scene.run_cleanup(apply=False, handler=stage)
        return 'report_scope' in disk_state.read(scene.state)

    def test_a_failed_listing_in_one_repo_keeps_every_removal_in_every_repo_and_writes_no_stamp(self):
        for stage in (4, 5):
            with self.subTest(stage=stage):
                scene = self.scene()
                scene.repo('beta')
                output(scene)
                output(scene, 'b', repo='beta')
                scene.refuse('worktree list', 'fatal: listing failed', scene.common().parent)
                trace = scene.record(apply=True, stage=stage)
                self.assertEqual((trace['deleted'], trace['journal']), ([], []))
                self.assertFalse([row for row in trace['rows'] if row['action'] in ('removed', 'candidate')])
                self.assertTrue(trace['errors'])
        self.assertFalse(self.stamped(scene))
        healthy = self.scene()
        output(healthy)
        self.assertTrue(self.stamped(healthy))

    def test_a_commit_after_the_listing_keeps_the_unit_whatever_the_first_pass_read(self):
        for stage, reason in ((4, 'identity_changed'), (5, 'output_evidence_unavailable')):
            with self.subTest(stage=stage):
                scene = self.scene()
                output(scene)
                scene.git.after_listing = lambda: scene.advance('w')
                self.assert_kept(scene.record(apply=True, stage=stage), reason)

    def test_a_stages_own_worktree_remove_does_not_refuse_its_next_removal(self):
        scene = self.scene()
        scene.worktree('w')
        scene.worktree('w2')
        trace = scene.record(apply=True)
        self.assertLessEqual({TREE, TREE + '2'}, set(trace['deleted']))
        self.assertEqual([row_at(trace, TREE)['action'], row_at(trace, TREE + '2')['action']], ['removed', 'removed'])

    def test_a_target_inside_another_repos_main_checkout_is_refused(self):
        scene = self.scene()
        scene.repo('beta')
        output(scene, at=scene.common('beta').parent / 'sub' / 'w')
        path = '<TMP>/repos/beta/sub/w'
        trace = scene.record(apply=True)
        self.assertEqual((row_at(trace, path)['reason_code'], trace['deleted']), ('nested_worktree', []))
        trace = scene.record(apply=True, stage=5)
        self.assertEqual((row_at(trace, path + '/bin/x')['reason_code'], trace['deleted']), ('protected', []))

    def test_an_unreadable_locked_never_reads_as_unlocked(self):
        for stage, name, repo in ((4, 'w', 'alpha'), (5, 'w', 'alpha'), (5, 'b', 'beta')):     # stage 5 has listed every repo by then
            with self.subTest(stage=stage, name=name):
                scene = self.scene()
                self.beta(scene)
                output(scene)
                locked = admin(scene, name, repo) / 'locked'
                scene.midstage = lambda request: locked.unlink() or locked.mkdir()
                self.assert_kept(scene.record(apply=True, stage=stage), 'registration_unknown')

    def test_each_refusal_scope_gives_its_row(self):
        moved = lambda scene: scene.move('w')
        other = lambda scene: scene.write(admin(scene, 'b', 'beta') / 'commondir', '../../..\n')
        for stage, change, reason, neighbour in ((4, moved, 'identity_changed', 'removed'),
                                                 (5, moved, 'output_evidence_unavailable', 'removed'),
                                                 (4, other, 'registration_unknown', 'keep'),
                                                 (5, other, 'registration_unknown', 'keep')):
            with self.subTest(stage=stage, reason=reason):
                scene = self.scene()
                scene.repo('beta')
                output(scene)
                output(scene, 'b', repo='beta')
                scene.write(scene.common('beta') / 'info/exclude', 'bin\n')
                scene.midstage = lambda request: change(scene)
                trace = scene.record(apply=True, stage=stage)
                self.assertEqual((row_at(trace)['action'], row_at(trace)['reason_code']), ('keep', reason))
                self.assertEqual(row_at(trace, BETA, BETA + '/bin/x')['action'], neighbour)

    def test_a_target_file_gone_after_the_evidence_is_unknown(self):
        for stage, last in ((4, 'merge-base'), (5, 'ls-files')):
            with self.subTest(stage=stage):
                scene = self.scene()
                output(scene)
                scene.midstage = lambda request: scene.git.after.update({last: (admin(scene) / 'HEAD').unlink})
                trace = scene.record(apply=True, stage=stage)
                self.assertEqual((row_at(trace)['reason_code'], trace['journal']), ('registration_unknown', []))

    def test_a_targets_pointer_change_reads_as_changed_though_every_removal_is_unknown(self):
        scene = self.scene()
        scene.worktree('w')
        scene.midstage = lambda request: scene.write(admin(scene) / 'gitdir', (scene.root / 'moved/.git').as_posix() + '\n')
        self.assert_kept(scene.record(apply=True), 'identity_changed')

    def test_a_read_past_its_timeout_keeps_the_removal_whatever_reach_holds(self):
        scene = self.scene()
        self.beta(scene)
        scene.worktree('w')
        real = git_worktrees._read_admin

        def read(common_dir, **options):
            if scene.common('beta').samefile(common_dir):
                raise TimeoutError('admin read past its timeout')
            return real(common_dir, **options)
        with patch.object(git_worktrees, '_read_admin', read):
            self.assert_kept(scene.record(apply=True), 'registration_unknown')

    def test_stage_4_measures_nesting_against_what_the_proof_read(self):
        """Stage 4's twin of test_disk_outputs' revalidation: a registration only the proof sees keeps the unit."""
        from drover import placement
        scene = self.scene()
        tree = scene.worktree('w')
        reach = (placement.cleanup_parts(tree / 'nested'),)
        with patch.object(git_worktrees, 'prove', return_value=git_worktrees.Proof((), reach)):
            trace = scene.record(apply=True)
        self.assert_kept(trace, 'nested_worktree')

    def test_a_registered_candidate_with_no_row_at_removal_keeps(self):
        """Section 7.3 step 1 at stage 4: the stage listing lost the target's row, so the removal keeps it."""
        scene = self.scene()
        tree = scene.worktree('w')
        real = disk_removal.remove_worktree

        def dropped(run, selected, **options):      # the scene wraps whatever `remove_worktree` is when it serves
            run.evidence.took(scene.machines[0]['repo'], tree)
            return real(run, selected, **options)
        with patch.object(disk_removal, 'remove_worktree', dropped), \
                patch.object(git_worktrees, 'prove', side_effect=AssertionError('proved a target with no row')):
            trace = scene.record(apply=True)
        self.assert_kept(trace, 'settled')
        self.assertEqual([(error['path'], error['action'], error['error']) for error in trace['errors']],
                         [(TREE, 'keep', 'registration changed')])

    def test_one_removal_lists_nothing_and_proves_once(self):
        for stage in (4, 5):
            with self.subTest(stage=stage):
                scene = self.scene()
                output(scene)
                seen = []
                scene.midstage = lambda request: seen.append(scene.git.count(*LIST))
                with patch.object(git_worktrees, 'prove', wraps=git_worktrees.prove) as proved:
                    trace = scene.record(apply=True, stage=stage)
                self.assertTrue(trace['deleted'])
                self.assertEqual(scene.git.count(*LIST) - seen[0], 0)       # at removal
                self.assertEqual(proved.call_count, 1)
                self.assertIs(proved.call_args.kwargs['pointer'], True)
                self.assertEqual(proved.call_args.kwargs['timeout_s'], git_worktrees.PROOF_TIMEOUT_S)

    def test_a_missing_candidate_and_a_stray_expect_no_pointer(self):
        scene = self.scene()
        missing(scene)
        with patch.object(git_worktrees, 'prove', wraps=git_worktrees.prove) as proved:
            self.assertEqual(row_at(scene.record(apply=True))['action'], 'removed')
        self.assertIs(proved.call_args.kwargs['pointer'], False)
        scene = self.scene()
        scene.folder('w')
        with patch.object(git_worktrees, 'prove', wraps=git_worktrees.prove) as proved:
            scene.record(apply=True)
            self.assertEqual(row_at(scene.record(apply=True, now=scene.now + 3 * DAY))['action'], 'removed')
        self.assertIs(proved.call_args.kwargs['pointer'], False)

    def test_a_stray_registered_between_listings_resets_its_quarantine(self):
        """Spec S5: the stage's listing missed it and the proof at removal sees it; the removal keeps, with today's
        `snapshot changed` error, and the stray's three days start again rather than standing elapsed."""
        scene = self.scene()
        answers = quarantine_unlisted_registered(scene)
        self.assertEqual([error['error'] for error in answers['elapsed']['errors']], ['snapshot changed'])
        self.assertEqual(disk_state.read(scene.state).get('quarantine', {}), {})

    def test_reregister_keeps_its_one_real_listing_and_tells_listings(self):
        scene = self.scene()
        lost_registration(scene)
        scene.worktree('w')
        with patch.object(Listings, 'repaired', autospec=True, side_effect=Listings.repaired) as told:
            trace = scene.record(apply=True)
        self.assertEqual(row_at(trace, f'<TMP>/{ROOT}/lost')['action'], 'reregistered')
        self.assertEqual(scene.git.count(*LIST), 2)     # the stage's, and the one that verifies the repair
        self.assertEqual(told.call_args.args[1:], (scene.machines[0]['repo'], scene.root / ROOT / 'lost'))
        self.assertEqual(row_at(trace)['action'], 'removed')    # the repair's own pointer refuses nothing

    def test_proof_rows_exist_only_in_applied_runs(self):
        scene = self.scene()
        scene.worktree('w')
        with patch.object(git_worktrees, 'prove', wraps=git_worktrees.prove) as proved:
            scene.git.after_listing = lambda: scene.move('w')
            self.assertEqual(row_at(scene.record(apply=False))['action'], 'candidate')
            proved.assert_not_called()
            scene.git.after_listing = lambda: scene.advance('moved')
            self.assertEqual(row_at(scene.record(apply=True))['reason_code'], 'identity_changed')
            proved.assert_called_once()

    def test_timed_out_worktree_remove_ends_failed_with_the_marker_restored(self):
        """Ruling 4: a git miss after `unlock_at`, a timeout included, relocks the worktree and journals `failed`."""
        scene = self.scene()
        scene.worktree('w')
        stick(scene, 'worktree remove')
        trace = scene.record(apply=True)
        self.assertEqual((row_at(trace)['action'], trace['deleted']), ('keep', []))
        self.assertEqual([(e['path'], e['action'], e['error']) for e in trace['errors']], [(TREE, 'keep', TIMED_OUT)])
        self.assertEqual([record['phase'] for record in trace['journal']], ['planned', 'failed'])
        self.assertEqual(scene.git.count('worktree', 'lock'), 1)
        self.assertTrue((admin(scene) / 'locked').exists())

    def test_timed_out_lock_at_on_the_restore_path_still_journals_failed_with_both_errors(self):
        """Section 7.3: a lock restore that fails does not skip `failed`, and the row carries both errors."""
        scene = self.scene()
        scene.worktree('w')
        stick(scene, 'worktree remove', 'worktree lock')
        trace = scene.record(apply=True)
        marker = '<TMP>/repos/alpha/.git/worktrees/w/locked'      # gone, as the relock did not take
        self.assertEqual((row_at(trace)['action'], trace['deleted']), ('keep', [marker]))
        self.assertEqual([(e['path'], e['action'], e['error']) for e in trace['errors']],
                         [(TREE, 'keep', f'{TIMED_OUT}; lock restore failed: {TIMED_OUT}')])
        self.assertEqual([record['phase'] for record in trace['journal']], ['planned', 'failed'])
        self.assertEqual(scene.git.count('worktree', 'lock'), 1)

    def test_every_step_between_planned_and_completed_takes_mutation_cap(self):
        """Section 6.1:643: rescue `update-ref`, its verifying `rev-parse`, unlock, `worktree remove` and the restore's lock."""
        for timeout, cap in ((1800, 60.0), (100, 4.0)):
            for stuck in ((), ('worktree remove',)):
                with self.subTest(timeout=timeout, stuck=stuck):
                    scene = self.scene()
                    scene.worktree('w', detached=True)
                    stick(scene, *stuck)
                    trace, events = steps(scene, timeout=timeout)
                    mutating = between(events)
                    self.assertEqual([named(command) for command, _ in mutating],
                                     ['update-ref', 'rev-parse', 'worktree unlock', 'worktree remove'] + ['worktree lock'] * len(stuck))
                    self.assertEqual({timeout for _, timeout in mutating}, {cap})
                    self.assertEqual(row_at(trace)['action'], 'keep' if stuck else 'removed')

    def test_tiny_mutation_cap_still_ends_in_a_failed_row(self):
        """Review Focus 4: worker_timeout_s=11 gives a cap of 0.44 s, and a removal that misses it still journals `failed`."""
        scene = self.scene()
        scene.worktree('w', detached=True)
        stick(scene, 'worktree remove')
        trace, events = steps(scene, timeout=11)
        self.assertEqual([record['phase'] for record in trace['journal']], ['planned', 'failed'])
        self.assertEqual((row_at(trace)['action'], trace['deleted']), ('keep', []))
        self.assertTrue((admin(scene) / 'locked').exists())
        for command, timeout in between(events):
            self.assertAlmostEqual(timeout, 0.44, msg=command)

    def test_failed_planned_append_stops_before_any_mutation(self):
        scene = self.scene()
        scene.worktree('w', detached=True)
        tried, append = [], disk_state.append_manifest

        def journal(state_dir, record):
            tried.append(record['phase'])
            if record['phase'] == 'planned':
                raise OSError('cannot sync journal')
            return append(state_dir, record)
        with patch.object(disk_state, 'append_manifest', journal):
            trace = scene.record(apply=True)
        self.assertEqual((tried, trace['journal'], trace['deleted']), (['planned'], [], []))     # no lone `failed` either
        self.assertEqual([(e['path'], e['action'], e['error']) for e in trace['errors']], [(TREE, 'keep', 'cannot sync journal')])
        for words in (('update-ref',), ('worktree', 'unlock'), ('worktree', 'remove'), ('worktree', 'lock')):
            self.assertEqual(scene.git.count(*words), 0, words)

    def test_journal_before_unlock_or_delete(self):
        scene = self.scene()
        scene.worktree('w', detached=True)
        trace, events = steps(scene)
        names = [event if isinstance(event, str) else named(event[0]) for event in events]
        mutations = [name for name in names if name in ('planned', 'update-ref', 'worktree unlock', 'worktree remove')]
        self.assertEqual(mutations, ['planned', 'update-ref', 'worktree unlock', 'worktree remove'])
        self.assertIn(TREE, trace['deleted'])

    def test_ordering_is_section_7_3(self):
        """Row, inspect, fresh_registrations, decide, nesting, ignored, can_mutate, planned: each where removal binds it."""
        scene = self.scene()
        scene.worktree('w')
        order = []

        def noted(name, real):
            return lambda *args, **options: (order.append(name), real(*args, **options))[1]
        fresh, can_mutate = StageEvidence.fresh_registrations, StageClock.can_mutate
        with patch.object(disk_removal, 'remove_worktree', noted('start', disk_removal.remove_worktree)), \
                patch.object(StageEvidence, 'row', noted('row', StageEvidence.row)), \
                patch.object(disk_removal, 'inspect', noted('inspect', disk_removal.inspect)), \
                patch.object(StageEvidence, 'fresh_registrations',
                             lambda *args, **options: Noted(noted('fresh_registrations', fresh)(*args, **options), order)), \
                patch.object(disk_removal, 'registration_identity', noted('decide', disk_removal.registration_identity)), \
                patch.object(disk_removal, 'ignored_outside', noted('ignored', disk_removal.ignored_outside)), \
                patch.object(StageClock, 'can_mutate', noted('can_mutate', can_mutate)), \
                patch.object(disk_state, 'append_manifest', lambda state_dir, record: order.append(record['phase'])):
            self.assertEqual(row_at(scene.record(apply=True))['action'], 'removed')
        seen = order[order.index('start') + 1:]
        self.assertEqual(sorted(set(seen), key=seen.index),
                         ['row', 'inspect', 'fresh_registrations', 'decide', 'nesting', 'ignored', 'can_mutate', 'planned', 'completed'])

    def test_one_fresh_proof_per_removal_and_one_new_process_proof(self):
        scene = self.scene()
        scene.worktree('w')
        scene.worktree('w2')
        with patch.object(git_worktrees, 'prove', wraps=git_worktrees.prove) as proved:
            trace = scene.record(apply=True)
        self.assertEqual([row_at(trace, TREE)['action'], row_at(trace, TREE + '2')['action']], ['removed', 'removed'])
        self.assertEqual(proved.call_count, 2)
        self.assertEqual(scene.proofs, 3)       # the stage's first-pass proof, then one new one per removal

    def test_unlisted_target_has_no_row_and_no_tip(self):
        """Section 7.3: a stray has no listing row, so its proof reads no tip; `pointer` is False."""
        scene = self.scene()
        scene.folder('w')
        scene.record(apply=True)
        rows, proofs = [], []
        real, prove = StageEvidence.row, git_worktrees.prove

        def row(evidence, repo, path):
            rows.append(real(evidence, repo, path))
            return rows[-1]

        def proving(*args, **options):
            before = len(scene.git.asked)
            proofs.append((options['pointer'], prove(*args, **options)))
            proofs[-1] += (scene.git.asked[before:],)
            return proofs[-1][1]
        with patch.object(StageEvidence, 'row', row), patch.object(git_worktrees, 'prove', proving):
            trace = scene.record(apply=True, now=scene.now + 3 * DAY)
        self.assertEqual(row_at(trace)['action'], 'removed')
        self.assertEqual(rows, [None])
        tips = [[args for args in spawned if args[:2] == ['rev-parse', '--verify']] for _, _, spawned in proofs]
        self.assertEqual(([pointer for pointer, _, _ in proofs], tips), ([False], [[]]))      # no tip `rev-parse`

    def test_a_completed_append_that_fails_once_is_retried(self):
        """A `completed` or `failed` append takes a few short tries within the cap: one miss must not leave `planned`
        alone after a real removal."""
        for phase, stuck in (('completed', ()), ('failed', ('worktree remove',))):
            with self.subTest(phase=phase):
                scene = self.scene()
                scene.worktree('w')
                stick(scene, *stuck)
                tried, append = [], disk_state.append_manifest

                def journal(state_dir, record, phase=phase, tried=tried, append=append):
                    tried.append(record['phase'])
                    if record['phase'] == phase and tried.count(phase) == 1:
                        raise OSError('journal busy')
                    return append(state_dir, record)
                pauses = []
                with patch.object(disk_state, 'append_manifest', journal), pausing(pauses):
                    trace = scene.record(apply=True)
                self.assertEqual((tried, pauses), (['planned', phase, phase], [disk_removal.JOURNAL_PAUSES_S[0]]))
                self.assertEqual([record['phase'] for record in trace['journal']], ['planned', phase])
                self.assertEqual(row_at(trace)['action'], 'removed' if not stuck else 'keep')
                self.assertEqual([e['error'] for e in trace['errors']], [TIMED_OUT] if stuck else [])

    def test_a_journal_that_stays_unwritable_is_reported_not_swallowed(self):
        for phase, stuck, action, first in (('completed', (), 'removed', None), ('failed', ('worktree remove',), 'keep', TIMED_OUT)):
            with self.subTest(phase=phase):
                scene = self.scene()
                scene.worktree('w')
                stick(scene, *stuck)
                append = disk_state.append_manifest

                def journal(state_dir, record, phase=phase, append=append):
                    if record['phase'] == phase:
                        raise OSError('journal gone')
                    return append(state_dir, record)
                pauses = []
                with patch.object(disk_state, 'append_manifest', journal), pausing(pauses):
                    trace = scene.record(apply=True)
                self.assertEqual(pauses, list(disk_removal.JOURNAL_PAUSES_S))       # three tries in all, then it reports
                self.assertEqual([record['phase'] for record in trace['journal']], ['planned'])
                self.assertEqual(row_at(trace)['action'], action)
                message = f'journal {phase} failed: journal gone'
                self.assertEqual([e['error'] for e in trace['errors']], ['; '.join(filter(None, (first, message)))])

    def test_a_repo_that_fails_once_stays_failed_so_a_purge_spares_the_other_repos_unit(self):
        scene = self.scene()
        scene.repo('beta')
        scene.used_gb = [5.]
        output(scene, 'old')
        scene.file(output(scene, 'new', repo='beta') / 'bin/x', age=3 * DAY)
        failed, refused = [], scene.check_refused

        def once(where, command):
            if command.startswith('worktree list') and where == scene.common().parent and not failed:
                failed.append(where)
                raise Failed(128, 'fatal: listing failed')
            refused(where, command)
        scene.check_refused = once
        trace = scene.record(apply=True, stage=5, budget=budget())
        self.assertEqual((trace['deleted'], trace['journal']), ([], []))
        self.assertEqual(scene.git.count(*LIST), 2)     # each repo once: the failure is kept, not retried


if __name__ == '__main__':
    unittest.main()
