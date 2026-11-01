"""Codex idle reads and footer reads. Screens are literals: some from the skill's tests, some from review probes."""
import unittest

from drover.pane_screen import classify, context_left, footer_model, status

AGENT = dict(pane_id='w1:p1', terminal_id='t1', name='fixture', agent='codex', agent_status='idle', focused=False)


def stuck(screen, item=None, agent=AGENT, now=0):
    return classify(agent, screen, item or {}, now)


class CodexIdle(unittest.TestCase):
    def test_current_footer_reads_as_idle_and_drafts_stay_retained(self):
        footer = ('  GPT-6.1-Sol high · ~/work/motif.worktrees/hx/order · Read briefs\n'
                  '  ? for shortcuts                  ⚠ 3 warnings · f2 to view\n'
                  '                                   ⚠ weekly limit: 22% left · resets at 09:35\n')
        self.assertEqual(status('OpenAI Codex\n› Ask Codex to do anything\n' + footer, 'codex'), 'idle')
        self.assertEqual(status('OpenAI Codex\n› \n' + footer, 'codex'), 'idle')
        self.assertIsNone(status('OpenAI Codex\n› owner is typing\n' + footer, 'codex'))
        self.assertIsNone(status('OpenAI Codex\n› \n  second line of a draft\n' + footer, 'codex'))

    def test_footer_with_thread_title_and_status_limit_notice_is_idle(self):
        screen = ('  Worked for 4m 4s \u2022 17:32\n'
                  '                                      \u26a0 weekly limit: 19% left \u00b7 /status\n\n'
                  '\u203a Ask Codex to do anything\n\n'
                  '  GPT-6.1 Terra xhigh \u00b7 ~/work/PanGloss.worktrees/pg-port-fixes \u00b7 Port HC fixes to Rust\n'
                  '  ? for shortcuts                       \u26a0 2 warnings \u00b7 f2 to view\n')
        self.assertEqual(status(screen, 'codex'), 'idle')
        self.assertIsNone(status(screen.replace('Ask Codex to do anything', 'my draft'), 'codex'))
        self.assertIsNone(status(screen.replace('\n\n  GPT-6.1', '\n  typed text\n  GPT-6.1'), 'codex'))

    def test_friendly_footers_are_idle_without_accepting_drafts(self):
        for label, model, effort in (('GPT-6.1 Terra', 'gpt-6.1-terra', 'xhigh'), ('GPT-6.1 Sol', 'gpt-6.1-sol', 'high')):
            with self.subTest(label=label):
                footer = f'  {label} {effort} · ~/work/motif.wo…\n  ? for shortcuts  ⚠ 1 · f2\n'
                screen = '› Ask Codex to do anything\n\n' + footer
                self.assertEqual(status(screen, 'codex'), 'idle')
                self.assertEqual(footer_model(screen), (model, effort))
                for location in ('/home/worker/repo', r'C:\work\repo', r'\\server\share\repo'):
                    located = f'› \n  {label} {effort} · {location}\n  ? for shortcuts\n'
                    self.assertEqual(status(located, 'codex'), 'idle')
                    self.assertEqual(footer_model(located), (model, effort))
                bare = f'› \n  {label} {effort}\n  ? for shortcuts\n'
                self.assertIsNone(status(bare, 'codex'))
                self.assertIsNone(footer_model(bare))
                for draft in ('owner is typing', '\n  second line of owner input'):
                    self.assertIsNone(status('› ' + draft + '\n' + footer, 'codex'))
                self.assertEqual(status('Allow once?\n› \n' + footer, 'codex'), 'blocked')
                self.assertIsNone(status('› \n' + footer + 'New activity · ^c copy · enter copy & follow · esc clear\n', 'codex'))
        for continuation in ('GPT-6.1 Terra xhigh · owner says stop', 'GPT-6.1 Sol high · inspect this first',
                             'GPT-6.1 Terra xhigh · ~/work owner says stop', 'GPT-6.1 Terra please fix my branch',
                             'GPT-6.1 Sol high do not start', 'GPT-6.1 Unknown high · ~/work'):
            self.assertIsNone(status('› \n' + continuation + '\n', 'codex'))

    def test_narrow_placeholder_reads_as_idle_and_a_short_prefix_as_a_draft(self):
        self.assertEqual(status('  Worked\n› Ask Cod\n  GPT-6.1…\n  ?   ⚠ 3\n', 'codex'), 'idle')
        self.assertEqual(status('is met.\n› Ask Code\n  GPT-6-Lu…\n  ⚠ 7 · f2\n', 'codex'), 'idle')
        self.assertIsNone(stuck('› Ask Cod\n  GPT-6.1…\n'))
        # Finding M3: the last three are real prefixes of the placeholder, with no cut footer to prove a narrow pane.
        for draft in ('Ask', 'Ask C', 'Ask Codex to fix it', 'Ask Codex to do anything now', 'Ask Codex', 'Ask Codex to do', 'Ask Codex…'):
            self.assertIsNone(status('› ' + draft + '\n? for shortcuts\n', 'codex'), draft)
            self.assertEqual(stuck('› ' + draft + '\n? for shortcuts\n'), 'owner_draft', draft)

    def test_footer_like_owner_continuations_are_not_idle(self):
        for text in ('gpt-6 please fix my branch', '? for the owner: hold', '⚠ do not run this'):
            self.assertIsNone(status('› \n' + text + '\n', 'codex'), text)

    def test_owner_screens_are_not_idle(self):
        for surface in ('› owner typed input', '› \nowner multiline input', 'Allow once?\n› ', '1 question\n› ',
                        'Trust this folder\n› ', 'not an agent input area', 'Permission request\nAllow once?\n',
                        'Codex is starting\n', 'Working (2s · esc to interrupt)\n› \n'):
            self.assertNotEqual(status(surface, 'codex'), 'idle', surface)

    def test_dialog_words_in_scrollback_do_not_block(self):
        scrollback = 'Research: 3 questions, update available, Trust this folder\n'
        for active in ('• Working …\n› \n? for shortcuts\n', 'Working (2s · esc to interrupt)\n› \n? for shortcuts\n'):
            screen = scrollback + '────────────\n' + active
            self.assertEqual(status(screen, 'codex'), 'working')
            self.assertIsNone(stuck(screen, now=100))
        screen = scrollback + '────────────\n› \n? for shortcuts\n'
        self.assertEqual(status(screen, 'codex'), 'idle')
        self.assertIsNone(stuck(screen, now=100))
        self.assertEqual(stuck(scrollback + '────────────\n2 questions\n'), 'question')
        # Deliberate change from the skill, finding I5: with no separator these rows may be a dialog, so they refuse.
        self.assertEqual(status(scrollback + '› \n? for shortcuts\n', 'codex'), 'blocked')

    def test_dialog_above_an_empty_composer_is_not_idle(self):
        tail = '› \n? for shortcuts\n'
        for rows in ('Please choose: allow once?', 'Allow\nonce?', 'Allow\u00a0once?', 'Ａllow once?',
                     'Allow once?\n' + 'detail\n' * 8):
            self.assertEqual(status(rows.rstrip('\n') + '\n' + tail, 'codex'), 'blocked', rows)
        for rows in ('Run rm -rf?\n  1. Approve\n  2. Reject', 'Run rm -rf build? [y/N]', 'Do you want to proceed?',
                     'Requires approval: rm -rf\n1. Yes\n2. No\n────', '› 1. Approve\n  2. Reject\n'):
            self.assertIsNone(status(rows + '\n' + tail, 'codex'), rows)
        self.assertIsNone(status('❯ 1. Approve\n  2. Reject\n\n❯\n⏵⏵ auto mode on\n', 'claude'))
        self.assertIsNone(status('Do you want to proceed?\n────\n❯\n────\n⏵⏵ auto mode on\n', 'claude'))
        self.assertEqual(status('Previous progress result: preserve approval/question UIs.\n' + tail, 'codex'), 'idle')


class FooterReads(unittest.TestCase):
    def test_footer_model_reads_under_the_last_composer(self):
        self.assertEqual(footer_model('› \n  GPT-6.1-Sol high · ~/w\n'), ('gpt-6.1-sol', 'high'))
        self.assertIsNone(footer_model('› \n  GPT-6.1…\n'))
        self.assertIsNone(footer_model('  GPT-6.1-Sol high · ~/w\n'))
        self.assertIsNone(footer_model('  GPT-6.1-Sol high · ~/w\n› \n? for shortcuts\n'))
        self.assertIsNone(footer_model('❯\n⏵⏵ auto mode on\n'))

    def test_context_left_reads_only_the_footer(self):
        self.assertEqual(context_left('› \n  42% context left\n  ? for shortcuts\n'), 42)
        self.assertEqual(context_left('› \n  gpt-6.1-sol high · 7% context left · ~/w\n'), 7)
        self.assertEqual(context_left('› \n  0% context left\n'), 0)
        self.assertEqual(context_left('› \n  100% context left\n'), 100)

    def test_context_left_is_unknown_without_a_footer_reading(self):
        for screen in ('5% context left\n› \n? for shortcuts\n', '› 5% context left\n', '› \n  owner says 5% context left\n',
                       '5% context left\n', '', '❯\n5% context left\n', '› \n  150% context left\n', '› \n  2.5% context left\n',
                       '› \n  gpt-6 · owner says 3% context left\n', '› \n  gpt-6 · -5% context left\n',
                       '› \n  42% cont…\n', '› \n  42% context left\n' + '  New activity · ^c copy · enter copy & follow · esc clear\n'):
            self.assertIsNone(context_left(screen), screen)


if __name__ == '__main__':
    unittest.main()
