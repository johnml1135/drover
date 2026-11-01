"""The one guarded path that writes to a pane. Every test runs on the fake: nothing here can reach a real Herdr."""
import ast
from unittest import mock

from drover import compaction_hold, pane_screen, pane_send
from drover.ledger import ops_panes
from drover.pane_send import PaneRefused
from tests.pane_send_support import (CLAUDE_IDLE, COPY, Case, DIALOG, DRAFT, ENTER, IDLE, MODEL, PANE, PASTE, ROOT, SUSPECT,
                                     TEXT, UNREADABLE, WORKING, WRITE_VERBS, composing, sources)


class Check(Case):
    def test_pane_without_a_drover_row_gets_mail_not_input(self):
        self.windows()
        self.refused(lambda: pane_send.send(self.port, self.herdr, None, TEXT, 'wake'), 'no-row')
        self.assertEqual(self.fake.calls, [])
        self.conn.execute("UPDATE pane_agents SET started_by='owner'")
        for act in (self.send, lambda: pane_send.interrupt(self.port, self.herdr, self.row),
                    lambda: pane_send.submit_own(self.port, self.herdr, self.row)):
            self.refused(act, 'not-drovers')
        self.conn.execute("UPDATE pane_agents SET started_by='drover', state='gone'")
        self.refused(self.send, 'no-row')
        self.assertEqual(self.fake.calls, [])

    def test_a_stale_row_is_read_again_before_any_write(self):
        self.windows()
        ops_panes.update_agent(self.conn, self.row['id'], state='retired')
        self.refused(self.send, 'not-live')
        with self.assertRaises(PaneRefused):
            pane_send.check(self.herdr, {**self.row, 'state': 'retired'})
        with self.assertRaises(PaneRefused):
            pane_send.check(self.herdr, None)

    def test_requires_row_unfocused_idle_empty_and_unchanged(self):
        cases = {'focused': dict(agent={'focused': True}), 'focus unknown': dict(agent={'focused': None}),
                 'pane focused': dict(pane={'focused': True}), 'working': dict(agent={'agent_status': 'working'}),
                 'blocked': dict(agent={'agent_status': 'blocked'}), 'unknown': dict(agent={'agent_status': 'unknown'}),
                 'no status': dict(agent={'agent_status': None}), 'not ready': dict(agent={'interactive_ready': False}),
                 'terminal': dict(agent={'terminal_id': 't2'}), 'session': dict(agent={'agent_session': {'value': 's2'}}),
                 'no session': dict(agent={'agent_session': None}), 'pane': dict(agent={'pane_id': 'w1:p2'}),
                 'kind': dict(agent={'agent': 'codex'}), 'row without session': dict(row={'session_id': None}),
                 'row without terminal': dict(row={'terminal_id': None}),
                 'neither knows the session': dict(row={'session_id': None}, agent={'agent_session': None}),
                 'neither knows the terminal': dict(row={'terminal_id': None}, agent={'terminal_id': None})}
        for name, over in cases.items():
            with self.subTest(name):
                self.windows(**over)
                self.refused(self.send)
                self.conn.execute('DELETE FROM pane_agents')

    def test_check_returns_what_it_read(self):
        self.windows()
        seen = pane_send.check(self.herdr, self.row)
        self.assertEqual(set(seen), {'agent', 'pane', 'screen'})
        self.assertEqual(seen['screen'], CLAUDE_IDLE)
        self.assertEqual(seen['pane']['pane']['scroll'], {'offset_from_bottom': 0})

    def test_a_done_agent_is_ready_for_input(self):
        self.windows(agent={'agent_status': 'done'})
        self.assertEqual(self.send()['state'], 'sent')

    def test_busy_blocked_unknown_and_draft_screens_send_nothing(self):
        for name, screen in dict(working=WORKING, dialog=DIALOG, model=MODEL, draft=DRAFT, suspect=SUSPECT, blank='',
                                 shell='PS C:\\>').items():
            with self.subTest(name):
                self.stage(screen)
                self.refused(self.send)
                self.conn.execute('DELETE FROM pane_agents')
        self.assertIsNone(ops_panes.last_write(self.conn, 1))

    def test_unreadable_capture_is_a_refusal(self):
        self.stage(UNREADABLE)
        self.refused(self.send, 'screen-unreadable')
        self.refused(lambda: self.stop(hold=None))
        self.refused(lambda: pane_send.interrupt(self.port, self.herdr, self.row), 'screen-unreadable')
        self.refused(lambda: pane_send.check(self.herdr, self.row), 'screen-unreadable')

    def test_owner_draft_on_last_read_sends_nothing(self):
        fake = self.stage()
        fake.after[('pane', 'read', 2)] = lambda: fake.screens.update({PANE: DRAFT})
        self.refused(self.send)
        self.assertEqual(fake.seen[('pane', 'read')], 3)
        self.assertEqual(self.last()['state'], 'unsent')

    def test_each_screen_is_refused_for_its_own_reason(self):
        spinner = 'history\n✢ Thinking… (3s · esc to interrupt)\n───\n❯ \n───\n'
        for name, screen, kind, reason in (('turn', WORKING, 'codex', 'busy'), ('draft', DRAFT, 'codex', 'draft'),
                                           ('spinner over an empty composer', spinner, 'claude', 'screen-not-idle')):
            with self.subTest(name):
                self.stage(screen, kind=kind)
                self.refused(self.send, reason)
                self.conn.execute('DELETE FROM pane_agents')

    def test_a_screen_that_moved_between_reads_sends_nothing(self):
        self.stage([IDLE, 'more history\n\n› \n\n? for shortcuts\n'])
        self.refused(self.send, 'screen-changed')

    def test_scrolled_or_unknown_viewport_sends_nothing(self):
        for name, pane in dict(scrolled={'scroll': {'offset_from_bottom': 3}}, missing={'scroll': None},
                               bad={'scroll': {'offset_from_bottom': '0'}}).items():
            with self.subTest(name):
                self.stage(pane=pane)
                self.refused(self.send)
                self.conn.execute('DELETE FROM pane_agents')
        self.stage(COPY)
        self.refused(self.send, 'viewport_held')

    def test_viewport_is_checked_again_just_before_send(self):
        fake = self.stage()
        fake.after[('pane', 'get', 2)] = lambda: fake.panes[PANE].update(scroll={'offset_from_bottom': 2})
        self.refused(self.send, 'viewport_held')
        self.assertEqual(self.last()['state'], 'unsent')

    def test_occupant_change_between_reads_sends_nothing(self):
        for name, field in dict(terminal={'terminal_id': 't2'}, session={'agent_session': {'value': 's2'}}).items():
            with self.subTest(name):
                fake = self.stage()
                fake.after[('agent', 'get', 1)] = lambda: fake.agents[PANE].update(field)
                self.refused(self.send, 'occupant-changed')
                self.conn.execute('DELETE FROM pane_agents')

    def test_no_option_clears_a_dialog(self):
        for screen in (DIALOG, MODEL):
            self.stage(screen)
            self.refused(self.send, 'dialog')
            self.conn.execute('DELETE FROM pane_agents')

    def test_herdr_unavailable_is_a_refusal_not_an_exception(self):
        for name, breaks in dict(closed=lambda f: setattr(f, 'closed', True), hang=lambda f: setattr(f, 'hang', True),
                                 refused=lambda f: f.refuse.update({('agent', 'get'): 'agent_not_found'})).items():
            with self.subTest(name):
                breaks(self.stage())
                for act in (self.send, lambda: pane_send.check(self.herdr, self.row),
                            lambda: pane_send.interrupt(self.port, self.herdr, self.row)):
                    self.refused(act)
                self.conn.execute('DELETE FROM pane_agents')

    def test_only_an_unfocused_idle_empty_composer_is_written(self):
        self.stage()
        self.paste_settles_to(composing(TEXT))
        self.assertEqual(self.send()['state'], 'sent')
        self.conn.execute('DELETE FROM pane_agents')
        self.stage(agent={'focused': True})
        self.refused(self.send, 'focused')

    def test_busy_agent_gets_no_follow_up(self):
        self.windows(agent={'agent_status': 'working'})
        self.refused(self.send, 'busy')


class Send(Case):
    def test_windows_agent_gets_one_prompt_and_no_enter(self):
        self.windows()
        write = self.send()
        self.assertEqual(self.sends(), [['agent', 'prompt', PANE, TEXT]])
        self.assertEqual((write['state'], write['kind']), ('sent', 'wake'))
        self.sleep.assert_not_called()

    def test_wsl_agent_gets_one_bracketed_paste_then_enter(self):
        self.stage()
        self.paste_settles_to(composing(TEXT))
        write = self.send()
        self.assertEqual(self.sends(), [PASTE, ENTER])
        self.sleep.assert_called_once_with(0.8)
        self.assertEqual(write['state'], 'sent')

    def test_prompts_never_use_wait(self):
        self.windows()
        self.send()
        self.assertEqual([c for c in self.fake.calls if '--wait' in c], [])

    def test_targets_are_explicit_pane_ids(self):
        self.stage()
        self.paste_settles_to(composing(TEXT))
        self.send()
        for call in self.fake.calls:
            self.assertEqual(call[2], PANE, call)
        self.assertFalse([c for c in self.fake.calls if '--current' in c or '--focused' in c])

    def test_screens_are_read_as_text_never_ansi(self):
        self.stage()
        pane_send.check(self.herdr, self.row)
        self.assertIn(['pane', 'read', PANE, '--source', 'visible'], self.fake.calls)
        self.assertFalse([c for c in self.fake.calls if 'ansi' in c])

    def test_intent_is_recorded_before_the_write(self):
        self.windows()
        state = []
        real = self.fake._answer

        def answer(args):
            if tuple(args[:2]) in WRITE_VERBS:
                state.append(self.last()['state'])
            return real(args)
        self.fake._answer = answer
        self.send()
        self.assertEqual(state, ['pending'])
        self.assertEqual(self.last()['digest'], pane_screen.prompt_fingerprint(TEXT)[0])

    def test_focus_between_final_read_and_send_sends_nothing(self):
        for name, placement in (('windows', self.windows), ('wsl', self.stage)):
            with self.subTest(name):
                fake = placement()
                real = ops_panes.note_write

                def noted(*args):
                    write = real(*args)
                    fake.agents[PANE]['focused'] = True
                    return write
                with mock.patch.object(ops_panes, 'note_write', noted):
                    self.refused(self.send, 'focused')
                self.assertEqual(self.last()['state'], 'unsent')
                self.conn.execute('DELETE FROM pane_agents')

    def test_typing_between_paste_and_enter_withholds_the_enter_and_never_resends(self):
        self.stage()
        self.paste_settles_to(composing(TEXT + ' and the owner kept typing'))
        write = self.send()
        self.assertEqual(self.sends(), [PASTE])
        self.assertEqual((write['state'], write['reason']), ('unverified', 'not-own'))

    def test_focus_after_the_paste_withholds_the_enter(self):
        fake = self.stage()
        self.sleep.side_effect = lambda seconds: (fake.screens.update({PANE: composing(TEXT)}),
                                                  fake.agents[PANE].update(focused=True))
        self.assertEqual(self.send()['reason'], 'focused')
        self.assertEqual(self.sends(), [PASTE])

    def test_dialog_after_paste_stops_the_enter(self):
        for name, screen in dict(dialog=DIALOG, model=MODEL, suspect='Sure?\n\n' + composing(TEXT)[9:]).items():
            with self.subTest(name):
                self.stage()
                self.paste_settles_to(screen)
                self.assertEqual(self.send()['state'], 'unverified')
                self.assertEqual(self.sends(), [PASTE])
                self.conn.execute('DELETE FROM pane_agents')

    def test_a_turn_seen_after_the_paste_stops_the_enter(self):
        spinner = f'✢ Thinking… (3s · esc to interrupt)\n───\n❯ {TEXT}\n───\n'
        for name, screen, status in (('working row', '• Working (2s)\n\n' + composing(TEXT)[9:], 'idle'),
                                     ('claude spinner', spinner, 'idle'),
                                     ('herdr working', composing(TEXT), 'working')):
            with self.subTest(name):
                fake = self.windows() if 'claude' in name else self.stage()
                self.conn.execute("UPDATE pane_agents SET placement='wsl'")
                self.sleep.side_effect = lambda seconds: (fake.screens.update({PANE: screen}),
                                                          fake.agents[PANE].update(agent_status=status))
                self.assertEqual(self.send()['state'], 'unverified')
                self.assertEqual(self.sends(), [PASTE])
                self.conn.execute('DELETE FROM pane_agents')

    def test_collapsed_paste_gets_no_enter(self):
        self.stage()
        self.paste_settles_to(composing('[Pasted Content 1200 chars]'))
        self.assertEqual(self.send()['reason'], 'not-own')
        self.assertEqual(self.sends(), [PASTE])

    def test_stalled_send_is_read_not_resent(self):
        for name, stage, pair in (('windows', self.windows, ('agent', 'prompt')), ('wsl', self.stage, ('pane', 'send-text'))):
            with self.subTest(name):
                fake = stage()
                fake.hang = {pair}
                write = self.send()
                self.assertEqual(write['state'], 'unverified')
                self.assertEqual(len(self.sends()), 1)
                self.conn.execute('DELETE FROM pane_agents')
        self.sleep.assert_not_called()


class SubmitOwn(Case):
    def pasted(self, screen):
        self.stage()
        self.paste_settles_to(screen)
        self.send()
        self.fake.calls.clear()

    def submit(self):
        return pane_send.submit_own(self.port, self.herdr, self.row)

    def test_unsent_own_prompt_gets_one_enter(self):
        self.pasted(composing(TEXT + ' typed'))
        self.fake.screens[PANE] = composing(TEXT)
        self.assertEqual(self.submit()['state'], 'sent')
        self.assertEqual(self.sends(), [ENTER])
        self.refused_again()

    def refused_again(self):
        self.fake.calls.clear()
        self.refused(self.submit, 'nothing-unsent')

    def test_a_stalled_paste_is_finished_only_after_a_read(self):
        fake = self.stage()
        fake.hang = {('pane', 'send-text')}
        self.send()
        fake.hang = False
        fake.screens[PANE] = composing(TEXT)
        self.submit()
        self.assertEqual(self.sends().count(ENTER), 1)

    def test_owner_text_or_nothing_gets_no_enter(self):
        for name, screen in dict(draft=composing('my own words'), prefix=composing(TEXT[:30]), empty=IDLE,
                                 collapsed=composing('[Pasted Content 900 chars]')).items():
            with self.subTest(name):
                self.pasted(composing(TEXT + ' typed'))
                self.fake.screens[PANE] = screen
                self.refused(self.submit, 'not-own')
                self.conn.execute('DELETE FROM pane_agents')

    def test_a_status_that_is_not_known_and_ready_refuses(self):
        for status in (None, '', 'unknown', 'working', 'blocked', 'paused', 7):
            with self.subTest(status=status):
                self.pasted(composing(TEXT + ' typed'))
                self.fake.screens[PANE] = composing(TEXT)
                self.fake.agents[PANE]['agent_status'] = status
                self.refused(self.submit, 'busy')
                self.conn.execute('DELETE FROM pane_agents')
        self.pasted(composing(TEXT + ' typed'))
        self.fake.screens[PANE] = composing(TEXT)
        del self.fake.agents[PANE]['agent_status']
        self.refused(self.submit, 'busy')

    def test_it_never_acts_on_classify_alone(self):
        for name, screen, agent in (('dialog', DIALOG, {}), ('model', MODEL, {}), ('unreadable', UNREADABLE, {}),
                                    ('turn', '• Working (2s)\n\n' + composing(TEXT)[9:], {}),
                                    ('suspect', 'Sure?\n\n' + composing(TEXT)[9:], {}),
                                    ('herdr working', composing(TEXT), {'agent_status': 'working'}),
                                    ('focused', composing(TEXT), {'focused': True})):
            with self.subTest(name):
                self.pasted(composing(TEXT + ' typed'))
                self.fake.screens[PANE] = screen
                self.fake.agents[PANE].update(agent)
                with mock.patch.object(pane_screen, 'classify', return_value='unsent_prompt'):
                    self.refused(self.submit)
                self.conn.execute('DELETE FROM pane_agents')

    def test_a_refused_submit_can_be_tried_on_a_later_read(self):
        self.pasted(composing(TEXT + ' typed'))
        self.fake.agents[PANE]['focused'] = True
        self.fake.screens[PANE] = composing(TEXT)
        self.refused(self.submit, 'focused')
        self.fake.agents[PANE]['focused'] = False
        self.assertEqual(self.submit()['state'], 'sent')

    def test_no_write_or_a_write_that_never_went_out_gets_no_enter(self):
        self.stage(composing(TEXT))
        self.refused(self.submit, 'nothing-unsent')
        write = ops_panes.note_write(self.conn, self.row['id'], 'wake', TEXT)
        self.refused(self.submit, 'nothing-unsent')
        ops_panes.finish_write(self.conn, write['id'], 'unsent', 'focused')
        self.refused(self.submit, 'nothing-unsent')


class Interrupt(Case):
    def act(self, row=None):
        return pane_send.interrupt(self.port, self.herdr, row or self.row)

    def test_interrupt_is_one_ctrl_c_to_an_own_pane(self):
        self.stage(WORKING, agent={'agent_status': 'working'})
        write = self.act()
        self.assertEqual(self.sends(), [['pane', 'send-keys', PANE, 'ctrl+c']])
        self.assertEqual((write['state'], write['kind']), ('sent', 'interrupt'))

    def test_any_dialog_or_draft_or_held_viewport_is_left_alone(self):
        for name, screen in dict(dialog=DIALOG, model=MODEL, suspect=SUSPECT, draft=DRAFT, copy=COPY).items():
            with self.subTest(name):
                self.stage(screen)
                self.refused(self.act)
                self.assertIsNone(self.last())
                self.conn.execute('DELETE FROM pane_agents')

    def test_a_live_row_keeps_the_focus_rule(self):
        self.stage(WORKING, agent={'focused': True})
        self.refused(self.act, 'focused')

    def test_only_a_compaction_row_under_a_hold_is_interrupted_while_focused(self):
        self.stage(WORKING, agent={'focused': True}, row={'state': 'compaction'})
        self.refused(self.act, 'focused')
        with mock.patch.object(compaction_hold, 'active', return_value={'id': 'h1'}):
            self.assertEqual(self.act()['state'], 'sent')

    def test_a_hold_alone_does_not_reach_a_focused_live_or_windows_pane(self):
        with mock.patch.object(compaction_hold, 'active', return_value={'id': 'h1'}):
            self.stage(WORKING, agent={'focused': True})
            self.refused(self.act, 'focused')
            self.conn.execute('DELETE FROM pane_agents')
            self.stage(WORKING, agent={'focused': True}, placement='windows', row={'state': 'compaction'})
            self.refused(self.act, 'focused')

    def test_a_key_that_stalls_is_unverified_and_not_repeated(self):
        fake = self.stage(WORKING)
        fake.hang = {('pane', 'send-keys')}
        self.assertEqual(self.act()['state'], 'unverified')
        self.assertEqual(len(self.sends()), 1)


class Stop(Case):
    def setUp(self):
        super().setUp()
        holder = mock.patch.object(compaction_hold, 'active', return_value={'id': 'h1'})
        self.hold = holder.start()
        self.addCleanup(holder.stop)

    def test_stop_needs_a_drover_row_in_wsl_and_a_live_hold(self):
        self.stage()
        self.refused(lambda: self.stop(hold='other'), 'no-live-hold')
        self.refused(lambda: self.stop(hold=None), 'no-live-hold')
        self.hold.return_value = None
        self.refused(self.stop, 'no-live-hold')
        self.hold.return_value = {'phase': 'draining'}
        self.refused(lambda: self.stop(hold=None), 'no-live-hold')
        self.hold.return_value = {'id': 'h1'}
        self.conn.execute('DELETE FROM pane_agents')
        self.windows(CLAUDE_IDLE)
        self.refused(self.stop, 'not-wsl')
        self.conn.execute('DELETE FROM pane_agents')
        self.stage(row={'started_by': 'owner'})
        self.refused(self.stop, 'not-drovers')
        self.conn.execute('DELETE FROM pane_agents')
        self.stage(row={'state': 'retired'})
        self.refused(self.stop, 'not-live')

    def test_a_stale_row_is_read_again_before_a_stop(self):
        self.stage()
        ops_panes.update_agent(self.conn, self.row['id'], state='retired')
        self.refused(self.stop, 'not-live')

    def test_a_hold_released_before_the_enter_stops_the_enter(self):
        fake = self.stage()

        def settle(seconds):
            fake.screens[PANE] = composing('stop now')
            self.hold.return_value = None
        self.sleep.side_effect = settle
        self.assertEqual(self.stop()['reason'], 'no-live-hold')
        self.assertEqual(len(self.sends()), 1)

    def test_stop_skips_a_dialog_or_a_draft(self):
        for name, screen in dict(dialog=DIALOG, model=MODEL, suspect=SUSPECT, draft=DRAFT, copy=COPY).items():
            with self.subTest(name):
                self.stage(screen, row={'state': 'compaction'})
                self.refused(self.stop)
                self.assertIsNone(self.last())
                self.conn.execute('DELETE FROM pane_agents')

    def test_stop_reaches_a_working_or_focused_pane(self):
        turn = 'history\n\n• Working ({}s • esc to interrupt)\n\n› {}\n\n? for shortcuts\n'
        for name, agent in dict(working={'agent_status': 'working'}, focused={'focused': True}).items():
            with self.subTest(name):
                # The turn's timer moves between reads: that is no reason to skip a stop.
                self.stage([turn.format(1, ''), turn.format(2, ''), turn.format(3, '')], agent=agent,
                           row={'state': 'compaction'})
                self.paste_settles_to(turn.format(4, 'stop now'))
                write = self.stop()
                self.assertEqual((write['state'], write['kind']), ('sent', 'stop'))
                self.assertEqual(self.sends(), [['pane', 'send-text', PANE, '\x1b[200~stop now\x1b[201~'], ENTER])
                self.conn.execute('DELETE FROM pane_agents')

    def test_identity_still_binds_a_stop(self):
        self.stage(agent={'terminal_id': 't2'})
        self.refused(self.stop, 'occupant-changed')

    def test_stop_gets_no_enter_when_the_composer_is_not_its_own(self):
        self.stage()
        self.paste_settles_to(composing('stop now and the owner typed'))
        self.assertEqual(self.stop()['state'], 'unverified')
        self.assertEqual(len(self.sends()), 1)


class OnlyCompaction(Case):
    def test_only_compaction_calls_send_stop(self):
        named = set()
        for path in sources():
            tree = ast.parse(path.read_text(encoding='utf-8'))
            if any(isinstance(node, (ast.Attribute, ast.Name, ast.alias)) and
                   'send_stop' in (getattr(node, 'attr', None), getattr(node, 'id', None), getattr(node, 'name', None))
                   for node in ast.walk(tree)):
                named.add(path.relative_to(ROOT).as_posix())
        self.assertLessEqual(named, {'drover/pane_compaction.py'})
