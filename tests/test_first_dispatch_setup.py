"""First dispatch git deadline, allowance path, brief, review and retry regressions."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from drover import allowance, brief, config, contract, dispatcher, launch, launch_usage, route
from tests.test_dispatcher import Base, res


class GitTimeouts(unittest.TestCase):
    def test_completed_git_past_deadline_keeps_output_on_timeout(self):
        from drover import placement_git as placement
        argv = ['git', '-C', 'repo', 'fetch', 'origin']
        completed = subprocess.CompletedProcess(argv, 0, 'fetched', 'diagnostic')
        with patch.object(placement, '_git_command', return_value=(argv, {})), \
                patch.object(placement.time, 'perf_counter', side_effect=[100, 101]), \
                self.assertRaises(subprocess.TimeoutExpired) as raised:
            placement.git('repo', 'fetch', 'origin', timeout=0.5, runner=lambda *a, **kw: completed)
        self.assertEqual(raised.exception.cmd, argv)
        self.assertEqual(raised.exception.timeout, 0.5)
        self.assertEqual(raised.exception.output, 'fetched')
        self.assertEqual(raised.exception.stderr, 'diagnostic')

    def test_pipeline_reports_late_completion_as_timeout(self):
        from drover import pipeline, placement_git as placement
        argv = ['git', '-C', 'repo', 'fetch', 'origin']
        with patch.object(placement, '_git_command', return_value=(argv, {})), \
                patch.object(placement.time, 'perf_counter', side_effect=[100, 101]):
            result = pipeline.git('repo', 'fetch', 'origin', timeout=0.5,
                                  runner=lambda *a, **kw: subprocess.CompletedProcess(argv, 0, '', ''))
        self.assertEqual(result.returncode, 124)
        self.assertIn('timed out', result.stderr)

    def test_git_within_deadline_returns_result_and_passes_timeout(self):
        from drover import placement_git as placement
        argv = ['git', '-C', 'repo', 'fetch', 'origin']
        completed = subprocess.CompletedProcess(argv, 0, 'fetched', '')
        with patch.object(placement, '_git_command', return_value=(argv, {})), \
                patch.object(placement.time, 'perf_counter', side_effect=[100, 100.25]), \
                patch.object(placement.subprocess, 'run', return_value=completed) as run:
            self.assertIs(placement.git('repo', 'fetch', 'origin', timeout=0.5), completed)
        self.assertEqual(run.call_args.kwargs['timeout'], 0.5)

    def test_git_without_deadline_does_not_check_elapsed_time(self):
        from drover import placement_git as placement
        argv = ['git', '-C', 'repo', 'status']
        completed = subprocess.CompletedProcess(argv, 0, '', '')
        with patch.object(placement, '_git_command', return_value=(argv, {})), \
                patch.object(placement.time, 'perf_counter', side_effect=AssertionError('no deadline')):
            self.assertIs(placement.git('repo', 'status', runner=lambda *a, **kw: completed), completed)


class AllowancePaths(unittest.TestCase):
    def test_config_state_and_one_time_legacy_migration(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            root = Path(tmp)
            legacy = root / '.local/state/drover/allowance'
            legacy.mkdir(parents=True)
            data = {'remaining_pct': 80, 'set_at': 100}
            (legacy / 'manual-codex.json').write_text(json.dumps(data))
            config.use(config.validate({'paths': {'state_dir': str(root / 'state')}}))
            self.addCleanup(config.reset)
            with patch.object(Path, 'home', return_value=root):
                self.assertEqual(allowance.state_dir(), root / 'state')
                self.assertIsNotNone(allowance.read_manual('codex', now=101))
                (legacy / 'manual-codex.json').write_text(json.dumps({**data, 'remaining_pct': 20}))
                self.assertEqual(100 - allowance.read_manual('codex', now=102)['windows']['seven_day']['used_pct'], 80)
            with patch.dict(os.environ, {'DROVER_STATE': str(root / 'override')}):
                self.assertEqual(allowance.state_dir(), root / 'override')

    def test_migration_skips_unreadable_legacy_files_and_keeps_configured_readings(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            root = Path(tmp)
            legacy = root / '.local/state/drover/allowance'
            target = root / 'state/allowance'
            legacy.mkdir(parents=True)
            target.mkdir(parents=True)
            (legacy / 'manual-codex.json').write_bytes(b'\xff')
            (legacy / 'claude-unreadable.json').write_bytes(b'\xff')
            (target / 'manual-codex.json').write_text(json.dumps({'remaining_pct': 80, 'set_at': 100}))
            config.use(config.validate({'paths': {'state_dir': str(root / 'state')}}))
            self.addCleanup(config.reset)
            with patch.object(Path, 'home', return_value=root):
                reading = allowance.read_manual('codex', now=101)
                self.assertEqual(reading['windows']['seven_day']['used_pct'], 20)
                self.assertEqual(allowance.read_claude(now=101)['source'], 'unknown')
            self.assertTrue((target / '.legacy-migrated').exists())

    def test_state_override_expands_home_like_configured_paths(self):
        with patch.dict(os.environ, {'DROVER_STATE': '~/.drover-custom'}):
            self.assertEqual(allowance.state_dir(), Path(os.path.expanduser('~/.drover-custom')))


class BriefPolicy(unittest.TestCase):
    def test_framework_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.specify').mkdir()
            task = {'id': 'T-8', 'title': 'fix', 'size': 'M', 'worktree': tmp}
            prompt = brief.assemble(task)['user_prompt']
            self.assertIn('.specify/', prompt)
            self.assertIn('Do not create', prompt)
            self.assertIn('approved design', prompt)

    def test_wsl_framework_detection_handles_nested_tree_listing(self):
        with patch('drover.placement.routes_through_wsl', return_value=True), \
                patch('drover.placement.git', return_value=SimpleNamespace(returncode=0, stdout='docs/superpowers/plans\n')):
            self.assertIn('docs/superpowers/', brief.framework_text('/home/seat/work/task'))

    def test_review_preserves_finding_classification(self):
        record = contract.parse_review_record(json.dumps({'verdict': 'HOLD', 'findings': [
            {'kind': 'follow-up', 'detail': 'Future feature'}, {'kind': 'blocking', 'detail': 'a.py:3 wrong result'}]}))
        self.assertEqual(record['findings'][0]['kind'], 'follow-up')

    def test_planner_fallback_uses_strongest_model(self):
        cfg = config.validate({})
        config.use(cfg)
        self.addCleanup(config.reset)
        choice = route.choose({'role': 'planner'}, {}, seat_headroom={'codex': {'remaining_pct': 90}})
        self.assertEqual(choice['model'], cfg['models']['ladders']['codex'][-1])
        self.assertEqual(choice['effort'], 'high')


class SetupClassification(unittest.TestCase):
    STATUS = {'started': '2026-10-24T10:00:00+00:00', 'ended': '2026-10-24T10:00:02+00:00', 'exit_code': 1}

    def test_only_fast_pre_model_known_errors_are_infrastructure(self):
        error = 'Error: CODEX_HOME="~/.codex": path does not exist'
        self.assertIsNotNone(launch_usage.infrastructure_error_of(self.STATUS, False, error))
        self.assertIsNone(launch_usage.infrastructure_error_of(self.STATUS, True, error))
        self.assertIsNone(launch_usage.infrastructure_error_of({**self.STATUS, 'ended': '2026-10-24T10:01:00+00:00'}, False, error))
        self.assertIsNone(launch_usage.infrastructure_error_of(self.STATUS, False, 'assertion failed in user tests'))
        self.assertIsNone(launch_usage.infrastructure_error_of({**self.STATUS, 'exit_code': 0}, False, error))

    def test_runner_reads_setup_diagnostic_from_raw_stream(self):
        from drover import runner
        with tempfile.TemporaryDirectory() as tmp:
            spec = {'run_id': 1, 'task': {'id': 'T-8'}}
            handle = launch.RunnerHandle(spec, tmp, ceiling=0, autostart=False)
            handle.dir.mkdir(parents=True)
            (handle.dir / runner.OUT_FILE).write_text('Error: CODEX_HOME path does not exist')
            result = handle._finish(self.STATUS)
            self.assertIsNotNone(result['infrastructure_error'])
            handle.spec['role'] = 'gate'
            self.assertIsNone(handle._finish(self.STATUS)['infrastructure_error'])
            handle.spec['role'] = 'worker'
            handle._usage.feed({'type': 'item.started'})
            self.assertIsNone(handle._finish(self.STATUS)['infrastructure_error'])


class Reviews(Base):
    SHA = 'a' * 40

    def review(self, findings, verdict='HOLD', record_sha=None):
        tid = self.task()
        self.d.tick()
        from tests.ledger_fixture import stage
        worker = self.port.list_runs(tid)[0]['id']
        stage(self.conn, tid, 'gated', self.SHA)
        run = self.port.start_run(tid, 'review', 'codex', 'gpt-6.1-sol', 'high')
        self.port.append_event('review-started', {'run': run, 'sha': self.SHA, 'worker_run': worker}, tid)
        self.pipe.current_head = lambda: self.SHA
        text = json.dumps({'verdict': verdict, 'sha': record_sha or self.SHA, 'findings': findings})
        self.d._finish_review(run, tid, {'backend': 'codex'}, res('done', text))
        return tid, run

    def test_only_followups_land_and_create_related_proposals(self):
        tid, run = self.review([{'kind': 'follow-up', 'detail': 'Future capability beyond this task'}])
        self.assertEqual(self.status(tid), 'reviewed')
        from drover.ledger import ops
        related = ops.related_of(self.conn, tid)
        self.assertEqual(len(related), 1)
        self.assertEqual(self.status(related[0]), 'proposed')
        self.assertEqual(dispatcher.failures(self.port.list_runs(tid)), 0)
        self.port.propose_review_followups(tid, run, [{'kind': 'follow-up', 'detail': 'Future capability beyond this task'}])
        self.assertEqual(ops.related_of(self.conn, tid), related)

    def test_blocking_finding_holds_even_with_land_verdict(self):
        tid, _ = self.review([{'kind': 'blocking', 'detail': 'a.py:3 return is wrong'},
                             {'kind': 'follow-up', 'detail': 'Future improvement'}], 'LAND@' + self.SHA)
        self.assertEqual(self.status(tid), 'ready')
        self.assertEqual(dispatcher.failures(self.port.list_runs(tid)), 1)
        excerpt = self.port.list_runs(tid)[-1]['gate_receipt']
        self.assertIn('return is wrong', str(excerpt))
        self.assertNotIn('Future improvement', str(excerpt))

    def test_followup_hold_without_exact_sha_cannot_land(self):
        tid, _ = self.review([{'kind': 'follow-up', 'detail': 'Future improvement'}], record_sha='b' * 40)
        self.assertEqual(self.status(tid), 'gated')
        self.assertEqual(self.port.list_runs(tid)[-1]['outcome'], 'no-record')

    def test_second_short_review_at_top_gets_one_extra_fix_round(self):
        tid = self.task()
        top = dispatcher.ladders()['codex'][-1]
        for n in range(3):
            from tests.ledger_fixture import claim, stage
            claim(self.conn, tid)
            worker = self.port.start_run(tid, 'worker', 'codex', top, 'high')
            stage(self.conn, tid, 'gated', self.SHA)
            run = self.port.start_run(tid, 'review', 'codex', top, 'high')
            self.port.append_event('review-started', {'run': run, 'sha': self.SHA, 'worker_run': worker}, tid)
            text = json.dumps({'verdict': 'HOLD', 'findings': [{'kind': 'blocking', 'detail': 'a.py:3 fix return'}]})
            self.d._finish_review(run, tid, {'backend': 'codex'}, res('done', text))
            if n < 2:
                self.assertEqual(self.status(tid), 'ready')
                self.assertFalse(self.port.waiting.replans())
        self.assertEqual(len(self.events('review-fix-round')), 1)
        self.assertTrue(self.port.waiting.replans())


class RetryContext(Base):
    def test_retry_brief_names_inherited_changes_and_run(self):
        tid = self.task()
        self.d.tick()
        first = self.port.list_runs(tid)[0]['id']
        self.finish(0, res('error'))
        with patch.object(self.pipe, 'status', return_value=' M drover/a.py\n?? tests/new.py'):
            self.d.tick()
        prompt = self.fl.specs[-1]['user_prompt']
        self.assertIn(f'Earlier worker runs: {first}', prompt)
        self.assertIn('drover/a.py', prompt)
        self.assertIn('tests/new.py', prompt)
        self.assertIn('continue them', prompt)
