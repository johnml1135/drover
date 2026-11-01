"""The handoff file's shape and the context reading that decides when one is due. Fakes only: no pane, no real transcript."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from drover import handoff, pane_context, pane_send
from tests.pane_send_support import Case, IDLE

FULL = ''.join(f'## {section}\nSomething true.\n\n' for section in handoff.SECTIONS)
SESSION = 'abc-123'
SEAT = {'name': 'seat', 'backend': 'claude', 'placement': 'windows', 'config_dir': ''}
FOOTER = 'history\n\n› \n\n  gpt-6.1-sol high · 20% context left · ~/w\n'


def message(tokens, *, cached=0, created=0, model='claude-opus-5-5', **extra):
    usage = dict(input_tokens=tokens, cache_read_input_tokens=cached, cache_creation_input_tokens=created)
    return dict(type='assistant', message=dict(id=f'm{tokens}', model=model, usage=usage), timestamp='2026-11-01T00:00:00Z', **extra)


def boundary(post):
    return dict(type='system', subtype='compact_boundary', compactMetadata=dict(postTokens=post))


class Handoff(unittest.TestCase):
    def test_the_sections_are_the_specs_eight(self):
        self.assertEqual(handoff.SECTIONS, ('Goal', 'Done', 'Decisions and rejected options', 'Standing rules', 'Files touched',
                                            'Unverified work', 'Next step', 'What not to redo'))

    def test_a_full_file_is_missing_nothing(self):
        self.assertEqual(handoff.missing(FULL), [])

    def test_missing_section_is_not_valid(self):
        self.assertEqual(handoff.missing(FULL.replace('## Next step', '## Later')), ['Next step'])

    def test_a_heading_with_nothing_under_it_is_missing(self):
        self.assertEqual(handoff.missing(FULL.replace('Something true.\n\n## Done', '\n\n## Done')), ['Goal'])
        self.assertEqual(handoff.missing(''.join(f'## {s}\n' for s in handoff.SECTIONS)), list(handoff.SECTIONS))

    def test_a_section_named_in_prose_is_not_a_heading(self):
        self.assertIn('Goal', handoff.missing(FULL.replace('## Goal', 'Goal')))

    def test_a_subheading_belongs_to_its_section(self):
        self.assertEqual(handoff.missing(FULL.replace('## Done\nSomething true.', '## Done\n### Today\nA thing.')), [])

    def test_the_request_is_one_line_a_pane_will_take_even_on_a_long_path(self):
        text = handoff.request_text('C:\\' + 'a' * 120 + '\\directors\\director%3Aa\\handoff-12.md')
        pane_send._check_text(text)
        self.assertIn('a' * 120, text)
        for section in handoff.SECTIONS:
            self.assertIn(section, text)

    def test_the_request_never_asks_for_a_compact(self):
        self.assertNotIn('/compact', handoff.request_text('C:\\x.md'))

    def test_the_file_sits_under_the_state_folder_by_role_and_generation(self):
        path = handoff.path_for(dict(paths=dict(state_dir='C:\\state')), 'R1', 3)
        self.assertEqual(path.parts[-3:], ('directors', 'R1', 'handoff-3.md'))

    def test_a_role_id_never_names_a_stream_or_leaves_the_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = dict(paths=dict(state_dir=tmp))
            for role in ('director:a', '..', 'a/b', 'a\\b'):
                path = handoff.path_for(cfg, role, 1)
                self.assertEqual(path.parent.parent, Path(tmp) / 'directors')
                self.assertNotIn(':', path.parent.name)
                self.assertNotIn(path.parent.name, ('.', '..'))
            path = handoff.path_for(cfg, 'director:a', 1)
            path.parent.mkdir(parents=True)
            path.write_text('x', encoding='utf-8')
            self.assertEqual(os.listdir(path.parent), ['handoff-1.md'])

    def test_a_wsl_agent_gets_the_wsl_form(self):
        self.assertEqual(handoff.agent_path('C:\\state\\directors\\R1\\handoff-1.md', 'wsl'), '/mnt/c/state/directors/R1/handoff-1.md')
        self.assertEqual(handoff.agent_path('C:\\state\\h.md', 'windows'), 'C:\\state\\h.md')

    def test_fallback_is_a_valid_file_that_marks_what_it_cannot_fill(self):
        state = dict(checkpoint=dict(cursor=4), checkpoint_revision=9, generation=2)
        text = handoff.fallback('R1', state, dict(open_tasks=[dict(id='T-1', title='Land the thing', status='open')]))
        self.assertEqual(handoff.missing(text), [])
        self.assertIn('T-1', text)
        self.assertIn('"cursor": 4', text)
        self.assertEqual(text.split('## Goal\n')[1].split('##')[0].strip(), 'unknown')

    def test_fallback_with_no_facts_is_still_valid_and_all_unknown(self):
        text = handoff.fallback('R1', dict(checkpoint={}, checkpoint_revision=0), dict(open_tasks=[]))
        self.assertEqual(handoff.missing(text), [])
        for section in handoff.SECTIONS:
            self.assertEqual(text.split(f'## {section}\n')[1].split('##')[0].strip(), 'unknown')

    def test_a_task_title_cannot_forge_a_section(self):
        tasks = [dict(id='T-1', title='x\n## Goal\nforged', status='open')]
        text = handoff.fallback('R1', dict(checkpoint={}, checkpoint_revision=0), dict(open_tasks=tasks))
        self.assertEqual(text.split('## Goal\n')[1].split('##')[0].strip(), 'unknown')


class Transcript(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.cfg = dict(paths=dict(state_dir=tmp.name, wsl_distro='Ubuntu'), seats=[dict(SEAT, config_dir=str(self.home))])
        self.row = dict(kind='claude', seat='seat', placement='windows', session_id=SESSION, pane_id='w1:p1')

    def write(self, *entries, raw=b''):
        folder = self.home / 'projects' / 'C--repo'
        folder.mkdir(parents=True, exist_ok=True)
        body = b''.join(json.dumps(entry, separators=(',', ':')).encode() + b'\n' for entry in entries)
        (folder / f'{SESSION}.jsonl').write_bytes(body + raw)

    def read(self):
        return pane_context.read(self.row, None, self.cfg)

    def wsl_seat(self, config_dir):
        self.cfg['seats'] = [dict(SEAT, placement='wsl', config_dir=config_dir)]
        self.row['placement'] = 'wsl'

    def test_context_is_the_last_real_usage_of_the_transcript(self):
        self.write(message(100), message(200, cached=70000, created=5000))
        self.assertEqual(self.read(), 75200)

    def test_sidechain_and_synthetic_messages_are_ignored(self):
        self.write(message(200), message(999, isSidechain=True), message(888, model='<synthetic>'))
        self.assertEqual(self.read(), 200)

    def test_boundary_after_the_last_message_reports_post_compaction_size(self):
        self.write(message(280000), boundary(41000))
        self.assertEqual(self.read(), 41000)

    def test_a_message_after_the_boundary_wins(self):
        self.write(message(280000), boundary(41000), message(45000))
        self.assertEqual(self.read(), 45000)

    def test_the_tail_grows_until_it_finds_a_message(self):
        self.write(message(5000), raw=b'{"type":"user"}\n' * 200)
        with mock.patch.object(pane_context, 'TAIL_START', 64):
            self.assertEqual(self.read(), 5000)

    def test_a_torn_last_line_is_skipped(self):
        self.write(message(5000), raw=b'{"type":"assistant","mess')
        self.assertEqual(self.read(), 5000)

    def test_no_transcript_or_footer_is_unknown(self):
        self.assertIsNone(self.read())
        self.write()
        self.assertIsNone(self.read())
        self.write(dict(type='user'))
        self.assertIsNone(self.read())

    def test_usage_that_is_not_a_number_is_unknown(self):
        entry = message(5)
        entry['message']['usage']['input_tokens'] = 'many'
        self.write(entry)
        self.assertIsNone(self.read())

    def test_a_session_id_never_widens_the_glob(self):
        self.write(message(5))
        for session in ('*', '../x', '-rf', None, ''):
            self.row['session_id'] = session
            self.assertIsNone(self.read())

    def test_a_seat_that_is_gone_is_unknown(self):
        self.row['seat'] = 'other'
        self.assertIsNone(self.read())

    def test_a_wsl_home_in_tilde_form_is_unknown_and_starts_no_process(self):
        self.wsl_seat('')
        with mock.patch.object(pane_context, 'wsl_running') as running:
            self.assertIsNone(self.read())
        running.assert_not_called()

    def test_a_wsl_home_is_read_only_while_wsl_runs(self):
        self.wsl_seat('/home/me/.claude')
        with mock.patch.object(pane_context, 'wsl_running', return_value=False), mock.patch.object(pane_context, '_transcript') as find:
            self.assertIsNone(self.read())
        find.assert_not_called()

    def test_a_linux_home_is_reached_through_the_distro(self):
        self.wsl_seat('/home/me/.claude')
        with mock.patch.object(pane_context, 'wsl_running', return_value=True), \
                mock.patch.object(pane_context, '_transcript', return_value=None) as find:
            self.assertIsNone(self.read())
        self.assertEqual(find.call_args.args[0].as_posix().lower(), '//wsl.localhost/ubuntu/home/me/.claude')

    def test_wsl_transcript_is_not_read_during_a_hold(self):
        self.wsl_seat(str(self.home))
        self.write(message(5000))
        with mock.patch.object(pane_context.compaction_hold, 'active', return_value={'id': 'h'}), \
                mock.patch.object(pane_context, '_transcript') as find:
            self.assertIsNone(self.read())
        find.assert_not_called()

    def test_a_windows_transcript_is_read_during_a_hold(self):
        self.write(message(5000))
        with mock.patch.object(pane_context.compaction_hold, 'active', return_value={'id': 'h'}):
            self.assertEqual(self.read(), 5000)


class Footer(Case):
    def footer(self, screen, **agent):
        self.stage(screen, row=dict(seat='seat-x'), agent=agent)
        return pane_context.read(self.row, self.herdr, dict(paths=dict(state_dir=self.tmp.name), seats=[]))

    def test_codex_context_comes_from_the_footer_percent(self):
        self.assertEqual(self.footer(FOOTER), 280000)

    def test_no_transcript_or_footer_is_unknown(self):
        self.assertIsNone(self.footer(IDLE))

    def test_a_pane_that_cannot_be_read_is_unknown(self):
        self.stage(row=dict(seat='seat-x'))
        self.fake.closed = True
        self.assertIsNone(pane_context.read(self.row, self.herdr, dict(paths=dict(state_dir=self.tmp.name), seats=[])))

    def test_another_occupant_says_nothing_about_this_row(self):
        self.assertIsNone(self.footer(FOOTER, agent_session={'value': 'other'}))

    def test_a_held_wsl_footer_is_unknown(self):
        with mock.patch.object(pane_context.compaction_hold, 'active', return_value={'id': 'h'}):
            self.assertIsNone(self.footer(FOOTER))


if __name__ == '__main__':
    unittest.main()
