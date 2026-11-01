import ast
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

from drover import pane_status_hook as hook

ENV = {'HERDR_ENV': '1', 'HERDR_PANE_ID': 'p1', 'HERDR_BIN_PATH': '/usr/bin/herdr'}


def call(event, agent='claude', env=ENV, session='s1', raw=None, run=None):
    seen = []
    payload = raw if raw is not None else json.dumps({'hook_event_name': event, 'session_id': session})
    code = hook.main(['--agent', agent], stdin=io.StringIO(payload), env=env, run=run or seen.append)
    return code, seen


class PaneStatusHookTest(unittest.TestCase):
    def test_outside_herdr_does_nothing(self):
        for env in ({}, {**ENV, 'HERDR_ENV': '0'}, {**ENV, 'HERDR_PANE_ID': ''}, {**ENV, 'HERDR_BIN_PATH': ''}):
            self.assertEqual(call('Stop', env=env), (0, []))

    def test_each_event_maps_to_its_state(self):
        table = {'SessionStart': 'idle', 'Stop': 'idle', 'Interrupt': 'idle', 'PermissionRequest': 'blocked',
                 **dict.fromkeys(('UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'SubagentStart', 'SubagentStop',
                                  'PreCompact', 'PostCompact'), 'working')}
        for event, state in table.items():
            self.assertEqual(hook.state_of(event), state)
            _, seen = call(event)
            self.assertEqual(seen[0][-1][-2:], ['--state', state])
        self.assertEqual(hook.state_of('SessionEnd'), 'release')

    def test_session_start_reports_the_session_then_idle(self):
        _, seen = call('SessionStart')
        cmds = seen[0]
        self.assertEqual([c[1:3] for c in cmds], [['pane', 'report-agent-session'], ['pane', 'report-agent']])
        self.assertEqual(cmds[0][-2:], ['--agent-session-id', 's1'])
        self.assertEqual(cmds[1][-2:], ['--state', 'idle'])
        self.assertEqual(len(call('SessionStart', session=None)[1][0]), 1)

    def test_session_end_releases_the_agent(self):
        _, seen = call('SessionEnd')
        seq = seen[0][0][-1]
        self.assertEqual(seen[0], [['/usr/bin/herdr', 'pane', 'release-agent', 'p1', '--source', 'herdr:claude-wsl',
                                    '--agent', 'claude', '--seq', seq]])

    def test_unknown_event_makes_no_call(self):
        self.assertEqual(call('Nonsense'), (0, []))

    def test_bad_json_makes_no_call_and_exits_zero(self):
        for raw in ('{nope', '', '[1]'):
            self.assertEqual(call('x', raw=raw), (0, []))

    def test_windows_binary_path_goes_through_wslpath(self):
        env = {**ENV, 'HERDR_BIN_PATH': r'C:\herdr\herdr.exe'}
        done = mock.Mock(stdout='/mnt/c/herdr/herdr.exe\n')
        with mock.patch.object(hook.subprocess, 'run', return_value=done) as run:
            _, seen = call('Stop', env=env)
        run.assert_called_once()
        self.assertEqual(run.call_args[0][0], ['wslpath', '-u', r'C:\herdr\herdr.exe'])
        self.assertEqual(seen[0][0][0], '/mnt/c/herdr/herdr.exe')

    def test_wslpath_failure_makes_no_call(self):
        env = {**ENV, 'HERDR_BIN_PATH': r'C:\herdr\herdr.exe'}
        with mock.patch.object(hook.subprocess, 'run', side_effect=OSError):
            self.assertEqual(call('Stop', env=env), (0, []))

    def test_linux_binary_path_is_used_as_is(self):
        with mock.patch.object(hook.subprocess, 'run') as run:
            _, seen = call('Stop')
        run.assert_not_called()
        self.assertEqual(seen[0][0][0], '/usr/bin/herdr')

    def test_claude_and_codex_use_their_own_source(self):
        for agent, source in (('claude', 'herdr:claude-wsl'), ('codex', 'herdr:codex-wsl')):
            cmd = call('Stop', agent=agent)[1][0][0]
            self.assertEqual(cmd[cmd.index('--source') + 1], source)
            self.assertEqual(cmd[cmd.index('--agent') + 1], agent)
        self.assertEqual(call('Stop', agent='other'), (0, []))

    def test_a_failing_herdr_never_fails_the_hook(self):
        def boom(cmds):
            raise OSError('no herdr')
        self.assertEqual(call('Stop', run=boom)[0], 0)
        with mock.patch.object(hook.subprocess, 'Popen', side_effect=OSError):
            self.assertEqual(call('Stop', run=hook.detached)[0], 0)

    def test_detached_discards_output_and_does_not_wait(self):
        with mock.patch.object(hook.subprocess, 'Popen') as popen:
            hook.detached([['x']])
        kw = popen.call_args.kwargs
        self.assertIs(kw['stdout'], hook.subprocess.DEVNULL)
        self.assertIs(kw['stderr'], hook.subprocess.DEVNULL)

    def test_wslenv_names(self):
        self.assertEqual(len(hook.WSLENV_NAMES), 6)
        self.assertIn('HERDR_BIN_PATH', hook.WSLENV_NAMES)

    def test_module_imports_only_the_standard_library(self):
        tree = ast.parse(Path(hook.__file__).read_text(encoding='utf-8'))
        names = {a.name.split('.')[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        names |= {n.module.split('.')[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        self.assertTrue(names <= set(sys.stdlib_module_names), names)


if __name__ == '__main__':
    unittest.main()
