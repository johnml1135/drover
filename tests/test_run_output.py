"""RunOutput reads every stored shape of a run's output and writes the three-key dict."""
import unittest

from drover.run_output import RunOutput

THREE_KEY_DICT = {'base': 'b', 'scope_files': ['f'], 'worktree': 'w'}


class RunOutputCodec(unittest.TestCase):
    def test_from_event_table(self):
        for stored, want in ((THREE_KEY_DICT, RunOutput('b', ('f',), 'w')), ({'base': 'b'}, RunOutput('b', (), None)),
                             ({'base': 'b', 'extra': 1}, RunOutput('b', (), None)), ({'base': ''}, None), ({}, None),
                             (None, None)):
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
        import dataclasses
        self.assertEqual([f.name for f in dataclasses.fields(RunOutput)], ['base', 'scope_files', 'worktree'])
        with self.assertRaises(dataclasses.FrozenInstanceError):
            RunOutput('b', (), None).base = 'c'

    def test_outside_land_fixture_one_key_dict_decodes(self):
        for base in ('a' * 40, None):     # tests/outside_land_fixture.py stores base and {'base': base}
            self.assertEqual(RunOutput.from_event(base and {'base': base}), base and RunOutput(base, (), None))
