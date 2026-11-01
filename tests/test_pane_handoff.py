"""A director is asked for its handoff once, and nothing reaches a pane that is not safe."""
from unittest import mock

from drover import handoff, pane_context, pane_handoff, pane_launch, watch
from drover.ledger import ops_panes
from drover.notifications import roles
from tests.pane_launch_support import CLAUDE_WORKING
from tests.pane_send_support import CLAUDE_IDLE, DIALOG, PANE
from tests.panes_support import ROLE, Resident

FULL = ''.join(f'## {section}\nSomething true.\n\n' for section in handoff.SECTIONS)
LOW, HANDOFF, FORCED = 100000, 275000, 300000


class Maintain(Resident):
    def setUp(self):
        super().setUp()
        self.launched()
        self.cfg['director'].update(handoff_tokens=HANDOFF)
        self.cfg['watch'] = dict(context_tokens=FORCED, interval_s=600, stalled_hours=24, red_rate=0.5, min_gates=6)
        self.idle()
        reading = mock.patch.object(pane_context, 'read', return_value=LOW)
        self.read = reading.start()
        self.addCleanup(reading.stop)
        self.fake.calls.clear()
        self.path = handoff.path_for(self.cfg, ROLE, self.run_row['generation'])

    def idle(self):
        self.fake.screens[PANE] = CLAUDE_IDLE
        self.fake.agents[PANE].update(agent_status='idle', focused=False)

    def at(self, tokens):
        self.read.return_value = tokens
        pane_handoff.maintain(self.panes, self.run_row)
        return self.row_now()

    def context_notices(self):
        return [event for event in self.port.events(kind='pane.notice') if event['data']['notice']['id'].startswith('pane-context:')]

    def request(self):
        return ops_panes.last_write(self.conn, self.row['id'], 'handoff')

    def requests(self):
        return [call for call in self.fake.calls if call[:2] in (['agent', 'prompt'], ['pane', 'send-text'])]

    def written(self, text=FULL):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text, encoding='utf-8')

    def test_under_the_level_nothing_is_asked(self):
        self.assertIsNone(self.at(HANDOFF - 1)['handoff_state'])
        self.assertEqual(self.requests(), [])

    def test_every_known_reading_is_kept_on_the_row(self):
        self.assertEqual(self.at(123456)['context_tokens'], 123456)

    def test_handoff_is_requested_once_at_275k(self):
        self.assertEqual(self.at(HANDOFF)['handoff_state'], 'requested')
        for tokens in (HANDOFF + 1, HANDOFF + 2):
            self.at(tokens)
        self.assertEqual(len(self.requests()), 1)
        self.assertIn(str(self.path), self.requests()[0][-1])
        self.assertEqual(self.request()['kind'], 'handoff')

    def test_handoff_tokens_is_configurable(self):
        self.cfg['director']['handoff_tokens'] = 50000
        self.assertIsNone(self.at(49999)['handoff_state'])
        self.assertEqual(self.at(50000)['handoff_state'], 'requested')

    def test_the_folder_exists_before_the_agent_is_told_of_it(self):
        self.at(HANDOFF)
        self.assertTrue(self.path.parent.is_dir())

    def test_a_wsl_agent_is_told_the_wsl_path(self):
        self.conn.execute("UPDATE pane_agents SET placement='wsl'")
        with mock.patch.object(pane_handoff.pane_send, 'send', return_value={}) as send:
            self.at(HANDOFF)
        self.assertIn(handoff.agent_path(self.path, 'wsl'), send.call_args.args[3])
        self.assertEqual(send.call_args.args[4], 'handoff')

    def test_unknown_context_requests_nothing_and_rotates_nothing(self):
        for _ in range(2):
            row = self.at(None)
        self.assertIsNone(row['handoff_state'])
        self.assertIsNone(row['context_tokens'])
        self.assertEqual(self.requests(), [])
        self.assertFalse(self.path.exists())
        self.assertIsNone(self.outcome())
        self.assertEqual(row['state'], 'live')

    def test_unknown_context_leaves_a_request_and_a_reading_as_they_were(self):
        self.at(HANDOFF)
        row = self.at(None)
        self.assertEqual((row['handoff_state'], row['context_tokens']), ('requested', HANDOFF))

    def test_unknown_never_cancels_a_written_file(self):
        self.at(HANDOFF)
        self.written()
        self.fake.agents[PANE].update(focused=True)
        self.at(HANDOFF)
        row = self.at(None)
        self.assertEqual(row['handoff_state'], 'written')
        self.assertTrue(self.path.exists())

    def test_unknown_that_lasts_is_one_notice_and_a_reading_closes_it(self):
        self.at(None)
        self.assertEqual(self.context_notices(), [])
        self.time += pane_handoff.UNKNOWN_S + 1
        for _ in range(2):
            self.at(None)
        self.assertEqual(len(self.context_notices()), 1)
        self.at(LOW)
        question = self.context_notices()[0]['data']['question']
        self.assertEqual(self.conn.execute('SELECT status FROM questions WHERE id=?', (question,)).fetchone()[0], 'answered')
        self.at(None)
        self.at(None)
        self.assertEqual(len(self.context_notices()), 1)

    def test_a_short_gap_in_the_reading_is_no_notice(self):
        self.at(None)
        self.time += pane_handoff.UNKNOWN_S - 1
        self.at(None)
        self.assertEqual(self.context_notices(), [])
        self.at(LOW)
        self.at(None)
        self.time += 2
        self.at(None)
        self.assertEqual(self.context_notices(), [])

    def test_unknown_after_a_forced_reading_writes_no_fallback(self):
        self.fake.agents[PANE].update(focused=True)
        self.at(FORCED)
        self.idle()
        self.at(None)
        self.assertFalse(self.path.exists())

    def test_unsafe_pane_retries_on_a_later_tick_without_a_second_write(self):
        self.fake.agents[PANE].update(agent_status='working')
        self.fake.screens[PANE] = CLAUDE_WORKING
        self.assertIsNone(self.at(HANDOFF)['handoff_state'])
        self.assertEqual(self.requests(), [])
        self.idle()
        self.assertEqual(self.at(HANDOFF)['handoff_state'], 'requested')
        self.at(HANDOFF)
        self.assertEqual(len(self.requests()), 1)

    def test_a_request_that_went_out_unverified_is_not_sent_again(self):
        self.fake.hang = {('agent', 'prompt')}
        self.assertEqual(self.at(HANDOFF)['handoff_state'], 'requested')
        self.assertEqual(self.request()['state'], 'unverified')
        self.fake.hang = False
        self.fake.calls.clear()
        self.at(HANDOFF)
        self.assertEqual(self.requests(), [])

    def test_a_hub_that_dies_inside_the_send_never_asks_twice(self):
        with mock.patch.object(pane_handoff.pane_send, 'send', side_effect=RuntimeError('the hub went away')):
            with self.assertRaises(RuntimeError):
                self.at(HANDOFF)
        self.assertEqual(self.at(HANDOFF)['handoff_state'], 'requested')
        self.assertEqual(self.requests(), [])

    def test_a_file_left_from_before_a_request_does_not_count(self):
        self.written()
        self.assertEqual(self.at(HANDOFF)['handoff_state'], 'requested')
        self.assertFalse(self.path.exists())

    def test_a_file_that_cannot_be_cleared_stops_the_request(self):
        self.written()
        with mock.patch.object(type(self.path), 'unlink', side_effect=PermissionError('held')):
            self.assertIsNone(self.at(HANDOFF)['handoff_state'])
        self.assertEqual(self.requests(), [])

    def test_a_valid_file_moves_to_written(self):
        self.at(HANDOFF)
        self.written()
        self.assertEqual(self.at(HANDOFF)['handoff_state'], 'written')

    def test_the_file_closes_a_request_that_went_out_unverified(self):
        self.fake.hang = {('agent', 'prompt')}
        self.at(HANDOFF)
        self.fake.hang = False
        self.written()
        self.at(HANDOFF)
        self.assertEqual(self.request()['state'], 'observed')

    def test_a_valid_file_on_an_unsafe_pane_is_still_written(self):
        self.at(HANDOFF)
        self.written()
        self.fake.agents[PANE].update(focused=True)
        self.assertEqual(self.at(HANDOFF)['handoff_state'], 'written')

    def test_missing_section_is_not_valid(self):
        self.at(HANDOFF)
        self.written(FULL.replace('## Goal', '## Aim'))
        self.assertEqual(self.at(HANDOFF)['handoff_state'], 'requested')

    def test_an_unreadable_file_is_not_valid(self):
        self.at(HANDOFF)
        self.path.write_bytes(b'\xff\xfe\xff')
        self.assertEqual(self.at(HANDOFF)['handoff_state'], 'requested')

    def test_at_300k_drover_writes_its_own_handoff_when_safe(self):
        self.conn.execute("UPDATE director_runtime SET checkpoint='{\"cursor\": 4}'")
        task = 'T-open'
        for ident, status in ((task, 'open'), ('T-landed', 'landed')):
            self.conn.execute('INSERT INTO tasks(id,repo,title,rank,status,created,updated) VALUES(?,?,?,1,?,?,?)',
                              (ident, 'repo', 'A task to name', status, 'now', 'now'))
        row = self.at(FORCED)
        self.assertEqual(row['handoff_state'], 'written')
        text = self.path.read_text(encoding='utf-8')
        self.assertEqual(handoff.missing(text), [])
        self.assertIn(task, text)
        self.assertNotIn('T-landed', text)
        self.assertIn('"cursor": 4', text)
        self.assertEqual([call for call in self.requests() if 'handoff' in call[-1]], [])

    def test_at_300k_a_half_written_file_is_replaced(self):
        self.at(HANDOFF)
        self.written('## Goal\nStill writing')
        self.at(FORCED)
        self.assertEqual(handoff.missing(self.path.read_text(encoding='utf-8')), [])

    def test_at_300k_the_directors_own_valid_file_is_kept(self):
        self.at(HANDOFF)
        self.written()
        self.at(FORCED)
        self.assertEqual(self.path.read_text(encoding='utf-8'), FULL)

    def test_at_300k_a_working_or_focused_pane_gets_no_input_and_one_notice(self):
        for name, unsafe in (('working', lambda: (self.fake.agents[PANE].update(agent_status='working'),
                                                  self.fake.screens.update({PANE: CLAUDE_WORKING}))),
                             ('focused', lambda: self.fake.agents[PANE].update(focused=True)),
                             ('dialog', lambda: self.fake.screens.update({PANE: DIALOG}))):
            with self.subTest(name):
                self.idle()
                unsafe()
                self.assertIsNone(self.at(FORCED + 1)['handoff_state'])
                self.assertEqual(self.requests(), [])
                self.assertFalse(self.path.exists())
        for now in (10000.0, 20000.0):
            opened = watch.tick(self.conn, self.cfg, None, now)
        self.assertEqual([ident for ident in opened if ident.startswith('context:')], [f'context:{self.rid}'])
        count = self.conn.execute("SELECT COUNT(*) FROM task_events WHERE kind LIKE 'watch.notice%'").fetchone()[0]
        self.assertEqual(count, 1)

    def test_no_compact_is_ever_sent(self):
        for tokens in (HANDOFF, FORCED, 340000):
            self.at(tokens)
        self.assertFalse([call for call in self.fake.calls if any('/compact' in str(part) for part in call)])

    def test_owner_compact_cancels_the_request(self):
        self.at(HANDOFF)
        self.written(FULL.replace('## Goal', '## Half'))
        row = self.at(HANDOFF - 1)
        self.assertIsNone(row['handoff_state'])
        self.assertFalse(self.path.exists())
        self.assertEqual(self.at(HANDOFF)['handoff_state'], 'requested')
        self.assertEqual(len(self.requests()), 2)

    def test_owner_compact_cancels_a_file_already_written(self):
        self.at(HANDOFF)
        self.written()
        self.fake.agents[PANE].update(focused=True)
        self.at(HANDOFF)
        self.assertIsNone(self.at(LOW)['handoff_state'])
        self.assertFalse(self.path.exists())

    def test_a_row_that_is_not_live_is_left_alone(self):
        ops_panes.update_agent(self.conn, self.row['id'], state='compaction')
        self.at(FORCED)
        self.read.assert_not_called()

    def test_the_real_tick_reaches_the_request(self):
        self.read.return_value = HANDOFF
        self.tick()
        self.assertEqual(self.row_now()['handoff_state'], 'requested')


class WakeAfterRotation(Resident):
    def test_a_rotated_role_is_due_at_once(self):
        self.launched()
        self.assertFalse(roles.wake_due(self.notifications, ROLE))
        self.runtime.finish(self.rid, dict(status='rotated', usage={}))
        self.assertTrue(roles.wake_due(self.notifications, ROLE))

    def test_any_other_ending_keeps_the_window(self):
        self.launched()
        self.runtime.finish(self.rid, dict(status='done', usage={}))
        self.assertFalse(roles.wake_due(self.notifications, ROLE))


class Brief(Resident):
    def test_a_handoff_brief_comes_first_and_needs_the_restatement(self):
        path = 'C:\\Users\\someone\\.drover\\directors\\director%3Aa\\handoff-12.md'
        prompt = 'Read your inbox with python -m drover.notifications inbox, act on it, and commit a checkpoint.'
        text = pane_launch.brief(dict(role_id=ROLE, user_prompt=prompt), path)
        self.assertLess(text.index(path), text.index('Read your inbox'))
        self.assertLess(text.index('restated.md'), text.index('Read your inbox'))
        pane_launch.pane_send._check_text(text)

    def test_a_brief_with_no_handoff_is_as_it_was(self):
        self.assertEqual(pane_launch.brief(dict(role_id=ROLE, user_prompt='Go.')), f'Drover: you are director role {ROLE}. Go.')
