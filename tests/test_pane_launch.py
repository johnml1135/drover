"""Rows 6, 9, 10, 25, 26, 28 and 29: an agent starts in a new pane once, and is proved before it counts."""
from drover import pane_launch, pane_send
from drover.launch import AdmissionHeld
from drover.ledger import ops, ops_panes
from tests.pane_launch_support import CLAUDE_TRUST, CLAUDE_WORKING, PROMPT, SECRET, TRUST, Launching
from tests.pane_send_support import CLAUDE_IDLE, DRAFT, IDLE, PANE, WORKING, WRITE_VERBS


class Holds(Launching):
    def held(self, reason, spec=None, **kw):
        with self.assertRaises(AdmissionHeld) as caught:
            self.start(spec, **kw)
        self.assertEqual(str(caught.exception), reason)
        self.assertEqual(self.calls('tab', 'create'), [])
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.private, [])

    def test_missing_cwd_fails_before_a_pane_opens(self):
        self.scene()
        self.held('pane-cwd-missing', self.spec(cwd=str(self.cwd / 'gone')))
        self.assertEqual(self.fake.calls, [])

    def test_hub_unreachable_from_wsl_holds_before_any_effect(self):
        self.scene('codex', 'wsl')
        self.cfg['hub']['api_listen_wsl'] = False
        self.held('hub-unreachable-from-wsl', self.spec('codex', 'wsl'))
        self.assertEqual(self.fake.calls, [])

    def test_herdr_closed_raises_admission_held(self):
        self.scene().closed = True
        self.held('herdr-unavailable')

    def test_permission_bypass_is_refused(self):
        self.scene()
        self.held('permission-bypass-refused', self.spec(extra_args=['--dangerously-skip-permissions']))
        self.assertEqual(self.fake.calls, [])

    def test_codex_on_windows_stays_forbidden(self):
        self.scene()
        self.held('codex-on-windows', self.spec('codex', 'windows'))

    def test_a_guard_that_holds_stops_the_launch_first(self):
        self.scene()

        def guard():
            raise AdmissionHeld('dispatch paused')
        self.held('dispatch paused', guard=guard)
        self.assertEqual(self.fake.calls, [])

    def test_no_workspace_holds(self):
        fake = self.scene()
        fake._workspaces = lambda args: (0, '{"result": {"workspaces": []}}', '')
        self.held('herdr-no-workspace')

    def test_a_refused_tab_holds_and_leaves_no_row(self):
        self.scene().refuse[('tab', 'create')] = 'workspace_not_found'
        with self.assertRaises(AdmissionHeld):
            self.start()
        self.assertEqual(self.rows(), [])


class Opening(Launching):
    def test_every_pane_opens_without_focus(self):
        self.scene()
        self.turns()
        self.start()
        self.assertEqual(self.calls('tab', 'create'), [['tab', 'create', '--workspace', 'w1', '--cwd', str(self.cwd), '--no-focus']])

    def test_the_configured_workspace_wins(self):
        self.scene()
        self.cfg['panes']['workspace'] = 'w9'
        self.assertEqual(pane_launch.open_pane(self.herdr, self.cfg, str(self.cwd)), 'w9:p1')
        self.assertNotIn(['workspace', 'list'], self.fake.calls)

    def test_seat_and_placement_are_written_before_any_input(self):
        fake = self.scene('codex', 'wsl')
        seen = []
        real = fake._answer

        def answer(args):
            if tuple(args[:2]) in WRITE_VERBS and not seen:
                seen.append(self.rows()[0])
            return real(args)
        fake._answer = answer
        self.start(self.spec('codex', 'wsl'))
        self.assertEqual({k: seen[0][k] for k in ('seat', 'placement', 'terminal_id', 'started_by', 'state', 'role_id', 'run_id', 'kind')},
                         dict(seat='seat-x', placement='wsl', terminal_id='t1', started_by='drover', state='starting',
                              role_id='R1', run_id=7, kind='codex'))

    def test_a_pane_with_no_terminal_gets_no_input(self):
        fake = self.scene()
        real = fake._tab_create

        def tab(args):
            answer = real(args)
            del fake.panes[PANE]['terminal_id']
            return answer
        fake._tab_create = tab
        row = self.start()
        self.assertEqual((row['state'], self.calls('agent', 'start'), self.calls('pane', 'run')), ('failed', [], []))

    def test_a_private_file_that_cannot_be_written_fails_the_row_with_nothing_typed(self):
        self.scene()

        def refuse(path, text, *, placement):
            raise OSError('nope')
        pane_launch.write_private = refuse
        row = self.start()
        self.assertEqual(row['state'], 'failed')
        self.assertEqual([c for c in self.fake.calls if tuple(c[:2]) in WRITE_VERBS], [])

    def test_a_guard_at_the_last_boundary_fails_the_row_and_types_nothing(self):
        self.scene()
        results = iter([None, AdmissionHeld('stopped')])

        def guard():
            result = next(results)
            if result:
                raise result
        with self.assertRaises(AdmissionHeld):
            self.start(guard=guard)
        self.assertEqual(self.rows()[0]['state'], 'failed')
        self.assertEqual([c for c in self.fake.calls if tuple(c[:2]) in WRITE_VERBS], [])


class Launcher(Launching):
    def test_windows_launch_uses_agent_start_after_env(self):
        self.scene()
        self.turns()
        row = self.start()
        writes = [c for c in self.fake.calls if c[:2] in (['pane', 'run'], ['agent', 'start'])]
        self.assertEqual([c[:2] for c in writes], [['pane', 'run'], ['agent', 'start']])
        self.assertTrue(writes[0][3].endswith("launch.ps1'"), writes[0][3])
        self.assertEqual(writes[1][:7], ['agent', 'start', 'director-7', '--kind', 'claude', '--pane', PANE])
        self.assertEqual(row['state'], 'live')
        text = (self.state / 'panes' / '7' / 'launch.ps1').read_text(encoding='utf-8')
        self.assertIn("$env:CLAUDE_CONFIG_DIR = 'C:\\seats\\c'", text)
        self.assertIn("$env:DROVER_HUB_URL = 'http://127.0.0.1:8766'", text)
        self.assertIn('$env:DROVER_CREDENTIAL_FILE = ', text)
        self.assertNotIn('WSLENV', text)

    def test_wsl_launch_sets_wslenv_and_runs_through_wsl_exe(self):
        self.scene('codex', 'wsl')
        self.start(self.spec('codex', 'wsl'))
        self.assertEqual(self.calls('agent', 'start'), [])
        self.assertEqual(len(self.calls('pane', 'run')), 1)
        ps1 = (self.state / 'panes' / '7' / 'launch.ps1').read_text(encoding='utf-8')
        self.assertIn('HERDR_ENV:HERDR_PANE_ID:HERDR_SOCKET_PATH:HERDR_BIN_PATH:HERDR_WORKSPACE_ID:HERDR_TAB_ID', ps1)
        self.assertIn('wsl.exe -d Ubuntu -e bash -li ', ps1)
        sh = (self.state / 'panes' / '7' / 'launch.sh').read_text(encoding='utf-8')
        self.assertIn('export CODEX_HOME="$HOME"/.codex', sh)
        self.assertIn('export DROVER_CREDENTIAL_FILE="$HOME"/.drover/panes/run-7.credential', sh)
        self.assertIn('export DROVER_HUB_URL=', sh)
        self.assertIn('exec codex ', sh)
        self.assertNotIn('\r', sh)

    def test_only_the_launchers_path_crosses_the_pane(self):
        self.scene('codex', 'wsl')
        self.start(self.spec('codex', 'wsl'))
        self.assertRegex(self.calls('pane', 'run')[0][3], r"^& '[^ ]*launch\.ps1'$")

    def test_credential_never_reaches_argv_scrollback_or_a_write(self):
        for kind, placement in (('claude', 'windows'), ('codex', 'wsl'), ('claude', 'wsl')):
            with self.subTest(kind=kind, placement=placement):
                self.scene(kind, placement)
                self.turns(kind=kind)
                self.start(self.spec(kind, placement, run_id=len(self.rows()) + 7))
                self.assertEqual({text for _, text, _ in self.private}, {SECRET})
                self.assertNotIn(SECRET, repr(self.fake.calls))
                self.assertNotIn(SECRET, ''.join(self.launcher_texts()))
                self.assertNotIn(SECRET, repr([dict(r) for r in self.conn.execute('SELECT * FROM pane_writes')]))
                self.assertNotIn(SECRET, repr(ops.events(self.conn)))
                self.reset()

    def test_agent_is_renamed_once_it_reports(self):
        fake = self.scene('codex', 'wsl', boots=False)
        order = []
        fake.after[('pane', 'run', 1)] = lambda: order.append(len(self.calls('agent', 'rename'))) or self.boot()
        row = self.start(self.spec('codex', 'wsl'))
        self.assertEqual(order, [0])
        self.assertEqual(self.calls('agent', 'rename'), [['agent', 'rename', PANE, 'director-7']])
        self.assertEqual(row['name'], 'director-7')

    def test_a_refused_rename_costs_nothing(self):
        self.scene('codex', 'wsl').refuse[('agent', 'rename')] = 'pane_not_found'
        self.turns(kind='codex')
        self.assertEqual(self.start(self.spec('codex', 'wsl'))['state'], 'live')

    def test_a_windows_agent_is_not_renamed(self):
        self.scene()
        self.turns()
        self.start()
        self.assertEqual(self.calls('agent', 'rename'), [])


class Startup(Launching):
    def test_session_id_is_recorded_at_session_start(self):
        self.scene(session='sess-9')
        self.turns()
        self.assertEqual(self.start()['session_id'], 'sess-9')

    def test_the_row_is_live_with_a_session_before_the_brief_goes(self):
        fake = self.scene()
        self.turns()
        seen = []
        real = fake._answer

        def answer(args):
            if args[:2] == ['agent', 'prompt']:
                seen.append(self.rows()[0])
            return real(args)
        fake._answer = answer
        self.start()
        self.assertEqual((seen[0]['state'], seen[0]['session_id'], seen[0]['terminal_id']), ('live', 's1', 't1'))

    def test_a_shell_with_no_agent_fails_and_types_nothing(self):
        self.scene(boots=False)
        row = self.start()
        self.assertEqual(row['state'], 'failed')
        self.assertEqual(self.calls('agent', 'prompt'), [])
        self.assertEqual(self.notices()[0]['id'], f"pane-launch:{row['id']}")

    def test_agent_that_exited_after_self_update_is_refused(self):
        fake = self.scene('codex', 'wsl', boots=False)

        def exit_after_update():
            fake.screens[PANE] = 'Update ran successfully! Please restart Codex.\n' + PROMPT
        fake.after[('pane', 'run', 1)] = exit_after_update
        row = self.start(self.spec('codex', 'wsl'))
        self.assertEqual(row['state'], 'failed')
        self.assertEqual(self.calls('pane', 'send-text'), [])

    def test_one_quiet_read_of_a_shell_does_not_fail_a_launch(self):
        fake = self.scene('codex', 'wsl', boots=False)
        fake.after[('pane', 'process-info', 3)] = self.boot     # after the claim's reads, the first startup read still shows it
        self.assertEqual(self.start(self.spec('codex', 'wsl'))['session_id'], 's1')

    def test_brief_waits_for_an_idle_composer_and_never_types_into_a_shell(self):
        fake = self.scene(boots=False)

        def boot():
            self.boot()
            fake.agents[PANE]['agent_status'] = 'working'
            fake.screens[PANE] = [CLAUDE_WORKING] * 6 + [CLAUDE_IDLE]
        fake.after[('agent', 'start', 1)] = boot
        real = fake._answer
        seen = []

        def answer(args):
            if args[:2] == ['agent', 'prompt']:
                seen.append(fake.agents[PANE]['agent_status'])
            if args[:2] == ['pane', 'read'] and fake.screens[PANE] == [CLAUDE_IDLE]:
                fake.agents[PANE]['agent_status'] = 'idle'
            return real(args)
        fake._answer = answer
        self.turns()
        self.assertEqual(self.start()['state'], 'live')
        self.assertEqual(seen, ['idle'])

    def test_a_draft_after_the_agent_reports_gets_no_brief(self):
        self.scene('codex', 'wsl', screen=DRAFT)
        row = self.start(self.spec('codex', 'wsl'))
        self.assertEqual(row['state'], 'failed')
        self.assertEqual(self.calls('pane', 'send-text'), [])

    def test_an_agent_that_never_reports_ends_failed_without_a_brief(self):
        fake = self.scene('codex', 'wsl', boots=False)

        def up():
            fake.processes = self.foreground
            fake.screens[PANE] = IDLE
        fake.after[('pane', 'run', 1)] = up
        row = self.start(self.spec('codex', 'wsl'))
        self.assertEqual((row['state'], row['session_id']), ('failed', None))
        self.assertEqual(self.calls('pane', 'send-text'), [])

    def test_a_different_kind_of_agent_in_the_pane_is_refused(self):
        fake = self.scene('codex', 'wsl', boots=False)

        def other():
            self.boot()
            fake.agents[PANE]['agent'] = 'claude'
        fake.after[('pane', 'run', 1)] = other
        self.assertEqual(self.start(self.spec('codex', 'wsl'))['state'], 'failed')
        self.assertEqual(self.calls('pane', 'send-text'), [])


class Trust(Launching):
    def trust_scene(self, screen=TRUST, *, trusted=True):
        fake = self.scene('codex', 'wsl', boots=False)
        self.trusted = trusted

        def up():
            fake.processes = self.foreground
            fake.screens[PANE] = screen
        fake.after[('pane', 'run', 1)] = up
        fake.after[('pane', 'send-keys', 1)] = self.boot
        return fake

    def test_codex_trust_is_answered_for_a_worktree_of_a_trusted_repo(self):
        self.trust_scene()
        row = self.start(self.spec('codex', 'wsl'))
        self.assertEqual(self.calls('pane', 'send-keys')[0], ['pane', 'send-keys', PANE, 'enter'])
        answered = ops.events(self.conn, kind='pane-trust-answered')
        self.assertEqual([e['data']['row'] for e in answered], [row['id']])

    def test_codex_trust_is_left_for_an_untrusted_repo(self):
        self.trust_scene(trusted=False)
        row = self.start(self.spec('codex', 'wsl'))
        self.assertEqual((row['state'], self.calls('pane', 'send-keys')), ('failed', []))
        self.assertEqual(len(self.notices()), 1)
        self.assertEqual(ops.events(self.conn, kind='pane-trust-answered'), [])

    def test_trust_unreadable_config_is_owner_only(self):
        self.trust_scene()

        def unreadable(cwd, seat, *, read=None):
            return pane_launch.pane_files.repo_trusted(cwd, seat, read=lambda args: 'not = [toml')
        pane_launch.repo_trusted = unreadable
        self.assertEqual(self.start(self.spec('codex', 'wsl'))['state'], 'failed')
        self.assertEqual(self.calls('pane', 'send-keys'), [])

    def test_near_miss_trust_screens_stay_with_the_owner(self):
        screens = dict(extra=TRUST + '3. More\n', changed=TRUST.replace('Quit', 'Leave'), selected=TRUST.replace('› 1', '  1'))
        for count, (name, screen) in enumerate(screens.items(), 1):
            with self.subTest(name):
                self.trust_scene(screen)
                self.assertEqual(self.start(self.spec('codex', 'wsl', role_id=name, run_id=count))['state'], 'failed')
                self.assertEqual(self.calls('pane', 'send-keys'), [])
                self.assertEqual(len(self.notices()), count)

    def test_claude_trust_dialog_stays_owner_only(self):
        self.trust_scene(CLAUDE_TRUST)
        row = self.start(self.spec('codex', 'wsl'))
        self.assertEqual((row['state'], self.calls('pane', 'send-keys')), ('failed', []))

    def test_claude_folder_trust_reports_to_the_owner_and_starts_nothing_else(self):
        fake = self.scene(boots=False)

        def blocked():
            fake.processes = self.foreground
            fake.screens[PANE] = CLAUDE_TRUST
        fake.after[('agent', 'start', 1)] = blocked
        fake.refuse[('agent', 'start')] = 'agent_not_ready'
        row = self.start()
        self.assertEqual(row['state'], 'failed')
        self.assertEqual(len(self.calls('agent', 'start')), 1)
        self.assertEqual([c for c in self.fake.calls if c[:2] in (['pane', 'send-keys'], ['agent', 'prompt'])], [])
        self.assertEqual(len(self.notices()), 1)

    def test_a_second_trust_dialog_stays_with_the_owner(self):
        fake = self.trust_scene()
        fake.after.pop(('pane', 'send-keys', 1))
        row = self.start(self.spec('codex', 'wsl'))
        self.assertEqual((row['state'], len(self.calls('pane', 'send-keys'))), ('failed', 1))

    def test_a_key_that_stalled_ends_the_row(self):
        self.trust_scene().hang = {('pane', 'send-keys')}
        row = self.start(self.spec('codex', 'wsl'))
        self.assertEqual((row['state'], len(self.calls('pane', 'send-keys'))), ('failed', 1))
        self.assertEqual(ops.events(self.conn, kind='pane-trust-answered'), [])
        self.assertEqual(ops_panes.open_writes(self.conn, row['id']), [])

    def test_a_dialog_after_the_row_went_live_stops_the_brief(self):
        fake = self.scene('codex', 'wsl')
        fake.after[('agent', 'get', 1)] = lambda: fake.screens.update({PANE: 'Allow once?\n› 1. Allow once\n  2. Deny\n'})
        row = self.start(self.spec('codex', 'wsl'))
        self.assertEqual((row['state'], self.calls('pane', 'send-text')), ('failed', []))
        self.assertEqual(len(self.notices()), 1)


class FirstTurn(Launching):
    def test_brief_is_submitted_once_and_never_retried(self):
        self.scene()
        self.turns()
        self.start()
        self.assertEqual(len(self.calls('agent', 'prompt')), 1)
        self.assertEqual([w['kind'] for w in self.writes(self.rows()[0])], ['shell', 'start', 'brief'])

    def test_a_brief_goes_as_one_paste_and_one_enter_in_wsl(self):
        fake = self.scene('codex', 'wsl')
        self.turns(kind='codex')
        spec = self.spec('codex', 'wsl')
        self.assertEqual(self.start(spec)['state'], 'live')
        self.assertEqual(len(self.calls('pane', 'send-text')), 1)
        self.assertEqual(self.calls('pane', 'send-keys'), [['pane', 'send-keys', PANE, 'enter']])
        self.assertIs(fake.processes, self.foreground)

    def test_empty_composer_after_send_proves_nothing(self):
        self.scene()
        row = self.start()      # the prompt goes out, nothing moves, and the composer stays empty
        self.assertEqual(row['state'], 'failed')
        self.assertEqual(len(self.calls('agent', 'prompt')), 1)
        self.assertEqual(self.notices()[0]['id'], f"pane-launch:{row['id']}")

    def test_blocked_is_not_a_first_turn(self):
        dialog = 'Allow once?\n❯ 1. Allow once\n  2. Deny\n'
        for name, over in dict(herdr=dict(screen=CLAUDE_IDLE, status='blocked'), screen=dict(screen=dialog, status='idle'),
                               both=dict(screen=dialog, status='blocked')).items():
            with self.subTest(name):
                self.scene()
                self.turns(**over)
                self.assertEqual(self.start()['state'], 'failed')
                self.reset()

    def test_failed_submission_is_not_live(self):
        for name, code in dict(stalled=None, refused='agent_blocked').items():
            with self.subTest(name):
                fake = self.scene()
                if code:
                    fake.refuse[('agent', 'prompt')] = code
                else:
                    fake.hang = {('agent', 'prompt')}
                row = self.start()
                self.assertEqual(row['state'], 'failed')
                self.assertEqual(len(self.calls('agent', 'prompt')), 1)
                self.assertEqual(ops_panes.open_writes(self.conn, row['id']), [])
                self.reset()

    def test_a_working_row_on_screen_proves_it_under_a_stale_status(self):
        self.scene()
        self.turns(status='idle')
        self.assertEqual(self.start()['state'], 'live')

    def test_a_working_status_proves_it_under_a_quiet_screen(self):
        self.scene()
        self.turns(CLAUDE_IDLE)
        self.assertEqual(self.start()['state'], 'live')

    def test_an_occupant_that_changed_in_the_first_turn_fails_the_row(self):
        fake = self.scene()
        self.turns(WORKING)
        fake.after[('agent', 'prompt', 1)] = lambda: fake.agents[PANE].update(agent_session={'value': 'other'})
        self.assertEqual(self.start()['state'], 'failed')

    def footer(self, row):
        self.scene('codex', 'wsl')
        self.turns(WORKING.replace('? for shortcuts', row), kind='codex')
        return self.start(self.spec('codex', 'wsl', effort='high'))

    def test_footer_mismatch_after_first_turn_goes_to_the_inbox(self):
        row = self.footer('gpt-6.1 terra high · ~/repo')
        self.assertEqual(row['state'], 'live')
        self.assertEqual([n['id'] for n in self.notices()], [f"pane-model:{row['id']}"])
        self.assertEqual(len(self.calls('pane', 'run')), 1)

    def test_a_matching_or_unreadable_footer_is_no_notice(self):
        for name, footer in dict(same='gpt-6.1 sol high · ~/repo', none='60% context left').items():
            with self.subTest(name):
                self.assertEqual(self.footer(footer)['state'], 'live')
                self.assertEqual(self.notices(), [])
                self.reset()

    def test_a_claude_footer_says_nothing_yet(self):
        self.scene()
        self.turns()
        self.assertEqual(self.start()['state'], 'live')
        self.assertEqual(self.notices(), [])

    def test_an_effort_mismatch_is_a_notice_too(self):
        row = self.footer('gpt-6.1 sol low · ~/repo')
        self.assertEqual([n['id'] for n in self.notices()], [f"pane-model:{row['id']}"])

    def test_brief_is_one_short_line(self):
        text = pane_launch.brief(self.spec(user_prompt='Do\nthis\n  now.'))
        self.assertIn('Do this now.', text)
        self.assertNotIn('\n', text)
        self.assertLessEqual(len(text), pane_send.MAX_TEXT)
        self.assertIn('/x/handoff.md', pane_launch.brief(self.spec(), handoff='/x/handoff.md'))
        self.assertNotIn(SECRET, text)


class Reused(Launching):
    def test_reused_pane_inherits_no_consent(self):
        fake = self.scene('codex', 'wsl', boots=False)
        self.trusted = True
        first = ops_panes.record_agent(self.conn, pane_id=PANE, terminal_id='t1', kind='codex', seat='seat-x', placement='wsl',
                                       started_by='drover', state='live', session_id='s1', role_id='R1', run_id=7,
                                       name='director-7', cwd=str(self.cwd))
        fake._tab_create(['tab', 'create'])
        fake.agents[PANE] = dict(pane_id=PANE, terminal_id='t1', agent='codex', agent_session={'value': 's1'}, agent_status='idle')

        def up():
            fake.processes = self.foreground
            fake.screens[PANE] = TRUST
        fake.after[('pane', 'run', 1)] = up
        row = pane_launch.resume(self.port, self.herdr, first, self.spec('codex', 'wsl'), cfg=self.cfg)
        self.assertEqual((row['state'], self.calls('pane', 'send-keys')), ('failed', []))
        self.assertEqual(ops.events(self.conn, kind='pane-trust-answered'), [])
