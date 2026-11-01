"""What a launch leaves and sends: the launcher, its one path across the pane, the credential, and the name."""
from tests.pane_launch_support import SECRET, Launching
from tests.pane_send_support import PANE


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
                self.assertNotIn(SECRET, repr(self.port.events()))
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
