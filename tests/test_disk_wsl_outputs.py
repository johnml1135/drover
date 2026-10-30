"""Windows hub requests must retain the WSL distro's physical disk evidence."""
import io
import json
import os
import subprocess
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from drover import config, disk_paths, disk_side, placement
from drover import placement_host, placement_paths, placement_worker
from tests.disk_fixture import DiskCase
from tests.disk_memory import MemoryWorktreeCase


class OutputEligibility(DiskCase):
    def test_each_failed_condition_is_named(self):
        tree = self.root / 'tree'
        tree.mkdir()
        machine = dict(repo=str(self.root / 'repo'))
        entry = dict(worktree=str(tree), locked=self.side)
        request = dict(drive=dict(path='H:/'), now=self.now, apply=False)
        cases = [('main_checkout', dict(repo=str(tree)), {}, {}),
                 ('not_directory', {}, dict(worktree=str(tree / 'missing')), {}),
                 ('unsafe_path', {}, {}, dict(safe=False)),
                 ('owner_side', {}, dict(locked=self.other_side), {}),
                 ('physical_drive', {}, {}, dict(contains=False))]
        for condition, repo_change, entry_change, flags in cases:
            with self.subTest(condition=condition), \
                    patch.object(disk_paths, 'safe_path', return_value=flags.get('safe', True)), \
                    patch.object(placement, 'cleanup_contains', return_value=flags.get('contains', True)), \
                    patch.object(placement, 'physical_disk_path', return_value=None):
                with self.assertRaisesRegex(ValueError, condition):
                    disk_paths.output_evidence(request, {**machine, **repo_change}, {**entry, **entry_change})

    def test_worker_receipt_records_request_and_execution_context(self):
        request = dict(_worker_id='trace', state_dir=str(self.state), stage=5, side='wsl',
                       apply=False, drive=dict(path='H:/'), repos=[], paths={})
        answer = dict(items=[], errors=[], reclaimed_bytes=0)
        with patch.object(placement_worker, 'IS_WINDOWS', False), \
                patch.object(placement_worker, 'lower_priority'), patch.object(os, 'setsid', create=True), \
                patch.object(disk_side, 'serve', return_value=answer), patch('sys.stdout'), \
                patch.dict(os.environ, WSL_DISTRO_NAME='Ubuntu', DROVER_VHDX_DIR='/mnt/h/'):
            placement_worker.cleanup_worker(request)
        receipt = json.loads((self.state / 'disk' / 'worker-trace.json').read_text())
        self.assertEqual(receipt['request']['drive'], request['drive'])
        self.assertEqual(receipt['request']['stage'], 5)
        self.assertEqual(receipt['context']['cwd'], os.getcwd())
        self.assertEqual(receipt['context']['distro'], 'Ubuntu')
        self.assertEqual(receipt['context']['vhdx_dir'], '/mnt/h/')


@unittest.skipIf(os.name == 'nt', 'replays a WSL filesystem worker on Linux')
class WindowsHubOutputs(MemoryWorktreeCase):
    def test_windows_hub_dry_run_without_worker_registry_interop(self):
        self.r.commit('.drover/config.toml', config.dumps({
            'schema_version': 1, 'cleanup': {'disposable': ['**/bin', '**/obj']}}))
        tree = self.worktree('idle-settled', lock='wsl')
        output = self.file(tree / 'src' / 'bin' / 'payload')
        self.age(tree, 4 * 86400)
        machine = dict(name='alpha', repo=str(self.r.repo), worktrees=str(self.trees), default_branch='main')
        request = dict(stage=5, mode='on-demand', apply=False, now=self.now, repos=[machine],
                       drive=dict(path='H:/', level='warn'), paths={}, state_dir=str(self.state))
        captured, receipts = [], []
        real_run = subprocess.run

        def launch(argv, **kwargs):
            payload = json.loads(kwargs['input'])
            captured.append(payload)
            child_env = dict(os.environ, WSL_DISTRO_NAME='Ubuntu')
            child_env.pop('DROVER_VHDX_DIR', None)
            # Replay the exact serialized request and env argv sent to wsl.exe.
            env_at = argv.index('env') + 1
            for value in argv[env_at:]:
                if '=' not in value:
                    break
                key, _, val = value.partition('=')
                child_env[key] = val
            with ExitStack() as stack:
                for module in (placement, placement_paths, placement_host):
                    stack.enter_context(patch.object(module, 'IS_WINDOWS', False))
                stack.enter_context(patch.dict(os.environ, child_env, clear=True))
                stack.enter_context(patch.object(placement_host, 'REG', str(self.root / 'no-reg.exe')))
                stack.enter_context(patch.object(subprocess, 'run', real_run))
                stack.enter_context(patch.object(placement_worker, 'lower_priority'))
                stack.enter_context(patch.object(os, 'setsid'))
                stream = stack.enter_context(patch('sys.stdout', new_callable=io.StringIO))
                placement_worker.cleanup_worker(payload)
                receipt = self.state / 'disk' / ('worker-' + payload['_worker_id'] + '.json')
                receipts.append(json.loads(receipt.read_text()))
            return SimpleNamespace(returncode=0, stdout=stream.getvalue(), stderr='')

        with ExitStack() as stack:
            for module in (placement, placement_paths, placement_host):
                stack.enter_context(patch.object(module, 'IS_WINDOWS', True))
            stack.enter_context(patch.object(placement, 'distro', return_value='Ubuntu'))
            registry = stack.enter_context(patch.object(placement, 'detect_vhdx_drives',
                                                         return_value={'Ubuntu': 'H:', 'Other': 'C:'}))
            stack.enter_context(patch.object(placement, 'cleanup_processes', return_value=SimpleNamespace(busy=lambda p: False)))
            stack.enter_context(patch.object(placement, 'recover_ci_children', return_value=True))
            stack.enter_context(patch.object(subprocess, 'run', side_effect=launch))
            answer = placement.cleanup_call('wsl', request)
            # A different physical drive must still retain the output.
            other = placement.cleanup_call('wsl', dict(request, drive=dict(path='C:/', level='warn')))
            # A different distro's mapping cannot stand in for unknown evidence.
            registry.return_value = {'Other': 'H:'}
            unknown = placement.cleanup_call('wsl', request)
        candidates = [i for i in answer['items'] if i['action'] == 'candidate']
        self.assertTrue(candidates, answer)
        self.assertIn(str(output), [i['path'] for i in candidates])
        self.assertFalse(any(i['action'] == 'candidate' for i in other['items']))
        self.assertTrue(any('physical_drive' in i.get('error', '') for i in other['items']))
        self.assertFalse(any(i['action'] == 'candidate' for i in unknown['items']))
        self.assertTrue(any('host=None' in i.get('error', '') for i in unknown['items']))
        self.assertEqual(captured[0]['drive']['path'], 'H:/')
        self.assertEqual(receipts[0]['request']['drive'], request['drive'])
        self.assertEqual(receipts[0]['context']['vhdx_dir'], '/mnt/h/')
        self.assertIsNone(receipts[-1]['context']['vhdx_dir'])
        self.assertTrue(output.exists(), 'dry run must preserve declared outputs')
