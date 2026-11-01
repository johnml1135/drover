"""Dialog and startup rules on pane screens. Screens are literals: some from the skill's tests, some from review probes."""
import unittest

from drover.pane_guard import (
    adjacent_dialog,
    blocking_dialog,
    bottom_region,
    decode_screen,
    model_prompt,
    queued_input_range,
    refuse_dialog,
    startup_keys,
    strong_dialog,
    suspected_dialog,
    working,
)

DIALOG = '1. Switch to gpt-6-luna\n2. Keep current model\n3. Keep current model (never show again)\nenter select · esc back\n› \n'
TRUST = 'Trust this folder?\n› 1. Trust and continue\n  2. Quit\n'
CLAUDE_TRUST = 'Do you trust the files in this folder?\n❯ 1. Yes, proceed\n  2. No, exit\n'


class ModelDialog(unittest.TestCase):
    def test_model_dialog_blocks_without_a_choice(self):
        for screen in (DIALOG, 'enter select · esc back', 'Switch to gpt-6-luna'):
            self.assertTrue(model_prompt(screen))
            with self.assertRaisesRegex(ValueError, 'owner-only'):
                refuse_dialog(screen)
        self.assertFalse(model_prompt('GPT-6.1-Sol high · worktree\n› \n'))
        self.assertFalse(blocking_dialog('1. No merge or push\n2. Continue implementation\n› \n'))

    def test_refusal_names_the_target(self):
        with self.assertRaisesRegex(ValueError, '^w1:p1: owner-only'):
            refuse_dialog(DIALOG, 'w1:p1')
        with self.assertRaisesRegex(ValueError, '^w1:p1: blocking dialog'):
            refuse_dialog('Allow once?\n› \n', 'w1:p1')
        self.assertIsNone(refuse_dialog('OpenAI Codex\n› \n? for shortcuts\n'))

    def test_a_missing_screen_is_refused_not_clear(self):
        with self.assertRaises(ValueError):
            refuse_dialog(None)


class OtherDialogs(unittest.TestCase):
    def test_selection_approval_question_trust_and_numbered_options_block(self):
        for screen in ('enter select', 'esc back', 'Allow once?', '2 questions', 'Trust this folder',
                       '1. Yes, proceed\n2. No', 'Would you like to run this command?', 'Update available'):
            self.assertTrue(blocking_dialog(screen), screen)
            with self.assertRaises(ValueError):
                refuse_dialog(screen)

    def test_strong_modal_overrides_working(self):
        for marker in ('Allow once?', 'Allow always', 'Requires approval', 'Do you trust this folder?',
                       'Trust this folder', 'enter select'):
            screen = '• Working …\n' + marker + '\n› \n'
            self.assertTrue(strong_dialog(screen), marker)
            self.assertTrue(blocking_dialog(screen), marker)
            with self.assertRaises(ValueError):
                refuse_dialog(screen)

    def test_selected_option_is_not_a_composer(self):
        for arrow in ('›', '❯'):
            body = 'Requires approval\n' + 'command detail\n' * 14 + arrow + ' 2. Reject this operation\n'
            for prefix in ('', 'Previous output\n────────────\n'):
                screen = prefix + body + '────────────\n'
                self.assertIn('Requires approval', bottom_region(screen))
                self.assertTrue(blocking_dialog(screen))
                with self.assertRaises(ValueError):
                    refuse_dialog(screen)

    def test_words_in_working_scrollback_do_not_block(self):
        self.assertFalse(blocking_dialog('• Working …\nResearch: 3 questions, update available\n› \n'))
        self.assertFalse(blocking_dialog('Result: 3 questions, update available\n────────────\n• Working …\n› \n'))
        self.assertIsNone(refuse_dialog('Research: 3 questions, update available\n────────────\n• Working …\n› \n'))
        self.assertIsNone(refuse_dialog('Research: 3 questions, update available\n────────────\n› \n'))

    def test_dialog_above_an_empty_composer_is_a_dialog(self):
        # Deliberate change from the skill, finding I5: with no separator, these words over the composer now refuse.
        for rows in ('Please choose: allow once?', 'Allow\nonce?', 'Allow\u00a0once?', 'Ａllow once?',
                     'Allow once?\n' + 'detail\n' * 8, 'Research: 3 questions, update available'):
            screen = rows.rstrip('\n') + '\n› \n? for shortcuts\n'
            self.assertTrue(adjacent_dialog(screen), rows)
            self.assertTrue(blocking_dialog(screen), rows)
            with self.assertRaises(ValueError):
                refuse_dialog(screen)
        self.assertFalse(adjacent_dialog('Allow once?\n────────────\n› \n'))
        self.assertFalse(adjacent_dialog('no composer here: allow once?\n'))

    def test_a_row_shaped_like_a_choice_is_suspected(self):
        for rows in ('Do you want to proceed?', 'Run rm -rf build? [y/N]', 'Run this (Y/n)', '1. Approve\n  2. Reject',
                     '› 1. Approve\n  2. Reject\n', '❯ 1. Approve\n  2. Reject\n',
                     'Requires approval: rm -rf\n1. Yes\n2. No\n────'):
            self.assertTrue(suspected_dialog(rows + '\n› \n? for shortcuts\n'), rows)
        for rows in ('All done.', 'Previous progress result: preserve approval/question UIs.', ''):
            self.assertFalse(suspected_dialog(rows + '\n› \n? for shortcuts\n'), rows)

    def test_a_hint_under_the_options_does_not_hide_them(self):
        # Finding I5: the option rows were looked for only in the last row.
        for hint in ('Use arrows to choose', '(esc to cancel)', 'Use arrows\n(esc to cancel)'):
            for rows in ('1. Approve\n2. Reject\n' + hint, '› 1. Approve\n  2. Reject\n' + hint, '1. Approve\n2. Reject\n' + hint + '\n\n'):
                self.assertTrue(suspected_dialog(rows + '\n› \n? for shortcuts\n'), rows)

    def test_only_the_rows_directly_above_the_composer_are_searched_for_options(self):
        for rows in ('1. Approve\n\nAll done.', '1. Approve\n────\nAll done.', '1. Approve\n---\nAll done.', 'Run it?\nAll done.'):
            self.assertFalse(suspected_dialog(rows + '\n› \n? for shortcuts\n'), rows)

    def test_a_dash_row_does_not_end_the_search_for_a_dialog(self):
        # Finding I5: `---` is text an agent or a dialog can print; only a real `─` row ends the search (rule 12).
        for rows in ('Requires approval: rm -rf\n---', 'Allow once\n---', 'Allow once\n  -----', 'Allow once\n---\n'):
            screen = rows + '\n› \n? for shortcuts\n'
            self.assertTrue(adjacent_dialog(screen), rows)
            self.assertTrue(blocking_dialog(screen), rows)
            with self.assertRaises(ValueError):
                refuse_dialog(screen)
        self.assertFalse(adjacent_dialog('Allow once\n────\n› \n? for shortcuts\n'))
        self.assertFalse(adjacent_dialog('Allow once\n━━━━\n› \n? for shortcuts\n'))

    def test_a_dash_row_does_not_hide_a_row_shaped_like_a_choice(self):
        for rows in ('Do you want to proceed?\n---', 'Run this (Y/n)\n  -----', '1. Approve\n2. Reject\n---'):
            self.assertTrue(suspected_dialog(rows + '\n› \n? for shortcuts\n'), rows)


class Working(unittest.TestCase):
    def test_working_ignores_scrollback_and_looks_above_the_queue(self):
        self.assertTrue(working('OpenAI Codex\nWorking (2s · esc to interrupt)\n› \n'))
        self.assertTrue(working('out\n• Working (5h 01m 35…\n› \nGPT-6.1-…\n'))
        self.assertFalse(working('• Worked for 3s\n› \n? for shortcuts\n'))
        self.assertFalse(working('Working (2s · esc to interrupt)\n────────────\n› \n? for shortcuts\n'))
        self.assertTrue(working('• Working (9m • esc to interrupt)\nQueued follow-up inputs\n  ↳ note\n'
                                '    shift+← edit last queued message\n› \n? for shortcuts\n'))

    def test_queue_range_needs_a_heading_or_a_footer_with_an_entry(self):
        self.assertEqual(queued_input_range('x\nQueued follow-up inputs\n↳ a\nshift+← edit last queued message\n› \n'), (1, 3))
        self.assertEqual(queued_input_range('x\n↳ a\nshift+← edit last queued message\n› \n'), (1, 2))
        self.assertIsNone(queued_input_range('x\nshift+← edit last queued message\n› \n'))
        self.assertIsNone(queued_input_range('↳ a quoted arrow\n› \n'))

    def test_a_queue_row_does_not_hide_a_working_row_below_it(self):
        # Finding I2. Rows above a real queue still count, and now so does everything below one.
        for screen in ('Queued follow-up inputs are drained on turn end.\n• Working (5s · esc to interrupt)\n› \n? for shortcuts\n',
                       'Queued follow-up inputs\n↳ a\nshift+← edit last queued message\n• Working (5s · esc to interrupt)\n› \n',
                       '↳ a\nshift+← edit last queued message\n• Working (5s)\n› \n? for shortcuts\n'):
            self.assertTrue(working(screen), screen)


class Startup(unittest.TestCase):
    def test_startup_keys_answers_only_codex_trust_in_a_fresh_pane(self):
        self.assertEqual(startup_keys(TRUST, True, 'codex'), ('enter',))
        self.assertEqual(startup_keys('\n' + TRUST + '\n', True, 'codex'), ('enter',))
        self.assertIsNone(startup_keys(TRUST, False, 'codex'))
        self.assertIsNone(startup_keys(TRUST, kind='codex'))
        self.assertIsNone(startup_keys(TRUST, True, 'claude'))
        self.assertIsNone(startup_keys(TRUST, True))
        self.assertIsNone(startup_keys(CLAUDE_TRUST, True, 'claude'))
        self.assertIsNone(startup_keys(CLAUDE_TRUST, True, 'codex'))

    def test_startup_update_keys_stay_with_the_owner(self):
        for screen in ('Skip until next version', 'Update available · esc skip',
                       'Trust this folder\nUpdate available · esc skip', TRUST + 'Update available\n'):
            self.assertIsNone(startup_keys(screen, True, 'codex'), screen)

    def test_startup_consent_never_covers_another_modal(self):
        for screen in ('Trust this folder: Allow once?',
                       'Trust this folder\n1. Switch to gpt-6-luna\nenter select · esc back',
                       'Trust this folder\n2 questions',
                       'Trust this folder\nNew hook - review required',
                       'Trust this folder\nWould you like to run this command?',
                       '• Working …\nTrust this folder\n› \n',
                       TRUST + 'Allow once?\n',
                       DIALOG):
            self.assertIsNone(startup_keys(screen, True, 'codex'), screen)

    def test_startup_ignores_trust_words_in_scrollback(self):
        self.assertIsNone(startup_keys('Research: trust this folder\n────────────\n› \n? for shortcuts\n', True, 'codex'))

    def test_startup_answers_nothing_but_the_exact_trust_dialog(self):
        # Finding C1. Bare trust words used to get Enter; the rule covers one dialog and "Trust and continue" only.
        for screen in ('Trust this folder', 'Trust this folder?', 'Do you trust this folder?',
                       'Do you trust this MCP server?\n› 1. Enable all tools\n',
                       'Trust this folder?\n› 1. Trust and enable network access\n  2. Quit\n',
                       'Trust this folder and allow all commands without asking?\n› 1. Yes\n',
                       'Trust this folder?\nDelete all files? press enter to confirm\n› 1. Yes\n',
                       'Trust this folder\nNew hook detected, review before enabling',
                       'Trust this folder?\n  1. Trust and continue\n› 2. Quit\n',
                       'Trust this folder?\n› 1. Trust and continue\n  2. Quit\n  3. Trust everything\n',
                       'Trust this folder?\n❯ 1. Trust and continue\n  2. Quit\n',
                       'Research: trust this folder\n? for shortcuts\n',
                       '$ codex "do you trust me"'):
            self.assertIsNone(startup_keys(screen, True, 'codex'), screen)

    def test_startup_gets_no_key_on_an_unreadable_screen(self):
        self.assertIsNone(startup_keys(TRUST + '\x07', True, 'codex'))
        self.assertIsNone(startup_keys(TRUST.encode('utf-8') + b'\xff', True, 'codex'))
        self.assertIsNone(startup_keys(None, True, 'codex'))


class Bytes(unittest.TestCase):
    def test_bytes_from_herdr_decode_as_utf8(self):
        raw = DIALOG.encode('utf-8')
        self.assertEqual(bottom_region(raw), bottom_region(DIALOG))
        self.assertTrue(model_prompt(raw))
        self.assertTrue(strong_dialog(raw))
        self.assertTrue(blocking_dialog(raw))
        with self.assertRaisesRegex(ValueError, 'owner-only'):
            refuse_dialog(raw)
        self.assertTrue(working('• Working …\n› \n'.encode('utf-8')))
        self.assertEqual(queued_input_range('Queued follow-up inputs\n↳ a\n'.encode('utf-8')), (0, 2))
        self.assertEqual(startup_keys(TRUST.encode('utf-8'), True, 'codex'), ('enter',))

    def test_unreadable_capture_is_refused_not_clear(self):
        # Finding I4: each of these hid a dialog or a Working row when decoded loosely.
        for screen in (b'\xffAllow once?\n\xe2\x80\xba \n? for shortcuts\n', '\ufffdAllow once?\n› \n',
                       '\x1b[1mAllow once?\x1b[0m\n› \n', '\x1b[2m• Working (3s)\x1b[0m\n› \n', '\x07Allow once?\n› \n',
                       '\x1b]0;x\x07Would you like to run this?\n› \n', '› owner draft\r› \n', '› \x0c\n', '› \x0b\n',
                       '› \u2028\n', '› \u2029\n', '› \x85\n', '› \x7f\n', '\u200bAllow once?\n› \n'):
            with self.assertRaises(ValueError, msg=repr(screen)):
                refuse_dialog(screen)
            with self.assertRaises(ValueError, msg=repr(screen)):
                blocking_dialog(screen)
            with self.assertRaises(ValueError, msg=repr(screen)):
                working(screen)

    def test_windows_line_endings_and_tabs_are_readable(self):
        self.assertEqual(decode_screen('a\r\n\tb\r\n'), 'a\n\tb\n')
        self.assertEqual(decode_screen('a\r\nb'.encode('utf-8')), 'a\nb')


if __name__ == '__main__':
    unittest.main()
