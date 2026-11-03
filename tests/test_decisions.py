"""Decision records and local config sync use disposable git repos."""
import io
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from drover import config
from drover import decisions, process
from tests.fakeci import FakeCommands, process_options


class Git(unittest.TestCase):
    def test_git_runs_through_process_and_any_failure_is_a_decision_error(self):
        with tempfile.TemporaryDirectory() as root:
            fake, state = FakeCommands(root), Path(root, 'state')
            argv = ['git', '-C', str(state), '-c', 'user.name=Drover', '-c', 'user.email=drover@example.invalid', 'log']
            fake.expect(argv, output=b'caf\xe9 \xc3\xa9\r\n', **process_options(30))
            fake.expect(argv, returncode=128, stderr_output=b'fatal: bad\n', **process_options(30))
            fake.expect(argv, error=subprocess.TimeoutExpired(argv, 30), **process_options(30))
            fake.expect(argv, error=FileNotFoundError(2, 'No such file'), **process_options(30))
            with fake.patch(process), patch('subprocess.run', side_effect=AssertionError('real process')):
                self.assertEqual(decisions._out(state, 'log'), 'caf� \xe9')      # UTF-8, errors replaced
                for message in ('^fatal: bad$', '^git: timed out after 30 s$', '^git: git: No such file or directory$'):
                    with self.subTest(message), self.assertRaisesRegex(decisions.DecisionError, message):
                        decisions._out(state, 'log')

    def test_git_failure_is_decision_error_at_config_475_and_decisions_88(self):
        """A push that times out is decisions.py's warning, and a failed sync is config's `drover config sync:` exit 2,
        not the OSError handler beneath it: both need DecisionError, whatever the failure."""
        from tests.fakeci import git_answers

        def reply(repo, *args, timeout):
            words = [a for a in args if not a.startswith(('-c', 'user.'))]
            if words[0] in failing:
                raise subprocess.TimeoutExpired(['git', *args], timeout)
            return subprocess.CompletedProcess(args, 0, 'changed\n' if words[0] == 'diff' else '', '')
        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            (state / '.git').mkdir()
            (state / 'config.toml').write_text('[config_sync]\nauto_push = true\nremote = "./elsewhere"\n')
            failing = ('push',)
            with git_answers(reply):
                result = decisions.sync(state)
            self.assertEqual((result['committed'], result['pushed']), (True, False))
            self.assertEqual(result['warning'], 'push failed; local history is safe: git: timed out after 30 s')
            failing = ('status',)
            with git_answers(reply), patch.object(config, 'load', return_value=config.validate({'paths': {'state_dir': d}})), \
                    patch('sys.stderr', new_callable=io.StringIO) as err:
                self.assertEqual(config.main(['sync']), 2)
            self.assertEqual(err.getvalue(), 'drover config sync: git: timed out after 30 s\n')


class Records(unittest.TestCase):
    def assert_concurrent_records(self, state, paths, count):
        self.assertEqual(len(set(paths)), count)
        self.assertEqual(len(list((state / 'decisions').glob('*.md'))), count)
        self.assertEqual(decisions._out(state, 'rev-list', '--count', 'HEAD'), str(count))
        self.assertEqual(decisions._out(state, 'status', '--porcelain'), '')
        for path in paths:
            self.assertTrue(Path(path).is_file())

    def test_concurrent_threads_keep_every_record_and_commit(self):
        with tempfile.TemporaryDirectory() as d:
            state = decisions.initialize(d)
            start = threading.Barrier(8)

            def write(number):
                start.wait(timeout=10)
                return decisions.record(state, 'Worker cap', f'Reason {number}', {'cap': number}, 'tuner')

            with ThreadPoolExecutor(max_workers=8) as pool:
                paths = list(pool.map(write, range(8)))
            self.assert_concurrent_records(state, paths, 8)
            latest = max(paths, key=lambda p: len(p.read_text()))
            for number in range(8):
                self.assertIn(f'Reason {number}', latest.read_text())

    def test_concurrent_processes_keep_every_record_and_commit(self):
        script = ('import sys\nfrom drover import decisions\n'
                  'print("ready", flush=True)\nsys.stdin.readline()\n'
                  'print(decisions.record(sys.argv[1], "Worker cap", sys.argv[2], {}, "tuner"))\n')
        with tempfile.TemporaryDirectory() as d:
            state = decisions.initialize(d)
            processes = [subprocess.Popen([sys.executable, '-c', script, d, f'Reason {i}'],
                                          stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE, text=True) for i in range(4)]
            try:
                for proc in processes:
                    self.assertEqual(proc.stdout.readline().strip(), 'ready')
                for proc in processes:
                    proc.stdin.write('\n')
                    proc.stdin.flush()
                paths = []
                for proc in processes:
                    out, err = proc.communicate(timeout=30)
                    self.assertEqual(proc.returncode, 0, err)
                    paths.append(out.strip())
                self.assert_concurrent_records(state, paths, 4)
            finally:
                for proc in processes:
                    if proc.poll() is None:
                        proc.kill()
                    proc.communicate()

    def test_records_are_committed_with_prior_reasons_and_policy_version(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            decisions.initialize(state)
            (state / 'config.toml').write_text('[config_sync]\nauto_push = true\nremote = ""\n')
            (state / 'policy.json').write_text(json.dumps({'version': 'p7'}))
            first = decisions.record(state, 'Worker cap', 'Keep memory free', 'cap = 2', 'owner')
            second = decisions.record(state, 'Worker cap', 'More memory available', 'cap = 3', 'tuner')
            self.assertNotEqual(first, second)
            text = second.read_text()
            self.assertIn('author: tuner', text)
            self.assertIn('policy_version: "p7"', text)
            self.assertIn('Keep memory free', text)
            self.assertIn(first.name, text)
            status = subprocess.run(['git', '-C', d, 'status', '--porcelain'], capture_output=True, text=True, check=True)
            self.assertEqual(status.stdout, '')
            (state / 'ledger.db').write_text('runtime')
            (state / 'hub-token').write_text('test secret')
            (state / 'config.toml').write_text('[config_sync]\nauto_push = true\nremote = ""\n# changed\n')
            result = decisions.sync(state)
            self.assertTrue(result['committed'])
            self.assertFalse(result['pushed'])
            tracked = subprocess.run(['git', '-C', d, 'ls-files'], capture_output=True, text=True, check=True).stdout
            self.assertNotIn('ledger.db', tracked)
            self.assertNotIn('hub-token', tracked)

    def test_push_failure_is_reported_and_keeps_local_commit(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            decisions.initialize(state)
            (state / 'config.toml').write_text('[config_sync]\nauto_push = true\nremote = "./missing-remote"\n')
            result = decisions.sync(state)
            self.assertTrue(result['committed'])
            self.assertFalse(result['pushed'])
            self.assertIn('push failed', result['warning'])

    def test_config_sync_cli(self):
        with tempfile.TemporaryDirectory() as d:
            decisions.initialize(d)
            (Path(d) / 'config.toml').write_text('# pending\n')
            with patch.object(config, 'load', return_value=config.validate({'paths': {'state_dir': d}})), patch('sys.stdout', new_callable=io.StringIO) as out:
                self.assertEqual(config.main(['sync', '--json']), 0)
                self.assertTrue(json.loads(out.getvalue())['committed'])


class IgnoreTopUp(unittest.TestCase):
    def test_sync_tops_up_an_existing_gitignore_once_and_a_stray_temp_stays_out(self):
        with tempfile.TemporaryDirectory() as d:
            state = decisions.initialize(d)
            ignore = state / '.gitignore'
            ignore.write_text('private/\n' + ignore.read_text().replace('*.tmp\n', '').rstrip('\n'))
            decisions._out(state, 'add', '--all', '--', '.')
            decisions._out(state, 'commit', '-m', 'A repo from before the top-up')
            stray = state / 'allowance' / 'manual-claude.json.abc123.tmp'
            stray.parent.mkdir()
            stray.write_text('half a write')

            first, second = decisions.sync(state), decisions.sync(state)

            self.assertEqual((first['committed'], second['committed']), (True, False))
            lines = ignore.read_text().splitlines()
            self.assertEqual((lines.count('*.tmp'), lines[0], lines.count('hub-token')), (1, 'private/', 1))
            self.assertEqual(decisions._out(state, 'rev-list', '--count', 'HEAD'), '2')
            self.assertEqual(decisions._out(state, 'status', '--porcelain'), '')
            self.assertNotIn('.tmp', decisions._out(state, 'ls-files'))
            self.assertTrue(stray.exists())
            record = next((state / 'decisions').glob('*.md')).read_text()
            self.assertIn('.gitignore', record)
            self.assertNotIn('manual-claude', record)

    def old_repo(self, d, ignore_bytes):
        state = decisions.initialize(d)
        (state / '.gitignore').write_bytes(ignore_bytes)
        decisions._out(state, 'add', '--all', '--', '.')
        decisions._out(state, 'commit', '-m', 'A repo from before the top-up')
        return state

    def test_a_non_utf8_ignore_file_keeps_its_bytes_and_gains_the_line(self):
        with tempfile.TemporaryDirectory() as d:
            old = b'# caf\xe9\n' + decisions.IGNORE.replace('*.tmp\n', '').encode()
            state = self.old_repo(d, old)
            decisions.sync(state)
            self.assertEqual((state / '.gitignore').read_bytes().replace(b'\r\n', b'\n'), old + b'*.tmp\n')
            decisions.record(state, 'Worker cap', 'Keep memory free', 'cap = 2', 'owner')
            self.assertEqual(decisions._out(state, 'rev-list', '--count', 'HEAD'), '3')
            self.assertEqual(decisions._out(state, 'status', '--porcelain'), '')

    def test_an_unreadable_ignore_file_is_a_warning_and_the_record_still_commits(self):
        with tempfile.TemporaryDirectory() as d:
            state = self.old_repo(d, decisions.IGNORE.replace('*.tmp\n', '').encode())
            real = Path.read_text

            def refused(path, *args, **kwargs):
                if path.name == '.gitignore':
                    raise PermissionError(13, 'refused', str(path))
                return real(path, *args, **kwargs)

            with patch.object(Path, 'read_text', refused), self.assertLogs('drover.decisions', 'WARNING') as logs:
                decisions.record(state, 'Worker cap', 'Keep memory free', 'cap = 2', 'owner')
            self.assertIn(str(state / '.gitignore'), '\n'.join(logs.output))
            self.assertEqual(decisions._out(state, 'rev-list', '--count', 'HEAD'), '2')
            self.assertNotIn('*.tmp', (state / '.gitignore').read_text().splitlines())


class HistoryIntegration(unittest.TestCase):
    def test_effective_config_disables_push_and_owner_sync_records_a_reason(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            decisions.initialize(state)
            (state / 'config.toml').write_text('[config_sync]\nremote="./missing"\nauto_push=true\n')
            cfg = config.validate({'paths': {'state_dir': d}, 'config_sync': {'auto_push': False}})
            with patch.object(config, 'load', return_value=cfg), patch('sys.stdout', new_callable=io.StringIO) as out:
                self.assertEqual(config.main(['sync', '--json', '--reason', 'Keep local history during review']), 0)
                result = json.loads(out.getvalue())
            self.assertNotIn('warning', result)
            records = list((state / 'decisions').glob('*.md'))
            self.assertEqual(len(records), 1)
            self.assertIn('Keep local history during review', records[0].read_text())

    def test_legacy_config_key_is_translated_with_a_warning(self):
        with self.assertLogs('drover.config', 'WARNING'):
            cfg = config.validate({'programs_dir': '/tmp/private-repos'})
        self.assertEqual(cfg['repos_dir'], '/tmp/private-repos')


class OwnerHistory(unittest.TestCase):
    def test_dispatcher_settings_are_never_attributed_to_owner(self):
        from hub.store import Store
        with tempfile.TemporaryDirectory() as d:
            state = decisions.initialize(d)
            with closing(Store(state / 'ledger.db')) as store:
                cfg = config.validate({'paths': {'state_dir': d}})
                with patch.object(config, 'get', return_value=cfg):
                    store.update_settings({'note': 'Worker observation'}, author='dispatcher')
            records = list((state / 'decisions').glob('*.md'))
            self.assertEqual(len(records), 1)
            text = records[0].read_text()
            self.assertIn('author: dispatcher', text)
            self.assertIn('Dispatcher changed hub settings.', text)
            self.assertNotIn('Owner changed', text)

    def test_owner_settings_and_tuner_answers_are_recorded(self):
        from hub.store import Store
        from drover.ledger import ops
        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            decisions.initialize(state)
            with closing(Store(state / 'ledger.db')) as store:
                cfg = config.validate({'paths': {'state_dir': d}})
                with patch.object(config, 'get', return_value=cfg):
                    store.update_settings({'parallel_cap': '3'})
                    epic = store.create_epic('drover', 'Tuner proposals')
                    ops.set_setting(store.conn, 'tuner_epic', epic['id'])
                    question = store.ask_question('epic', epic['id'], 'Adjust criteria?', options=[{'label': 'Approve'}])
                    store.answer_question(question['id'], 'Approve', 'The evidence supports the change')
            texts = [p.read_text() for p in (state / 'decisions').glob('*.md')]
            self.assertEqual(len(texts), 2)
            self.assertTrue(all('author: owner' in text for text in texts))
            self.assertTrue(any('The evidence supports the change' in text for text in texts))
            self.assertEqual(json.loads((state / 'settings.json').read_text())['parallel_cap'], '3')
