"""What a pane screen says about its agent. Screens are literals: some from the skill's tests, some from review probes."""
import hashlib
import unittest

from drover import pane_screen
from drover.pane_screen import (
    classify,
    composer_empty,
    composer_text,
    context_left,
    footer_model,
    own_prompt,
    prompt_fingerprint,
    queued_idle,
    queued_prompt,
    status,
    viewport_class,
    viewport_held,
)

AGENT = dict(pane_id='w1:p1', terminal_id='t1', name='fixture', agent='codex', agent_status='idle', focused=False)
CLAUDE = dict(AGENT, agent='claude')
IDLE = 'OpenAI Codex\n› \n\n? for shortcuts\n'
HISTORICAL = ('• Working (1m • esc to interrupt)\n'
              '• Twelve of 13 test processes are complete.\n'
              '  Progress updated; the 30-minute watch continues.\n'
              '  Worked for 1m 30s • 17:16\n'
              '› check again - use herdr to have 30 minute checkins\n'
              '› Ask Codex to do anything\n'
              '  GPT-6.1-Sol high · /mnt/h/repos · Main [default] Goal stalled (/goal resume)\n'
              '  New activity · ^c copy · enter copy & follow · esc clear\n')
QUEUE = 'Queued follow-up inputs\n  ↳ note\n    shift+← edit last queued message\n'
TAIL = '› \n? for shortcuts\n'
WAKE = ('Progress timer for your owned scope /x. Check owned progress and evidence now. '
        'Update its progress token with a new check result.')


def scrolled(offset):
    return dict(pane=dict(scroll=dict(offset_from_bottom=offset)))


def stuck(screen, item=None, agent=AGENT, now=0):
    return classify(agent, screen, item or {}, now)


class Viewport(unittest.TestCase):
    def test_copy_viewport_is_held_not_working(self):
        self.assertTrue(viewport_held(HISTORICAL))
        self.assertTrue(viewport_held('❯\n────\nNew activity · ^c copy · enter copy & follow · esc clear\nrow\nrow\nrow\n'))
        self.assertTrue(viewport_held('Back to bottom · esc\nrow\nrow\n› \n? for shortcuts\n'))
        self.assertIsNone(status(HISTORICAL, 'codex'))
        self.assertEqual(stuck(HISTORICAL, dict(last_activity=0), now=900), 'viewport_held')
        self.assertFalse(queued_idle('Queued follow-up inputs\n↳ note\n' + HISTORICAL))
        self.assertFalse(composer_empty(HISTORICAL, 'codex'))
        self.assertIsNone(composer_text(HISTORICAL, 'codex'))
        self.assertFalse(composer_empty('› Ask Codex to do anything\n  New activity · enter/esc latest\n', 'codex'))
        self.assertIsNone(status('• Explored\n   New activity · ↓ Back to bottom · esc\n› Ask Codex to do anything\n'
                                 '  GPT-6-Luna xhigh · ~/work/x\n  New activity · enter/esc latest  ⚠ 6 · f2\n', 'codex'))
        self.assertIsNone(status('› Ask Codex to do anything\n  New activity · enter/esc latest · ? shortcuts   '
                                 '⚠ 7 warnings · f2 to view\n', 'codex'))

    def test_viewport_marker_in_transcript_is_not_held(self):
        screen = 'The old footer was New activity · ^c copy · enter copy & follow · esc clear\n› \n? for shortcuts\n'
        self.assertFalse(viewport_held(screen))
        self.assertEqual(status(screen, 'codex'), 'idle')

    def test_native_scroll_decides_the_viewport(self):
        self.assertIsNone(viewport_class(scrolled(0), IDLE))
        self.assertEqual(viewport_class(scrolled(4), IDLE), 'viewport_held')
        self.assertEqual(viewport_class(scrolled(0), HISTORICAL), 'viewport_held')

    def test_missing_or_invalid_native_scroll_is_unknown(self):
        snapshots = [None, {}, dict(pane={}), dict(pane=dict(scroll={})), dict(pane=None), dict(pane=dict(scroll=None)), []]
        snapshots += [scrolled(value) for value in (None, '0', -1, True, 0.0)]
        for snapshot in snapshots:
            self.assertEqual(viewport_class(snapshot, IDLE), 'viewport_unknown', snapshot)
        self.assertEqual(viewport_class(None, HISTORICAL), 'viewport_held')


class Working(unittest.TestCase):
    def test_visible_working_overrides_stale_idle(self):
        screen = '• Working (25s)\n› \n'
        self.assertEqual(AGENT['agent_status'], 'idle')
        self.assertEqual(status(screen, 'codex'), 'working')
        self.assertIsNone(stuck(screen, dict(last_activity=0), now=100))
        self.assertEqual(status('OpenAI Codex\nWorking (2s · esc to interrupt)\n› \n', 'codex'), 'working')

    def test_narrow_pane_working_row(self):
        for line in ('• Working …', '• Working (5h 01m 35…'):
            self.assertEqual(status(f'out\n{line}\n› \nGPT-6.1-…\n', 'codex'), 'working')
        self.assertIsNone(status('• Worked for 3s\n› draft', 'codex'))
        for footer in ('GPT-6.1-…', 'gpt-6-luna x…', '? fo…', '⚠ 7 · f2'):
            self.assertEqual(status(f'› \n\n{footer}\n', 'codex'), 'idle', footer)
        self.assertIsNone(status('› \nowner text\nGPT-6.1-…\n', 'codex'))


class ClaudeIdle(unittest.TestCase):
    def test_claude_uses_bottom_region_not_scrollback(self):
        self.assertEqual(status('Research: 3 questions; update available\n────────────\n❯\n⏵⏵ auto mode on\n', 'claude'), 'idle')
        self.assertEqual(status('Research done\n────────────\nAllow once?\n❯\n', 'claude'), 'blocked')
        self.assertEqual(status('• Working …\n❯\n', 'claude'), 'working')
        self.assertEqual(status('• Working …\nAllow once?\n❯\n', 'claude'), 'blocked')

    def test_claude_spinner_outside_the_bottom_region_is_never_idle(self):
        # Finding I3.
        spinner = '✻ Pondering… (12s · esc to interrupt)\n'
        for screen in (spinner + '\n────\n❯\n────\n⏵⏵ auto mode on\n', spinner + '❯\n⏵⏵ auto mode on\n'):
            self.assertIsNone(status(screen, 'claude'), screen)
            self.assertTrue(composer_empty(screen, 'claude'), screen)
        self.assertEqual(status('❯\n⏵⏵ auto mode on · esc to interrupt\n', 'claude'), 'working')
        old = spinner + 'row\n' * 12 + '────\n❯\n⏵⏵ auto mode on\n'
        self.assertEqual(status(old, 'claude'), 'idle')

    def test_unknown_claude_layout_fails_closed(self):
        for screen in ('❯\n⏵⏵ accept edits on\n', '❯\n? for shortcuts\n', '❯ draft\n⏵⏵ auto mode on\n', '❯\n---\n',
                       '❯\n⏵⏵ auto mode on but hold off\n',
                       '❯\n  second line of a draft\n', '❯\n⏵⏵ auto mode on\nowner text\n', 'Welcome to Claude Code\n', '$ '):
            self.assertIsNone(status(screen, 'claude'), screen)
            self.assertFalse(composer_empty(screen, 'claude'), screen)

    def test_claude_composer_text(self):
        self.assertEqual(composer_text('❯ my draft\n────\n⏵⏵ auto mode on\n', 'claude'), 'my draft')
        self.assertEqual(composer_text('❯\n────\n⏵⏵ auto mode on\n', 'claude'), '')
        self.assertTrue(composer_empty('❯\n────\n⏵⏵ auto mode on\n', 'claude'))
        self.assertIsNone(composer_text('no prompt here\n', 'claude'))
        self.assertEqual(stuck('❯ my draft\n⏵⏵ auto mode on\n', agent=CLAUDE), 'owner_draft')
        self.assertIsNone(stuck('❯\n⏵⏵ auto mode on\n', agent=CLAUDE))
        self.assertEqual(stuck('Allow once?\n❯\n', agent=CLAUDE), 'approval')

    def test_unknown_layout_is_none_for_both_kinds(self):
        for kind in ('claude', 'codex'):
            for screen in ('', '$ ', 'not an agent input area', 'Codex is starting\n', 'PS C:\\repo> '):
                self.assertIsNone(status(screen, kind), (kind, screen))
                self.assertIsNone(composer_text(screen, kind), (kind, screen))
                self.assertFalse(composer_empty(screen, kind), (kind, screen))
        self.assertIsNone(status('❯\n⏵⏵ auto mode on\n', 'codex'))
        self.assertFalse(composer_empty('❯\n⏵⏵ auto mode on\n', 'codex'))
        self.assertIsNone(status(IDLE, 'claude'))
        self.assertFalse(composer_empty(IDLE, 'claude'))
        for kind in ('gemini', '', None):
            self.assertIsNone(status(IDLE, kind))
            self.assertIsNone(composer_text(IDLE, kind))
            self.assertFalse(composer_empty(IDLE, kind))
            self.assertIsNone(status('Allow once?\n› \n', kind))


class Composer(unittest.TestCase):
    def test_composer_empty_follows_the_idle_read_not_the_first_line(self):
        self.assertTrue(composer_empty(IDLE, 'codex'))
        self.assertTrue(composer_empty('› Ask Cod\n  GPT-6.1…\n', 'codex'))
        self.assertTrue(composer_empty('Working (1s · esc to interrupt)\n› \n? for shortcuts\n', 'codex'))
        self.assertFalse(composer_empty('› \n\nowner text\n', 'codex'))
        self.assertFalse(composer_empty('› \nAsk Codex to do anything\n', 'codex'))
        self.assertFalse(composer_empty('› Ask\n', 'codex'))
        self.assertFalse(composer_empty('› 1. Trust and continue\n  2. Quit\n', 'codex'))

    def test_text_below_a_blank_row_belongs_to_the_composer(self):
        prompt = 'Progress timer for your owned scope /x. Check now.'
        digest, head = prompt_fingerprint(prompt)
        item = dict(last_prompt_digest=digest, last_prompt_head=head, last_prompt_at=0)
        screen = '› ' + prompt + '\n\nOwner says stop.\n? for shortcuts\n'
        self.assertEqual(composer_text(screen, 'codex'), prompt + ' Owner says stop.')
        self.assertFalse(own_prompt(composer_text(screen, 'codex'), digest, 0, 0))
        self.assertEqual(stuck(screen, item), 'owner_draft')
        self.assertEqual(stuck('› \n\nowner text\n? for shortcuts\n'), 'owner_draft')

    def test_a_mark_at_the_end_of_a_draft_is_not_an_empty_composer(self):
        # Finding C2: pasted terminal output often ends on a bare prompt.
        for screen in ('› check this:\n  ›\n? for shortcuts\n', '› check this:\n›\n? for shortcuts\n',
                       '› check this:\n  more\n  ›\n? for shortcuts\n', '› check this:\n\n›\n? for shortcuts\n',
                       '› check this:\n\n  more\n\n  ›\n? for shortcuts\n'):
            self.assertNotEqual(status(screen, 'codex'), 'idle', screen)
            self.assertFalse(composer_empty(screen, 'codex'), screen)
            self.assertIn('check this:', composer_text(screen, 'codex'), screen)
        for screen in ('────\n❯ run this in the shell:\n  ❯\n────\n⏵⏵ auto mode on\n',
                       '────\n❯ run this in the shell:\n❯\n────\n⏵⏵ auto mode on\n',
                       '────\n❯ run this in the shell:\n\n\n  more\n\n❯\n────\n⏵⏵ auto mode on\n'):
            self.assertNotEqual(status(screen, 'claude'), 'idle', screen)
            self.assertFalse(composer_empty(screen, 'claude'), screen)
            self.assertIn('run this in the shell:', composer_text(screen, 'claude'), screen)

    def test_a_bare_indented_mark_is_a_draft_row(self):
        for screen, kind in (('some output\n  ›\n? for shortcuts\n', 'codex'), ('  ❯\n⏵⏵ auto mode on\n', 'claude')):
            self.assertIsNone(status(screen, kind), screen)
            self.assertFalse(composer_empty(screen, kind), screen)

    def test_our_prompt_above_a_stacked_mark_is_not_ours(self):
        prompt = 'Read the brief now'
        digest = prompt_fingerprint(prompt)[0]
        item = dict(last_prompt_digest=digest, last_prompt_at=0)
        self.assertEqual(stuck('› check this:\n› ' + prompt + '\n? for shortcuts\n', item), 'owner_draft')
        self.assertEqual(stuck('› ' + prompt + '\n  ›\n? for shortcuts\n', item), 'owner_draft')

    def test_past_prompts_in_the_transcript_are_not_a_draft(self):
        self.assertEqual(status('› an earlier prompt\n\n• Done.\n\n› \n? for shortcuts\n', 'codex'), 'idle')
        # Deliberate change, finding N1: Claude's half read idle here. No Claude answer row is captured, so a separator was
        # all that ended its walk. Task 15's capture can give it a stop; test_pane_screen_walk pins the None until then.

    def test_footer_shaped_owner_text_stays_in_the_digest(self):
        # Finding I1. In the first screen the owner's row sat between our words, and the digest matched.
        prompt = 'Read the brief now'
        digest = prompt_fingerprint(prompt)[0]
        item = dict(last_prompt_digest=digest, last_prompt_at=0)
        for screen in ('› Read the\n  gpt-6 · no wait, use main\n  brief now\n? for shortcuts\n',
                       '› Read the\n  ? for shortcuts\n  brief now\n',
                       '› Read the\n  ────────\n  brief now\n',
                       '› Read the brief now\n  gpt-6 · no wait\n',
                       '› Read the brief now\n  gpt-6.1-sol\n',
                       '› Read the brief now\n---\n',
                       '› Read the brief now\n- - -\n',
                       '› Read the brief now\n- \n'):
            self.assertEqual(stuck(screen, item), 'owner_draft', screen)
        self.assertEqual(stuck('› ' + prompt + '\n\n  GPT-6.1-Sol high · ~/w\n  ? for shortcuts\n', item), 'unsent_prompt')
        self.assertEqual(stuck('❯ ' + prompt + '\n⏵⏵ auto mode on but hold off\n', dict(item), CLAUDE), 'owner_draft')

    def test_footer_shaped_owner_text_is_not_idle(self):
        for row in ('gpt-6 · owner says stop', 'gpt-6.1-sol', '---', '- ', '- - -', 'gpt-6 high · no wait', 'GPT-6-Luna xhigh'):
            self.assertIsNone(status('› \n' + row + '\n', 'codex'), row)
            self.assertFalse(composer_empty('› \n' + row + '\n', 'codex'), row)

    def test_observed_model_footers_stay_footers(self):
        for row in ('GPT-6.1-Sol high · /mnt/h/repos · Main [default] Goal stalled (/goal resume)',
                    'GPT-6-Luna xhigh · ~/work/x', 'gpt-6.1-terra xhigh · ~/w', 'gpt-6.1-sol high · 7% context left · ~/w',
                    'GPT-6.1-…', 'GPT-6.1…', 'GPT-6-Lu…', 'gpt-6-luna x…'):
            self.assertEqual(status('› \n' + row + '\n', 'codex'), 'idle', row)

    def test_collapsed_paste_is_pending_not_ours(self):
        brief = 'read the brief and end with a final report ' * 60
        digest, head = prompt_fingerprint(brief)
        for paste in ('[Pasted Content 2048 chars]', 'owner words [Pasted Content 2048 chars] more words'):
            screen = 'OpenAI Codex\n› ' + paste + '\n  GPT-6-Luna xhigh\n'
            item = dict(last_prompt_digest=digest, last_prompt_head=head, last_prompt_at=100)
            self.assertNotEqual(status(screen, 'codex'), 'idle')
            self.assertFalse(composer_empty(screen, 'codex'))
            self.assertIn(paste, composer_text(screen, 'codex'))
            self.assertFalse(own_prompt(paste, digest, 100, 101))
            self.assertEqual(stuck(screen, item, now=101), 'owner_draft')

    def test_own_prompt_needs_the_full_digest(self):
        prompt = 'Progress timer for your owned scope /x. Check now.'
        digest, head = prompt_fingerprint(prompt)
        self.assertEqual(digest, hashlib.sha256(prompt.encode('utf-8')).hexdigest())
        self.assertEqual(head, prompt[:60])
        item = dict(last_prompt_digest=digest, last_prompt_head=head, last_prompt_at=0)
        sent = '› ' + prompt + '\n\n? for shortcuts\n'
        self.assertEqual(stuck(sent, item), 'unsent_prompt')
        self.assertTrue(own_prompt('  Progress timer for your owned   scope /x.\n Check now. ', digest, 0, 0))
        self.assertEqual(stuck('› fix the build please\n', item), 'owner_draft')
        self.assertEqual(stuck(sent, {}), 'owner_draft')
        # Deliberate change from the skill's test: a collapsed paste within 15 minutes of a send was unsent_prompt there.
        # Rule 3, SKILL.md:42 and references/supervision.md:172 make any opaque paste an owner draft, at any age.
        for now in (60, pane_screen.STALE + 1):
            self.assertEqual(stuck('› [Pasted Content 1200 chars]\n', item, now=now), 'owner_draft')
        self.assertEqual(stuck('› [Pasted Content 1200 chars]\n', {}), 'owner_draft')
        for text, digest_value in ((prompt, None), (prompt, ''), (None, digest), ('', digest)):
            self.assertFalse(own_prompt(text, digest_value, 0, 0))
        self.assertFalse(own_prompt(head[:20], digest, 0, 0))

    def test_prefix_of_our_prompt_is_a_draft(self):
        prompt = 'Progress timer for your owned scope /x. Check now.'
        digest, head = prompt_fingerprint(prompt)
        item = dict(last_prompt_digest=digest, last_prompt_head=head, last_prompt_at=0)
        self.assertEqual(stuck('› ' + prompt + ' Owner says stop.\n', item), 'owner_draft')
        self.assertEqual(stuck('› ' + prompt[:30] + '\n', item), 'owner_draft')


class Queue(unittest.TestCase):
    def test_queued_wake_is_recognised_only_for_its_scope(self):
        ours = '• Working (300s …)\nQueued follow-up inputs\n↳ ' + WAKE + '\n' + 'queued wrap\n' * 12 + \
            'shift+← edit last queued message\n› \n? for shortcuts\n'
        self.assertTrue(queued_prompt(ours, WAKE))
        self.assertEqual(status(ours, 'codex'), 'working')
        wrapped = WAKE[:75] + '\n' + WAKE[75:]
        screen = '• Working (17m …)\n↳ ' + wrapped + '\nshift+← edit last queued message\n› \n'
        self.assertTrue(queued_prompt(screen, WAKE))
        self.assertEqual(status(screen, 'codex'), 'working')
        self.assertFalse(queued_prompt('Queued follow-up inputs\n↳ another task\nshift+← edit last queued message\n', WAKE))
        self.assertFalse(queued_prompt('Queued follow-up inputs\n↳ [Pasted Content 1000 chars]\n', WAKE))
        self.assertFalse(queued_prompt('ordinary output quotes ' + WAKE, WAKE))

    def test_a_longer_scope_is_another_scope(self):
        other = WAKE.replace('/x.', '/x2.')
        screen = 'Queued follow-up inputs\n↳ ' + other + '\nshift+← edit last queued message\n› \n'
        self.assertFalse(queued_prompt(screen, WAKE))
        self.assertTrue(queued_prompt(screen, other))

    def test_a_scope_inside_another_scope_is_another_scope(self):
        # Finding M1.
        for other in ('/x.y', '/x.worktrees/dip'):
            screen = 'Queued follow-up inputs\n↳ ' + WAKE.replace('/x.', other + '.', 1) + '\nshift+← edit last queued message\n› \n'
            self.assertFalse(queued_prompt(screen, WAKE), other)

    def test_queued_wake_needs_its_listed_reports(self):
        prompt = WAKE + ' Worker completion reports ready for your review: ["/x/report.md"].'
        self.assertFalse(queued_prompt('Queued follow-up inputs\n↳ ' + WAKE + '\n', prompt))
        self.assertTrue(queued_prompt('Queued follow-up inputs\n↳ ' + prompt + '\n', prompt))

    def test_empty_prompt_and_held_viewport_prove_nothing(self):
        self.assertFalse(queued_prompt('Queued follow-up inputs\n↳ a\n', ''))
        self.assertFalse(queued_prompt('Queued follow-up inputs\n↳ ' + WAKE + '\n' + HISTORICAL, WAKE))

    def test_fixed_text_prompt_is_matched_whole(self):
        text = 'Drover: new notices. Read your inbox and commit a checkpoint.'
        self.assertTrue(queued_prompt('Queued follow-up inputs\n↳ ' + text + '\n', text))
        self.assertFalse(queued_prompt('Queued follow-up inputs\n↳ Drover: new notices.\n', text))

    def test_queue_under_a_running_turn_or_a_drained_queue_is_not_wedged(self):
        self.assertFalse(queued_idle('• Working (9m • esc to interrupt)\n' + QUEUE + TAIL))
        self.assertFalse(queued_idle('done\n' + TAIL))
        self.assertFalse(queued_idle(QUEUE + '1. Switch to gpt-6-luna\n2. Keep current model\nenter select · esc back\n'))
        self.assertTrue(queued_idle('• Worked for 1m\n' + QUEUE + TAIL))
        item = dict(queued_since=0)
        self.assertIsNone(stuck(QUEUE + TAIL, item, now=pane_screen.WEDGE - 1))
        self.assertEqual(stuck(QUEUE + TAIL, item, now=pane_screen.WEDGE), 'queued_wedged')

    def test_idle_with_a_queue_that_never_drains_is_wedged(self):
        wedged = ('• Worked for 2m\nQueued follow-up inputs\n  ↳ ' + WAKE + '\n  ↳ motif-harness: rebase\n'
                  '    shift+← edit last queued message\n› Ask Codex to do anything\n  GPT-6.1-Sol high · ~/w\n  ? for shortcuts\n')
        self.assertTrue(queued_prompt(wedged, WAKE))
        self.assertEqual(stuck(wedged, dict(queued_since=301), now=301 + pane_screen.WEDGE), 'queued_wedged')


class Classify(unittest.TestCase):
    def test_each_stuck_class(self):
        item = dict(last_activity=0)
        self.assertEqual(stuck('', agent=None), 'exited')
        self.assertEqual(stuck("■ You've hit your usage limit.\n› \n"), 'usage_limit')
        self.assertIsNone(stuck('less than 5% weekly limit left\n› \n? for shortcuts\n'))
        self.assertEqual(stuck('Trust this folder?\n'), 'trust')
        self.assertEqual(stuck('Allow once?\n'), 'approval')
        self.assertEqual(stuck('2 questions\n'), 'question')
        self.assertEqual(stuck('turn\nstream disconnected\n› \n? for shortcuts\n'), 'error_turn')
        busy = 'Working (1s · esc to interrupt)\n› \n'
        working = dict(AGENT, agent_status='working')
        self.assertIsNone(stuck(busy, item, working, 100))
        self.assertEqual(stuck(busy, item, working, pane_screen.STALE), 'stale_working')
        for cls in ('wake_unverified', 'scope_stale'):
            self.assertEqual(stuck('› \n? for shortcuts\n', dict(state='paused', pause_class=cls)), cls)

    def test_refused_dialog_screen_is_never_unsent_prompt(self):
        # Finding I6.
        prompt = 'our prompt'
        item = dict(last_prompt_digest=prompt_fingerprint(prompt)[0], last_prompt_at=0)
        for row in ('enter select', 'esc back', '1. Yes, proceed\n2. No', 'Press enter to continue', 'Update available'):
            screen = row + '\n› ' + prompt + '\n'
            self.assertEqual(status(screen, 'codex'), 'blocked', row)
            self.assertNotEqual(stuck(screen, item), 'unsent_prompt', row)
        self.assertEqual(stuck('› ' + prompt + '\n', item), 'unsent_prompt')
        self.assertEqual(stuck('Do you want to proceed?\n› ' + prompt + '\n', item), 'owner_draft')

    def test_model_prompt_is_owner_only(self):
        screen = '1. Switch to gpt-6-luna\n2. Keep current model\n3. Keep current model (never show again)\nenter select · esc back\n› \n'
        self.assertEqual(status(screen, 'codex'), 'blocked')
        self.assertEqual(stuck(screen), 'model_prompt')
        spent = ('■ You’ve hit your usage limit. Visit\nhttps://chatgpt.com/settings/usage to\npurchase more credits or try again at Oct\n'
                 '21st, 2026 10:05 AM.\n  Approaching rate limits\n  Switch to gpt-6-luna for lower credit\n  usage?\n'
                 '› 1. Switch to gpt-6-luna\n  2. Keep current model\n  3. Keep current model (never show again)\n  enter select · esc back\n')
        self.assertEqual(stuck(spent), 'model_prompt')

    def test_strong_modal_is_blocked_despite_a_working_row(self):
        screen = '• Working …\nAllow once?\n› \n'
        self.assertEqual(status(screen, 'codex'), 'blocked')
        self.assertEqual(stuck(screen), 'approval')

    def test_active_rate_limit_is_distinct_from_a_passive_warning(self):
        self.assertEqual(stuck('■ Rate limit reached. Try again in five minutes.\n› \n'), 'rate_limit')
        self.assertIsNone(stuck('less than 5% weekly limit left\n› \n? for shortcuts\n'))

    def test_old_limit_scrollback_is_ignored_but_a_recent_refusal_is_kept(self):
        for refusal, cls in (("■ You've hit your usage limit.", 'usage_limit'), ('■ Rate limit reached.', 'rate_limit')):
            old = refusal + '\n' + 'ordinary output\n' * 20 + '────────────\n› \n? for shortcuts\n'
            self.assertIsNone(stuck(old), cls)
            self.assertEqual(stuck(refusal + '\n────────────\n› \n? for shortcuts\n'), cls)
            self.assertEqual(stuck('old output\n────────────\n' + refusal + '\n› \n'), cls)

    def test_process_classes_are_not_read_from_the_screen(self):
        self.assertNotIn('waiting_on_dead_process', pane_screen.STUCK_CLASSES)
        self.assertIsNone(stuck('Gate queued for a test slot.\n› \n? for shortcuts\n'))


class Bytes(unittest.TestCase):
    def test_bytes_from_herdr_decode_as_utf8(self):
        raw = '› \n  42% context left\n  GPT-6.1-Sol high · ~/w\n  ? for shortcuts\n'.encode('utf-8')
        self.assertEqual(status(raw, 'codex'), 'idle')
        self.assertTrue(composer_empty(raw, 'codex'))
        self.assertEqual(composer_text(raw, 'codex'), '')
        self.assertEqual(footer_model(raw), ('gpt-6.1-sol', 'high'))
        self.assertEqual(context_left(raw), 42)
        self.assertIsNone(viewport_class(scrolled(0), raw))
        self.assertEqual(status('❯\n⏵⏵ auto mode on\n'.encode('utf-8'), 'claude'), 'idle')
        self.assertTrue(viewport_held(HISTORICAL.encode('utf-8')))
        self.assertEqual(status('› \r\n? for shortcuts\r\n', 'codex'), 'idle')
        self.assertEqual(classify(AGENT, 'Allow once?\n› \n'.encode('utf-8'), {}, 0), 'approval')
        queue = ('• Worked for 1m\n' + QUEUE + TAIL).encode('utf-8')
        self.assertTrue(queued_idle(queue))
        self.assertTrue(queued_prompt(('Queued follow-up inputs\n↳ ' + WAKE + '\n').encode('utf-8'), WAKE))
        digest = prompt_fingerprint('naïve › prompt')[0]
        self.assertTrue(own_prompt('naïve › prompt'.encode('utf-8'), digest, 0, 0))

    def test_unreadable_capture_is_unknown_everywhere(self):
        # Finding I4: a screen that cannot be read exactly gives every reader's non-permitting answer.
        for screen in (b'\xffAllow once?\n\xe2\x80\xba \n? for shortcuts\n', '\ufffdAllow once?\n› \n? for shortcuts\n',
                       '\x1b[1mAllow once?\x1b[0m\n› \n? for shortcuts\n', '\x1b[2m• Working (3s)\x1b[0m\n› \n',
                       '\x07Allow once?\n› \n', '\x1b]0;x\x07Would you like to run this?\n› \n', '› owner draft\r› \n',
                       '› \x0c\n', '› \x0b\n', '› \u2028\n', '› \u2029\n', None):
            for kind in ('codex', 'claude'):
                self.assertIsNone(status(screen, kind), repr(screen))
                self.assertIsNone(composer_text(screen, kind), repr(screen))
                self.assertFalse(composer_empty(screen, kind), repr(screen))
            self.assertTrue(viewport_held(screen), repr(screen))
            self.assertEqual(viewport_class(scrolled(0), screen), 'viewport_unknown', repr(screen))
            self.assertFalse(queued_idle(screen), repr(screen))
            self.assertFalse(queued_prompt(screen, WAKE), repr(screen))
            self.assertIsNone(footer_model(screen), repr(screen))
            self.assertIsNone(context_left(screen), repr(screen))
            self.assertEqual(stuck(screen), 'screen_unknown', repr(screen))
        digest = prompt_fingerprint('Check now.')[0]
        self.assertFalse(own_prompt('Check now.\x07', digest, 0, 0))
        self.assertFalse(own_prompt(b'\xff', digest, 0, 0))


if __name__ == '__main__':
    unittest.main()
