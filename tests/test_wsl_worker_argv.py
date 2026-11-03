"""A worker's command crosses into WSL as a file: Windows caps wsl.exe's command line, and a Claude worker's
system prompt and settings alone go past that cap."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from drover import launch, placement, runner


class WslWorkerCommand(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.launcher = launch.RunnerLauncher(runs_dir=self.root, windows=True, distro='U')
        self.spec = dict(run_id='r1', nonce='a' * 32, placement='wsl', cwd='/home/x/work', backend='claude')

    def test_a_long_prompt_stays_off_the_wsl_command_line(self):
        worker = ['/bin/claude', '--append-system-prompt', 'rules ' * 2000, '--model', 'm']
        with patch.object(self.launcher, 'wsl_exe', side_effect=lambda argv: argv):
            command, _ = self.launcher.command(self.spec, self.root, worker)
        line = subprocess.list2cmdline(command)
        self.assertNotIn('rules', line)
        self.assertLess(len(line.encode('utf-16-le')), 8192)
        self.assertEqual(json.loads((self.root / launch.ARGV_FILE).read_text(encoding='utf-8')), worker)
        self.assertIn(placement.to_wsl_path(str(self.root / launch.ARGV_FILE)), command)

    def test_a_windows_worker_keeps_its_command_inline(self):
        self.spec['placement'] = 'windows'
        self.spec['cwd'] = str(self.root)
        with patch.object(placement, 'native_runner_cwd', side_effect=lambda cwd, windows: cwd), \
                patch.object(self.launcher, 'windows_exe', side_effect=lambda exe: exe):
            command, _ = self.launcher.command(self.spec, self.root, ['claude', '-p'])
        self.assertEqual(command[-3:], ['--', 'claude', '-p'])
        self.assertFalse((self.root / launch.ARGV_FILE).exists())


class RunnerReadsTheFile(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.file = self.root / 'argv.json'
        self.worker = ['claude', '--append-system-prompt', 'line one\nline "two" é']
        self.file.write_text(json.dumps(self.worker), encoding='utf-8')
        self.head = ['--run-id', 'r1', '--runs-dir', str(self.root), '--nonce', 'a' * 32]

    def test_supervises_the_worker_named_in_the_file(self):
        with patch.object(runner, 'supervise', return_value=dict(state='exited', exit_code=0)) as supervise:
            self.assertEqual(runner.main([*self.head, '--argv-file', str(self.file)]), 0)
        self.assertEqual(supervise.call_args.args[2], self.worker)

    def test_detach_hands_the_worker_on(self):
        with patch.object(runner, 'detach', return_value={}) as detach:
            self.assertEqual(runner.main([*self.head, '--detach', '--argv-file', str(self.file)]), 0)
        passed = detach.call_args.args[2]
        self.assertEqual(passed[passed.index('--') + 1:], self.worker)

    def test_an_empty_file_is_refused(self):
        self.file.write_text('[]', encoding='utf-8')
        with self.assertRaises(SystemExit):
            runner.main([*self.head, '--argv-file', str(self.file)])


if __name__ == '__main__':
    unittest.main()
