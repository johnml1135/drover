"""What crosses a pane: the private files, the agent's arguments, and the proof a repo is trusted."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from drover import pane_files, pane_launch, process
from tests.fakeci import FakeCommands, process_options
from tests.pane_launch_support import SECRET, SEATS

CONFIG = '[projects."/mnt/h/repos/drover"]\ntrust_level = "trusted"\n[projects."/mnt/h/repos/other"]\ntrust_level = "untrusted"\n'
WORKTREE = 'H:\\repos\\drover.worktrees\\t9'


def spec(kind='claude', placement='windows', **over):
    return dict(backend=kind, model='m', seat='s', cwd='H:\\x', placement=placement, run_id=7, **over)


class WindowsFile(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / '7' / 'credential'
        self.seen = []

    def icacls(self, argv, **kw):
        self.seen.append((argv, self.path.read_bytes()))
        return subprocess.CompletedProcess(argv, 0, '', '')

    def write(self, run):
        return pane_files.write_private(self.path, SECRET, placement='windows', run=run)

    def test_windows_credential_file_has_a_private_acl(self):
        with mock.patch('getpass.getuser', return_value='me'):
            self.assertEqual(self.write(self.icacls), str(self.path))
        self.assertEqual(self.seen[0][0], ['icacls', str(self.path), '/inheritance:r', '/grant:r', 'me:F'])
        self.assertEqual(self.path.read_text(encoding='utf-8'), SECRET)

    def test_the_acl_comes_before_the_secret(self):
        self.write(self.icacls)
        self.assertEqual(self.seen[0][1], b'')

    def test_an_existing_file_is_never_written_through(self):
        self.path.parent.mkdir()
        self.path.write_text('old', encoding='utf-8')
        with self.assertRaises(FileExistsError):
            self.write(self.icacls)
        self.assertEqual((self.path.read_text(encoding='utf-8'), self.seen), ('old', []))

    def test_a_refused_acl_leaves_no_file(self):
        with self.assertRaises(OSError):
            self.write(lambda argv, **kw: subprocess.CompletedProcess(argv, 5, '', 'denied'))
        self.assertFalse(self.path.exists())

    def test_the_secret_is_never_an_argument(self):
        self.write(self.icacls)
        self.assertNotIn(SECRET, repr(self.seen[0][0]))


class WslFile(unittest.TestCase):
    def write(self, code=0, out='/home/x/.drover/panes/run-7.credential', name='run-7.credential'):
        self.calls = []

        def run(argv, **kw):
            self.calls.append((argv, kw))
            return subprocess.CompletedProcess(argv, code, out, '')
        with mock.patch.object(pane_files, 'wsl_control', lambda args: ['wsl.exe', *args]), \
                mock.patch.object(pane_files.placement, 'distro', lambda: 'Ubuntu'):
            return pane_files.write_private(Path('D:/state') / name, SECRET, placement='wsl', run=run)

    def test_wsl_credential_file_is_private_on_ext4(self):
        self.assertEqual(self.write(), '/home/x/.drover/panes/run-7.credential')
        argv, kw = self.calls[0]
        self.assertEqual(kw['input'], SECRET)
        self.assertEqual(argv[:5], ['wsl.exe', '-d', 'Ubuntu', '--exec', 'bash'])
        script = argv[6]
        self.assertTrue(script.startswith('umask 077;'), script)
        self.assertIn('set -C', script)        # noclobber: never through an existing file
        self.assertIn('"$HOME/.drover/panes"', script)
        self.assertNotIn('/mnt/', ' '.join(argv))

    def test_the_secret_stays_on_stdin(self):
        self.write()
        self.assertNotIn(SECRET, repr(self.calls[0][0]))

    def test_a_failed_write_raises(self):
        with self.assertRaises(OSError):
            self.write(code=1)

    def test_a_name_that_could_leave_the_folder_is_refused(self):
        for name in ('a b', '$(x)'):
            with self.subTest(name), self.assertRaises(ValueError):
                self.write(name=name)


class Args(unittest.TestCase):
    def args(self, *a, **kw):
        return pane_files.agent_args(*a, model='m', effort='high', **kw)

    def test_native_compaction_sits_above_the_forced_handoff(self):
        self.assertEqual(pane_launch.NATIVE_COMPACT, 350_000)
        self.assertEqual(pane_launch.FIRST_TURN_S, 90)
        from drover.config_defaults import DEFAULTS
        self.assertGreater(pane_launch.NATIVE_COMPACT, DEFAULTS['watch']['context_tokens'])
        self.assertEqual(self.args(spec())[:6], ['--model', 'm', '--effort', 'high', '--autocompact', '350k'])
        codex = self.args(spec('codex', 'wsl'))
        self.assertIn('model_context_window=350000', codex)
        self.assertIn('model_auto_compact_token_limit=350000', codex)

    def test_codex_update_check_is_off(self):
        self.assertIn('check_for_update_on_startup=false', self.args(spec('codex', 'wsl')))

    def test_only_a_wsl_claude_gets_the_hook_settings(self):
        self.assertNotIn('--settings', self.args(spec()))
        with_hook = self.args(spec(placement='wsl'), settings='/mnt/d/s/settings.json')
        self.assertEqual(with_hook[with_hook.index('--settings') + 1], '/mnt/d/s/settings.json')

    def test_a_bypass_as_model_or_effort_raises(self):
        for value in ('--dangerously-skip-permissions', '--allow-dangerously-skip-permissions=1',
                      '--permission-mode=bypassPermissions'):
            with self.subTest(value), self.assertRaises(ValueError):
                pane_files.agent_args(spec(), model=value, effort='high')
            with self.subTest(value), self.assertRaises(ValueError):
                pane_files.agent_args(spec(), model='m', effort=value)

    def test_nothing_but_the_named_flags_reach_the_agent(self):
        self.assertEqual(self.args(spec(extra_args=['--yolo'], sandbox='danger-full-access')),
                         ['--model', 'm', '--effort', 'high', '--autocompact', '350k'])

    def test_a_resume_session_leads_the_arguments(self):
        self.assertEqual(self.args(spec(), resume='s1')[:2], ['--resume', 's1'])
        self.assertEqual(self.args(spec('codex', 'wsl'), resume='s1')[:2], ['resume', 's1'])

    def test_hook_settings_report_every_state_through_the_hook(self):
        settings = json.loads(pane_files.hook_settings('claude'))
        from drover.pane_status_hook import STATES
        self.assertEqual(set(settings['hooks']), set(STATES))
        command = settings['hooks']['SessionStart'][0]['hooks'][0]['command']
        self.assertRegex(command, r'^python3 /\S*pane_status_hook\.py --agent claude$')
        self.assertNotIn('permissions', settings)


class Trusted(unittest.TestCase):
    def check(self, *, config=CONFIG, top='/mnt/h/repos/drover.worktrees/t9', common='/mnt/h/repos/drover/.git', cwd=WORKTREE,
              seat=None):
        self.asked = []

        def read(args):
            self.asked.append(args)
            if args[:1] == ['git']:
                if common is None:
                    raise OSError('not a repo')
                return f'{top}\n{common}\n'
            if config is None:
                raise OSError('unreadable')
            return config
        return pane_launch.repo_trusted(cwd, seat or SEATS['codex'], read=read)

    def test_a_worktree_of_a_trusted_repo_is_trusted(self):
        self.assertIs(self.check(), True)

    def test_the_main_checkout_of_a_trusted_repo_is_trusted(self):
        self.assertIs(self.check(top='/mnt/h/repos/drover', cwd='H:\\repos\\drover'), True)

    def test_git_is_asked_in_wsl_form_with_the_seats_home(self):
        self.check()
        self.assertEqual(self.asked[0][:3], ['git', '-C', '/mnt/h/repos/drover.worktrees/t9'])
        self.assertEqual(self.asked[1], ['sh', '-c', 'exec cat -- "$HOME/$1"', 'sh', '.codex/config.toml'])

    def test_an_absolute_home_is_read_directly(self):
        self.check(seat=dict(SEATS['codex'], codex_home='/home/x/seat'))
        self.assertEqual(self.asked[1], ['cat', '--', '/home/x/seat/config.toml'])

    def test_everything_else_is_untrusted(self):
        cases = dict(untrusted=dict(common='/mnt/h/repos/other/.git'), unlisted=dict(common='/mnt/h/repos/third/.git'),
                     no_repo=dict(common=None), unreadable=dict(config=None), bad_toml=dict(config='not = [toml'),
                     no_projects=dict(config='x = 1\n'), scalar=dict(config='projects = 1\n'),
                     project_scalar=dict(config='[projects]\n"/mnt/h/repos/drover" = "trusted"\n'),
                     word=dict(config='[projects."/mnt/h/repos/drover"]\ntrust_level = "trusted-ish"\n'),
                     subfolder=dict(cwd=WORKTREE + '\\sub'), submodule=dict(common='/mnt/h/repos/drover/.git/modules/x'),
                     bare=dict(common='/mnt/h/repos/drover.git'), listed_folder=dict(common='/mnt/h/repos/drover'))
        for name, over in cases.items():
            with self.subTest(name):
                self.assertIs(self.check(**over), False)

    def test_a_key_in_windows_form_or_with_a_slash_still_matches(self):
        for key in (r'H:\repos\drover', '/mnt/h/repos/drover/'):
            with self.subTest(key):
                self.assertIs(self.check(config=f"[projects.'{key}']\ntrust_level = 'trusted'\n"), True)

    def test_a_home_on_the_wrong_kind_of_seat_is_untrusted(self):
        self.assertIs(self.check(seat={'name': 'c', 'backend': 'claude'}), False)

    def test_the_wsl_reads_run_through_process_and_any_failure_is_untrusted(self):
        top = '/mnt/h/repos/drover.worktrees/t9'
        wsl = lambda *args: ['wsl.exe', '-d', 'Ubuntu', '--exec', *args]
        git = wsl('git', '-C', top, 'rev-parse', '--path-format=absolute', '--show-toplevel', '--git-common-dir')
        with tempfile.TemporaryDirectory() as root:
            fake = FakeCommands(root)
            fake.expect(git, output=f'{top}\n/mnt/h/repos/drover/.git\n'.encode(), **process_options(15))
            fake.expect(wsl('sh', '-c', 'exec cat -- "$HOME/$1"', 'sh', '.codex/config.toml'), output=CONFIG.encode(),
                        **process_options(15))
            fake.expect(git, returncode=128, stderr_output=b'fatal: not a git repository\n', **process_options(15))
            fake.expect(git, error=subprocess.TimeoutExpired(git, 15), **process_options(15))
            fake.expect(git, error=FileNotFoundError(2, 'No such file'), **process_options(15))
            fake.expect(git, output=b'\xff\n', **process_options(15))     # not UTF-8
            with mock.patch.object(pane_files, 'wsl_control', lambda argv: ['wsl.exe', *argv]), \
                    mock.patch.object(pane_files.placement, 'distro', return_value='Ubuntu'), \
                    mock.patch('subprocess.run', side_effect=AssertionError('real process')), fake.patch(process):
                self.assertIs(pane_launch.repo_trusted(WORKTREE, SEATS['codex']), True)
                for _ in range(4):
                    self.assertIs(pane_launch.repo_trusted(WORKTREE, SEATS['codex']), False)
