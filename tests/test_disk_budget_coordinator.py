"""The coordinator of a budget purge across both sides."""
import json
from unittest.mock import patch
import unittest

from drover import disk, disk_cleanup, disk_state, placement
from tests.disk_fixture import GIB, empty_result
from tests.disk_budget_fixture import BUDGET, BudgetCase


class Coordinator(BudgetCase):
    """What disk_cleanup.run asks of the sides for a wsl claim; the sides themselves are not run."""
    def setUp(self):
        super().setUp()
        self.answer = dict(empty_result(), budget=dict(status='met', used_gb=224.0, target_gb=225.0, shortfall_bytes=0))
        self.approve_report()
        self.calls.clear()

    def handler(self, side, request):
        return self.answer

    def run_triggered(self, *, handler=None, apply=True):
        return self.run_cleanup(apply=apply, mode='triggered', real=False, handler=handler or self.handler)

    def asked(self):
        return [(side, request['stage'], request.get('budget')) for side, request in self.calls]

    def test_wsl_only_trigger_reaches_the_wsl_output_stage_alone(self):
        self.claim()
        self.run_triggered()
        self.assertEqual(self.asked(), [('wsl', 5, BUDGET)])
        request = self.calls[0][1]
        self.assertTrue(request['apply'])
        self.assertEqual(request['drive']['name'], 'test-drive')

    def test_configured_target_is_the_budget(self):
        self.cfg['disk_cleanup'].update(wsl_target_gb=150.0, wsl_purge_gb=200.0, wsl_stop_gb=300.0)
        self.approve_report()
        self.calls.clear()
        self.claim()
        self.run_triggered()
        self.assertEqual(self.asked(), [('wsl', 5, dict(BUDGET, target_gb=150.0))])

    def test_low_drive_without_its_own_claim_runs_no_drive_stages(self):
        self.free = 80.
        self.claim()
        self.run_triggered()
        self.assertEqual(self.asked(), [('wsl', 5, BUDGET)])

    def test_drive_claim_keeps_its_stages_and_they_carry_no_budget(self):
        self.free = 80.
        self.claim()

        def seed(state):
            state['episodes']['test-drive'] = dict(claim=dict(id='drive-claim', identity={}, applied=True, at=self.now))
        disk_state.update(self.state, seed)
        self.run_triggered()
        asked = self.asked()
        self.assertEqual(asked[-1], ('wsl', 5, BUDGET))
        self.assertEqual([a for a in asked if a[2]], [asked[-1]])
        self.assertEqual({a[1] for a in asked[:-1]}, {1, 2, 3, 4, 5, 6})
        self.assertIsNone(self.episode()['claim'])
        self.assertTrue((self.state / 'disk' / 'result-drive-claim.json').exists())

    def test_no_wsl_claim_means_no_budget(self):
        self.free = 80.
        self.run_triggered()
        self.assertTrue(self.calls)
        self.assertFalse([a for a in self.asked() if a[2]])

    def test_only_a_triggered_run_serves_the_claim(self):
        self.free = 80.
        self.claim()
        self.run_cleanup(apply=True, real=False, handler=self.handler)
        self.assertFalse([a for a in self.asked() if a[2]])
        self.assertIsNotNone(self.episode()['claim'])

    def test_receipt_clears_the_claim_and_sets_no_free_space_baseline(self):
        self.answer['reclaimed_bytes'] = 123
        self.claim()
        self.run_triggered()
        episode = self.episode()
        self.assertIsNone(episode['claim'])
        self.assertNotIn('post', episode)
        self.assertNotIn('report_post', episode)
        self.assertEqual(self.receipt(), dict(applied=True, status='completed', budget='met', used_gb=224.0,
                                              target_gb=225.0, reclaimed_bytes=123, shortfall_bytes=0, partial=False))

    def test_tick_also_settles_a_receipt_the_run_could_not_clear(self):
        self.claim()
        disk_state.save_file(self.state, 'result-wsl-claim.json', dict(applied=True, status='completed', budget='met'))
        with patch.object(placement, 'physical_disk_path', return_value=str(self.root)):
            disk.tick(self.cfg, now=self.now + 60, reader=lambda path: 200., wsl_used=lambda: 200., enqueue=lambda: None)
        self.assertIsNone(self.episode()['claim'])
        self.assertNotIn('post', self.episode())

    def test_unknown_drive_reading_stays_report_only(self):
        self.free = None
        self.claim()
        self.run_triggered()
        self.assertEqual([r['apply'] for s, r in self.calls if r.get('budget')], [False])
        self.assertFalse(self.receipt()['applied'])

    def test_failed_worker_still_ends_the_claim(self):
        self.claim()

        def fail(side, request):
            raise RuntimeError('worker gone')
        result = self.run_triggered(handler=fail)
        self.assertEqual(self.receipt()['budget'], 'failed')
        self.assertIsNone(self.episode()['claim'])
        self.assertIn('worker gone', json.dumps(result['errors']))

    def test_worker_answer_without_a_budget_is_a_failure(self):
        self.claim()
        self.answer = dict(empty_result(), errors=[dict(reason_code='worker_timeout')])
        self.run_triggered()
        self.assertEqual(self.receipt()['budget'], 'failed')

    def test_unconfirmed_termination_propagates(self):
        self.claim()

        def lost(side, request):
            error = RuntimeError('cannot confirm')
            error.termination_confirmed = False
            raise error
        with self.assertRaises(RuntimeError):
            self.run_triggered(handler=lost)
        self.assertIsNotNone(self.episode()['claim'])      # the dead-process path in tick releases it

    def test_no_wsl_host_drive_ends_the_claim_without_a_call(self):
        self.cfg['trackers'][0].pop('wsl_combined')
        self.cfg['trackers'][0].pop('host_floor_gb')
        self.approve_report()
        self.calls.clear()
        self.claim()
        self.run_triggered()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.receipt()['budget'], 'no_host')
        self.assertIsNone(self.episode()['claim'])

    def test_large_purge_is_trimmed_like_any_wsl_stage(self):
        self.answer['reclaimed_bytes'] = GIB
        self.claim()
        self.run_triggered()
        self.assertEqual(self.asked(), [('wsl', 5, BUDGET), ('windows', 1, None)])

    def test_dry_claim_runs_no_drive_stages_either(self):
        self.claim(applied=False)
        self.run_triggered(apply=False)
        self.assertEqual(self.asked(), [('wsl', 5, BUDGET)])
        self.assertEqual({r['apply'] for s, r in self.calls}, {False})

    def test_a_dry_claim_is_no_report_for_apply(self):
        # The stages it skipped are the report; it must not refresh the receipt that authorizes apply.
        self.cfg['disk_cleanup']['cooldown_s'] = 1234
        self.claim(applied=False)
        self.run_triggered(apply=False)
        self.assertNotEqual(disk_state.read(self.state).get('report_scope'), disk_cleanup.scope_key(self.cfg))

    def test_budget_observation_does_not_run_for_a_wsl_claim(self):
        machine = self.repos_dir / 'alpha.toml'
        machine.write_text(machine.read_text(encoding='utf-8') + 'budget_gb = 1\n', encoding='utf-8')
        self.approve_report()
        self.calls.clear()
        self.claim()
        self.run_triggered()
        self.assertEqual(self.asked(), [('wsl', 5, BUDGET)])


if __name__ == '__main__':
    unittest.main()
