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
from drover import decisions


class Records(unittest.TestCase):
    def assert_concurrent_records(self, state, paths, count):
        self.assertEqual(len(set(paths)), count)
        self.assertEqual(len(list((state / 'decisions').glob('*.md'))), count)
        self.assertEqual(decisions._git(state, 'rev-list', '--count', 'HEAD'), str(count))
        self.assertEqual(decisions._git(state, 'status', '--porcelain'), '')
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
