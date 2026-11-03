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

from drover.disk_stage import StageRequest
from drover import config, disk_paths, disk_runtime, disk_side, git_worktrees, placement
from drover import placement_host, placement_paths, placement_worker, process
from tests.disk_budget_fixture import BudgetCase
from tests.disk_fixture import DiskCase, placement_processes
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
        real_run = process.subprocess.run

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
                stack.enter_context(patch.object(process.subprocess, 'run', real_run))
                stack.enter_context(patch.object(placement_worker, 'lower_priority'))
                stack.enter_context(patch.object(os, 'setsid'))
                stream = stack.enter_context(patch('sys.stdout', SimpleNamespace(buffer=io.BytesIO())))
                stack.enter_context(placement_processes())
                placement_worker.cleanup_worker(payload)
                receipt = self.state / 'disk' / ('worker-' + payload['_worker_id'] + '.json')
                receipts.append(json.loads(receipt.read_text()))
            return SimpleNamespace(returncode=0, stdout=stream.buffer.getvalue(), stderr=b'')

        with ExitStack() as stack:
            for module in (placement, placement_paths, placement_host):
                stack.enter_context(patch.object(module, 'IS_WINDOWS', True))
            stack.enter_context(patch.object(placement, 'distro', return_value='Ubuntu'))
            registry = stack.enter_context(patch.object(placement, 'detect_vhdx_drives',
                                                         return_value={'Ubuntu': 'H:', 'Other': 'C:'}))
            stack.enter_context(patch.object(placement, 'cleanup_processes', return_value=SimpleNamespace(busy=lambda p: False)))
            stack.enter_context(patch.object(placement, 'recover_ci_children', return_value=True))
            stack.enter_context(patch.object(process.subprocess, 'run', side_effect=launch))
            answer = disk_runtime.cleanup_call('wsl', StageRequest.from_wire(request))
            # A different physical drive must still retain the output.
            other = disk_runtime.cleanup_call('wsl', StageRequest.from_wire(dict(request, drive=dict(path='C:/', level='warn'))))
            # A different distro's mapping cannot stand in for unknown evidence.
            registry.return_value = {'Other': 'H:'}
            unknown = disk_runtime.cleanup_call('wsl', StageRequest.from_wire(request))
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


class OutputStageCost(BudgetCase):
    """Stage 5 must finish on a busy machine: registrations are listed once per run, and a deadline ends it early."""
    def trees_with(self, count, units):
        for i in range(count):
            tree = self.worktree(f'w{self.tree_count}')
            (tree / 'generated').mkdir()
            for u in range(units):
                path = tree / 'generated' / f'unit-{u}'
                path.write_text('x')
                self.outputs.append(path)
            self.age(tree, 4 * 86400)

    def listings(self, **paths):
        real, calls = git_worktrees.listing, []

        def counting(repo, **options):
            calls.append(repo)
            return real(repo, **options)
        with patch.object(git_worktrees, 'listing', side_effect=counting):
            self.stage5(**paths)
        return len(calls)

    def test_outputs_list_registrations_once_per_run(self):
        self.trees_with(3, 2)
        few = self.listings()
        self.trees_with(3, 6)
        many = self.listings()
        self.assertEqual(few, many)
        self.assertEqual(few, 1)

    def test_outputs_deadline_returns_partial_report_instead_of_worker_timeout(self):
        self.trees_with(4, 1)
        self.clock_step = 20.       # deadline 20 + 0.8 * 100 s; the repo, then each worktree, takes 20 s to reach
        answer = self.stage5(worker_timeout_s=100)
        late = [i for i in answer['items'] if i.get('reason_code') == 'output_deadline']
        self.assertEqual(len(late), 1)
        self.assertEqual(answer['errors'], [])
        done = [i for i in answer['items'] if i.get('reason_code') == 'declared_disposable']
        self.assertTrue(done)
        self.assertLess(len(done), 4)
        evaluated = {Path(i['root']).name for i in done}
        left = {Path(name).name for name in late[0]['worktrees']}
        self.assertFalse(evaluated & left)
        self.assertEqual(len(left) + len(evaluated), 4)

    def test_deadline_between_units_of_one_worktree_names_it_and_the_rest(self):
        self.trees_with(2, 3)
        self.clock_step = 25.       # the deadline, 25 + 80 s, passes after the second unit of the first worktree
        answer = self.stage5(apply=True, worker_timeout_s=100)
        late = [i for i in answer['items'] if i.get('reason_code') == 'output_deadline']
        done = [i for i in answer['items'] if i.get('reason_code') == 'declared_disposable']
        self.assertEqual((len(late), len(done), answer['errors']), (1, 2, []))
        self.assertEqual({Path(i['root']).name for i in done}, {Path(late[0]['worktrees'][0]).name})
        self.assertEqual(len(late[0]['worktrees']), 2)
        self.assertEqual(late[0]['repos'], [])
        self.assertEqual(late[0]['count'], 2)

    def test_a_failed_listing_stays_failed_for_the_stage_and_keeps_the_repo(self):
        self.trees_with(2, 2)
        real, calls = git_worktrees.listing, []

        def flaky(repo, **options):
            calls.append(repo)
            if len(calls) == 1:
                raise git_worktrees.RegistrationsUnknown('listing failed')
            return real(repo, **options)
        with patch.object(git_worktrees, 'listing', side_effect=flaky):
            answer = self.stage5()
        self.assertEqual([e['reason_code'] for e in answer['errors']], ['repository_unavailable'])
        self.assertEqual([i['reason_code'] for i in answer['items'] if i.get('path') == str(self.r.repo)], ['repository_unavailable'])
        self.assertFalse([i for i in answer['items'] if i.get('reason_code') == 'declared_disposable'])
        self.assertEqual(len(calls), 1)
