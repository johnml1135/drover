"""Screens from the second review: the walk above the composer, a turn above a separator, and footer-shaped rows."""
import unittest

from drover.pane_guard import refuse_dialog
from drover.pane_screen import classify, composer_empty, composer_text, prompt_fingerprint, status, turn_showing

TAIL = '? for shortcuts\n'
FENCE = '────\n⏵⏵ auto mode on\n'
PROMPT = 'Read the brief now'
ITEM = dict(last_prompt_digest=prompt_fingerprint(PROMPT)[0], last_prompt_at=0)
SPINNER = '✻ Pondering… (12s · esc to interrupt)\n'


def stuck(screen, kind='codex', item=ITEM):
    return classify(dict(agent=kind), screen, item, 0)


class Walk(unittest.TestCase):
    def assert_not_empty(self, screen, kind):
        self.assertIsNone(status(screen, kind), screen)
        self.assertFalse(composer_empty(screen, kind), screen)
        self.assertIsNone(refuse_dialog(screen), screen)

    def test_a_rule_or_a_blank_row_in_a_draft_is_not_the_end_of_it(self):
        # Finding N1: each of these read idle and empty.
        for screen in ('› check this:\n---\n›\n' + TAIL, '› check this:\n  ---\n›\n' + TAIL,
                       '› check this:\n\n  more\n›\n' + TAIL):
            self.assert_not_empty(screen, 'codex')
            self.assertIn('check this:', composer_text(screen, 'codex'), screen)
        screen = '────\n❯ run this:\n---\n❯\n' + FENCE
        self.assert_not_empty(screen, 'claude')
        self.assertIn('run this:', composer_text(screen, 'claude'))

    def test_a_rule_or_a_blank_row_in_a_draft_is_not_unsent(self):
        # Finding N1: each of these read as Drover's own prompt, so Enter would have sent the owner's rows too.
        for screen, kind in (('› hold off\n---\n› ' + PROMPT + '\n' + TAIL, 'codex'),
                             ('────\n❯ hold off\n---\n❯ ' + PROMPT + '\n' + FENCE, 'claude'),
                             ('› hold off\n\n  more\n› ' + PROMPT + '\n' + TAIL, 'codex')):
            self.assertEqual(stuck(screen, kind), 'owner_draft', screen)
            self.assertIn('hold off', composer_text(screen, kind), screen)

    def test_a_fence_does_not_prove_the_row_above_it_is_finished(self):
        # C2's remainder: a real `─` row between two marks may be a past prompt or a draft, and only a capture can say.
        for screen, kind in (('❯ a\n────\n❯\n' + FENCE, 'claude'), ('› a\n────\n›\n' + TAIL, 'codex'),
                             ('────\n❯ a\n\n────\n❯\n' + FENCE, 'claude')):
            self.assert_not_empty(screen, kind)
        self.assertEqual(stuck('❯ a\n────\n❯ ' + PROMPT + '\n' + FENCE, 'claude'), 'owner_draft')
        self.assertEqual(stuck('› a\n────\n› ' + PROMPT + '\n' + TAIL), 'owner_draft')

    def test_a_mark_with_nothing_to_explain_it_is_not_a_past_prompt(self):
        self.assert_not_empty('› an earlier prompt\n\n› \n' + TAIL, 'codex')
        self.assert_not_empty('› an earlier prompt\n\nsome output\n\n› \n' + TAIL, 'codex')

    def test_an_answer_row_in_a_draft_does_not_end_the_walk(self):
        # A row copied from another pane: indented, or with no blank row above it, it is no answer that started here.
        for screen in ('› run the tests\n• Ran 72 tests, OK.\n›\n' + TAIL, '› run the tests\n  more\n• Ran 72 tests\n›\n' + TAIL,
                       '› run the tests\n\n  • Ran 72 tests\n\n›\n' + TAIL):
            self.assert_not_empty(screen, 'codex')
        for screen in ('› hold off\n• note\n› ' + PROMPT + '\n' + TAIL, '› hold off\n\n  • note\n\n› ' + PROMPT + '\n' + TAIL):
            self.assertEqual(stuck(screen), 'owner_draft', screen)

    def test_claude_has_no_captured_answer_row_so_nothing_ends_its_walk(self):
        for row in ('• Done.', '● Done.', '⏺ Done.'):
            self.assert_not_empty('❯ an earlier prompt\n\n' + row + '\n────\n❯\n' + FENCE, 'claude')
            self.assertEqual(stuck('❯ an earlier prompt\n\n' + row + '\n────\n❯ ' + PROMPT + '\n' + FENCE, 'claude'), 'owner_draft')

    def test_a_codex_answer_ends_the_walk(self):
        for screen in ('› past\n\n• Done.\n\n› \n' + TAIL, '› past\n\n• Done.\n────\n› \n' + TAIL,
                       '› past\n\n• first\n• second\n\n› \n' + TAIL, '› past\n\n• first\n  rest\n\n› \n' + TAIL):
            self.assertEqual(status(screen, 'codex'), 'idle', screen)
            self.assertEqual(composer_text(screen, 'codex'), '', screen)
        self.assertEqual(stuck('› past\n\n• Done.\n\n› ' + PROMPT + '\n' + TAIL), 'unsent_prompt')
        self.assertEqual(stuck('› past\n\n• Done.\n\n› hold off\n---\n› ' + PROMPT + '\n' + TAIL), 'owner_draft')

    def test_rows_with_no_mark_above_them_stay_transcript(self):
        self.assertEqual(status('output\n---\n› \n' + TAIL, 'codex'), 'idle')
        self.assertEqual(status('output\n\n  more\n\n› \n' + TAIL, 'codex'), 'idle')
        self.assertEqual(status('output\n---\n────\n❯\n' + FENCE, 'claude'), 'idle')
        self.assertEqual(stuck('output\n---\n› ' + PROMPT + '\n' + TAIL), 'unsent_prompt')

    def test_an_indented_mark_is_never_unsent(self):
        # Finding M9 is this rule's screen: the row above the mark cannot be accounted for.
        self.assertEqual(stuck('owner note\n  › ' + PROMPT + '\n' + TAIL), 'owner_draft')
        self.assertEqual(stuck('  ❯ ' + PROMPT + '\n' + FENCE, 'claude'), 'owner_draft')


class Turn(unittest.TestCase):
    def test_a_codex_working_row_above_a_separator_is_never_idle(self):
        # Finding N2.
        for rows in ('• Working (3s · esc to interrupt)\n────\n', 'Working (3s · esc to interrupt)\n\n────\n',
                     'Queued follow-up inputs\n• Working (5s · esc to interrupt)\n────\n', '• Working (3s)\n────\n',
                     '• Working (5h 01m 35…\n────\n'):
            screen = rows + '›\n' + TAIL
            self.assertIsNone(status(screen, 'codex'), screen)
            self.assertTrue(turn_showing(screen), screen)
            self.assertTrue(composer_empty(screen, 'codex'), screen)  # send_stop needs this

    def test_a_turn_is_never_an_unsent_prompt(self):
        # Finding N3: status was None on these, as on a quiet pane, so only classify can separate them.
        for screen, kind in ((SPINNER + '\n────\n❯ ' + PROMPT + '\n' + FENCE, 'claude'),
                             (SPINNER + '❯ ' + PROMPT + '\n' + FENCE, 'claude'),
                             ('• Working (3s · esc to interrupt)\n────\n› ' + PROMPT + '\n' + TAIL, 'codex'),
                             ('• Working (3s)\n────\n› ' + PROMPT + '\n' + TAIL, 'codex')):
            self.assertIsNone(refuse_dialog(screen), screen)
            self.assertEqual(stuck(screen, kind), 'owner_draft', screen)

    def test_a_finished_turn_does_not_hold_an_unsent_prompt(self):
        self.assertEqual(stuck('• Worked for 3s\n────\n› ' + PROMPT + '\n' + TAIL), 'unsent_prompt')
        self.assertEqual(stuck('────\n❯ ' + PROMPT + '\n' + FENCE, 'claude'), 'unsent_prompt')
        old = SPINNER + 'row\n' * 12 + '────\n❯ ' + PROMPT + '\n' + FENCE
        self.assertFalse(turn_showing(old))
        self.assertEqual(stuck(old, 'claude'), 'unsent_prompt')
        self.assertFalse(turn_showing('Some • Working row in a sentence\n› \n' + TAIL))

    def test_a_cut_or_wrapped_turn_row_is_not_idle_or_unsent(self):
        # Finding N5: a narrow pane cuts or wraps the row, and Herdr may still say idle. Any leading glyph, or none.
        for row in ('Working (5h 01m 35…', 'Working (3s · esc to int…', 'Working (3s · esc to\ninterrupt)',
                    '✻ Pondering… (12s · esc to\ninterrupt)', '✻ Pondering… (12s · esc\nto interrupt)',
                    '✻ Pondering… (12s · esc t\no interrupt)', '✻ Pondering… (12s · esc…', '✻ Pondering… (12s · e…',
                    '✻ Pondering… (12', '◦ Working (5h 01m 35…', '+ Working (5h', '• Wo…', 'Worki…', '• Thinking (5h 01m…',
                    # No running time, so each of these stands on its own part of the match.
                    'Working …', '+ Working', '✻ Pondering… (esc to int…', '✻ Pondering… (esc t\no interrupt)',
                    '✻ Pondering… (es\nc to interrupt)', '✻ Pondering… (esc…', '✻ Pondering… (esc t…', '✻ Pondering… (esc', '✻ Pondering… (ESC TO INTERRUPT)'):
            for rule in ('', '────\n', '---\n'):
                for kind, empty, typed in (('codex', '›\n' + TAIL, '› ' + PROMPT + '\n' + TAIL),
                                           ('claude', '────\n❯\n' + FENCE, '────\n❯ ' + PROMPT + '\n' + FENCE)):
                    self.assertTrue(turn_showing(row + '\n' + rule + empty), (row, rule))
                    self.assertNotEqual(status(row + '\n' + rule + empty, kind), 'idle', (row, rule, kind))
                    self.assertNotEqual(stuck(row + '\n' + rule + typed, kind), 'unsent_prompt', (row, rule, kind))

    def test_a_finished_row_is_not_a_cut_turn(self):
        for row in ('• Worked for 3s', '• Worked…', 'Done (3 of 4)', 'Some • Working row', 'Escaped to safety', 'use the esc key now',
                    'Wrote the report…', 'gpt-6-luna x…', 'Ran two checks (12s'):
            self.assertFalse(turn_showing(row + '\n› \n' + TAIL), row)

    def test_an_unreadable_screen_shows_a_turn(self):
        self.assertTrue(turn_showing('› \x07\n'))
        self.assertTrue(turn_showing(None))


class FooterRows(unittest.TestCase):
    def test_a_short_word_after_a_model_name_is_owner_text(self):
        # Findings I1 and N4.
        for word in ('wait', 'hold', 'stop', 'nope', 'no', 'a', 'lowly', 'mid'):
            row = 'gpt-6 ' + word + '…\n'
            self.assertIsNone(status('› \n' + row, 'codex'), row)
            self.assertFalse(composer_empty('› \n' + row, 'codex'), row)
            self.assertEqual(stuck('› ' + PROMPT + '\n' + row), 'owner_draft', row)

    def test_a_cut_effort_stays_a_footer(self):
        for effort in ('l', 'lo', 'low', 'm', 'me', 'med', 'medi', 'mediu', 'h', 'hi', 'hig', 'high', 'x', 'xh', 'xhi', 'xhig', 'xhigh'):
            row = 'gpt-6-luna ' + effort + '…\n'
            self.assertEqual(status('› \n' + row, 'codex'), 'idle', row)
            self.assertEqual(stuck('› ' + PROMPT + '\n' + row), 'unsent_prompt', row)


if __name__ == '__main__':
    unittest.main()
