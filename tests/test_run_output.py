"""RunOutput reads every stored shape of a run's output and writes the three-key dict."""
import dataclasses
import unittest
from unittest.mock import MagicMock

from drover.dispatcher_finish import FinishSteps
from drover.run_output import RunOutput

THREE_KEY_DICT = {'base': 'b', 'scope_files': ['f'], 'worktree': 'w'}


class RunOutputCodec(unittest.TestCase):
    def test_from_event_table(self):
        for stored, want in ((THREE_KEY_DICT, RunOutput('b', ('f',), 'w')), ({'base': 'b'}, RunOutput('b', (), None)),
                             ({'base': 'b', 'extra': 1}, RunOutput('b', (), None)), ({'base': ''}, None), ({}, None),
                             (None, None),
                             ('x', None), ([1], None), (5, None), ({'base': 5}, None),
                             ({'base': 'b', 'scope_files': 'ab'}, None), ({'base': 'b', 'scope_files': 5}, None),
                             ({'base': 'b', 'worktree': 3}, None),
                             ({'base': 'b', 'scope_files': ['f', 3, None]}, RunOutput('b', ('f',), None)),
                             ({'base': 'b', 'scope_files': None, 'worktree': None}, RunOutput('b', (), None))):
            self.assertEqual(RunOutput.from_event(stored), want, stored)

    def test_to_event_writes_the_same_three_keys(self):
        self.assertEqual(RunOutput('b', ('f', 'g'), 'w').to_event(), {'base': 'b', 'scope_files': ['f', 'g'], 'worktree': 'w'})
        self.assertEqual(RunOutput('b', (), None).to_event(), {'base': 'b', 'scope_files': [], 'worktree': None})
        self.assertEqual(RunOutput.from_event(THREE_KEY_DICT).to_event(), THREE_KEY_DICT)

    def test_owns_is_the_check_at_dispatcher_finish_246(self):
        out = RunOutput('b', (), 'w')
        self.assertTrue(out.owns('w'))
        self.assertFalse(out.owns('other'))
        self.assertFalse(out.owns(None))
        self.assertTrue(RunOutput('b', (), None).owns(None))
        self.assertFalse(RunOutput('b', (), None).owns('w'))

    def test_record_is_frozen_with_three_fields(self):
        self.assertEqual([f.name for f in dataclasses.fields(RunOutput)], ['base', 'scope_files', 'worktree'])
        with self.assertRaises(dataclasses.FrozenInstanceError):
            RunOutput('b', (), None).base = 'c'

    def test_outside_land_fixture_one_key_dict_decodes(self):
        for base in ('a' * 40, None):     # tests/outside_land_fixture.py stores base and {'base': base}
            self.assertEqual(RunOutput.from_event(base and {'base': base}), base and RunOutput(base, (), None))


class FinishChecksOwnership(unittest.TestCase):
    """dispatcher_finish refuses a run whose output belongs to another worktree, as it refused before RunOutput."""
    def finish(self, task_worktree, stored):
        steps = MagicMock()
        steps._end_if_yanked.return_value = False
        steps._compaction_killed.return_value = False
        steps._allowance_used.return_value = 0
        port = steps.port
        port.waiting.replans.return_value = []
        port.get_task.return_value = {'id': 't', 'worktree': task_worktree, 'write': True}
        result = {'status': 'ready_to_commit', 'usage': {'tokens_in': 0, 'tokens_out': 0, 'tokens_cached': 0},
                  'record': {'status': 'ready_to_commit', 'commit_message': 'm'}}
        FinishSteps._finish(steps, 1, 't', {'role': 'worker', 'output_snapshot': stored}, result)
        return steps

    def test_a_run_whose_output_does_not_own_the_worktree_is_refused(self):
        steps = self.finish('moved', {'base': 'b', 'scope_files': ['f'], 'worktree': 'w'})
        steps.pipeline.commit_ready.assert_not_called()
        steps.port.set_status.assert_called_once()
        self.assertEqual(steps.port.set_status.call_args.args[:2], ('t', 'needs-owner'))
        self.assertIn('worktree changed', steps.port.set_status.call_args.kwargs['reason'])

    def test_an_unreadable_stored_output_is_refused(self):
        for stored in (None, 'x', {'base': 'b', 'scope_files': 5}):
            steps = self.finish('w', stored)
            steps.pipeline.commit_ready.assert_not_called()

    def test_an_owning_run_commits_from_its_base_with_its_scope(self):
        steps = self.finish('w', {'base': 'b', 'scope_files': ['f'], 'worktree': 'w'})
        task, = steps.pipeline.commit_ready.call_args.args[:1]
        self.assertEqual(task['scope_files'], ['f'])
        self.assertEqual(steps.pipeline.commit_ready.call_args.kwargs['base'], 'b')

    def test_a_one_key_stored_output_has_no_scope(self):
        steps = self.finish(None, {'base': 'b'})
        self.assertEqual(steps.pipeline.commit_ready.call_args.args[0]['scope_files'], [])
