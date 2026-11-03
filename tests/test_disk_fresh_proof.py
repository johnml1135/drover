"""A removal takes its row from the stage listing and one fresh proof, through `disk_side.serve` over a scene's tree."""
import subprocess
import unittest
from unittest.mock import patch

from drover import disk_cleanup, disk_state, git_worktrees
from drover.git_worktrees import Listings
from tests.disk_fixture import DAY, empty_result
from tests.disk_trace import Scene, lost_registration, missing
from tests.disk_trace_scene import Failed, quick_state
from tests.disk_trace_stages import budget, output

ROOT = 'worktrees/alpha.worktrees'
TREE = f'<TMP>/{ROOT}/w'
BETA = '<TMP>/worktrees/beta.worktrees/b'
LIST = ('worktree', 'list')


def row_at(trace, *paths):
    return next(row for row in trace['rows'] if row['path'] in (paths or (TREE, TREE + '/bin/x')))


def admin(scene, name='w', repo='alpha'):
    return scene.common(repo) / 'worktrees' / name


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
        scene.midstage = lambda request: request['_listings'].removed(scene.machines[0]['repo'], tree)
        with patch.object(git_worktrees, 'prove', side_effect=AssertionError('proved a target with no row')):
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

    def test_git_failure_after_unlock_restores_the_marker_and_journals_failed(self):
        """disk_worktrees.py's removal handler (ruling 12): a git miss after `unlock_at`, a timeout included, relocks the
        worktree and journals `failed`, even when the relock itself times out."""
        for stuck in (('worktree remove',), ('worktree remove', 'worktree lock')):
            with self.subTest(stuck=stuck):
                scene = self.scene()
                scene.worktree('w')
                refused = scene.check_refused

                def timing_out(where, command, refused=refused, stuck=stuck):
                    if command.startswith(stuck):
                        raise subprocess.TimeoutExpired(['git', command], 60)
                    refused(where, command)
                scene.check_refused = timing_out
                trace = scene.record(apply=True)
                row = row_at(trace)
                marker = '<TMP>/repos/alpha/.git/worktrees/w/locked'      # gone only if the relock did not run
                self.assertEqual((row['action'], trace['deleted']), ('keep', [] if len(stuck) == 1 else [marker]))
                self.assertEqual([(e['path'], e['action'], 'time' in e['error']) for e in trace['errors']], [(TREE, 'keep', True)])
                self.assertEqual([record['phase'] for record in trace['journal']], ['planned', 'failed'])
                self.assertEqual(scene.git.count('worktree', 'lock'), 1)
                self.assertEqual((admin(scene) / 'locked').exists(), len(stuck) == 1)

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
