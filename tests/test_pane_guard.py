"""Dialog and startup rules on pane screens; every screen is a literal copied from the skill's tests."""
import unittest

from drover import pane_guard
from drover.pane_guard import (
    blocking_dialog,
    bottom_region,
    model_prompt,
    queued_input_range,
    refuse_dialog,
    startup_keys,
    strong_dialog,
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
        self.assertIsNone(refuse_dialog('Research: 3 questions, update available\n› \n'))


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


class Startup(unittest.TestCase):
    def test_startup_keys_answers_only_codex_trust_in_a_fresh_pane(self):
        for screen in (TRUST, 'Trust this folder', 'Do you trust this folder?'):
            self.assertEqual(startup_keys(screen, True, 'codex'), ('enter',), screen)
            self.assertIsNone(startup_keys(screen, False, 'codex'), screen)
            self.assertIsNone(startup_keys(screen, kind='codex'), screen)
            self.assertIsNone(startup_keys(screen, True, 'claude'), screen)
            self.assertIsNone(startup_keys(screen, True), screen)
        self.assertIsNone(startup_keys(CLAUDE_TRUST, True, 'claude'))

    def test_startup_update_keys_stay_with_the_owner(self):
        for screen in ('Skip until next version', 'Update available · esc skip',
                       'Trust this folder\nUpdate available · esc skip'):
            self.assertIsNone(startup_keys(screen, True, 'codex'), screen)

    def test_startup_consent_never_covers_another_modal(self):
        for screen in ('Trust this folder: Allow once?',
                       'Trust this folder\n1. Switch to gpt-6-luna\nenter select · esc back',
                       'Trust this folder\n2 questions',
                       'Trust this folder\nNew hook - review required',
                       'Trust this folder\nWould you like to run this command?',
                       '• Working …\nTrust this folder\n› \n',
                       DIALOG):
            self.assertIsNone(startup_keys(screen, True, 'codex'), screen)

    def test_startup_ignores_trust_words_in_scrollback(self):
        self.assertIsNone(startup_keys('Research: trust this folder\n────────────\n› \n? for shortcuts\n', True, 'codex'))


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

    def test_bytes_that_are_not_utf8_never_raise_a_decode_error(self):
        self.assertEqual(pane_guard.decode_screen(b'\xff\xfe'), '��')
        self.assertFalse(blocking_dialog(b'\xff\xfe'))


if __name__ == '__main__':
    unittest.main()
