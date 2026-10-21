"""Fresh CI defers under hold; saved executions and exact-commit recovery continue."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.disk_fixture import DiskCase
from tests.gitfix import Repo


class Admission(DiskCase):
    def setUp(self):
        super().setUp()
        mapping = patch('drover.placement.physical_disk_path', return_value=str(self.root))
        mapping.start()
        self.addCleanup(mapping.stop)

    def fixture(self):
        from drover import ci, config, repos
        from drover.ledger import connect
        repo = Repo()
        self.addCleanup(repo.cleanup)
        repo.commit('.drover/scripts/test.ps1', 'exit 0\n')
        repo.commit('.drover/config.toml', config.dumps({'schema_version': 1,
                    'scripts': {'test': '.drover/scripts/test.ps1'},
                    'run': {'platforms': ['wsl'], 'default': 'wsl'},
                    'gate': {'platform': 'wsl', 'docs_only': False}}))
        repo.git('checkout', '-b', 'topic')
        commit = repo.commit('change.txt', 'exact commit\n')
        prog = repos.load({'name': 'fixture', 'repo': str(repo.repo)})
        conn = connect(self.root / 'ci.db')
        self.addCleanup(conn.close)
        calls = []
        def execute(spec):
            calls.append(('launch', spec))
            d = Path(self.root / 'runs' / str(spec['run_id']))
            plan = json.loads((d / 'plan.json').read_text())
            (d / 'receipt.json').write_text(json.dumps({'version': 1, 'passed': True, 'status': 'passed',
                    'sha': plan['sha'], 'kind': plan['kind'], 'platform': plan['platform'],
                    'exit_code': 0, 'tree_unchanged': True, 'log_tail': []}))
            return SimpleNamespace(poll=lambda: {'status': 'done', 'returncode': 0})
        def adopt(spec):
            handle = execute(spec)
            calls[-1] = ('adopt', spec)
            return handle
        launcher = SimpleNamespace(launch=execute, adopt=adopt)
        service = ci.CI(self.root / 'runs', launcher=launcher)
        return repo, commit, prog, conn, calls, service

    def test_hold_blocks_checks_and_full_ci_running_work_winds_down(self):
        from drover import disk, ci, runner
        from drover.ledger import ci as slots
        repo, commit, prog, conn, calls, service = self.fixture()
        real_read = disk.read
        with patch.object(disk, 'read', side_effect=lambda cfg=None, **kw: real_read(self.cfg, reader=lambda p: 49)):
            for kind in ('checks', 'full'):
                with self.assertRaises(ci.CIInterrupted):
                    service.run(prog, repo.repo, commit, kind=kind, conn=conn)
                self.assertEqual(calls, [])
                self.assertEqual(slots.lease_queue(conn), [])
            requests = list((self.root / 'runs').glob('ci-*.json'))
            self.assertEqual(len(requests), 2)
            saved = json.loads(next(p for p in requests if json.loads(p.read_text())['kind'] == 'full').read_text())
            spec = saved['context']['spec']
            runner.write_json_atomic(runner.run_dir(service.runs_dir, spec['run_id']) / runner.STATUS_FILE,
                                     {'state': 'running', 'run_id': spec['run_id'], 'nonce': spec['nonce']})
            receipt = service.run(prog, repo.repo, commit, kind='full', conn=conn)
            self.assertTrue(receipt['passed'])
            self.assertEqual([kind for kind, spec in calls], ['adopt'])
            self.assertEqual(slots.lease_queue(conn), [])

    def test_hold_release_resumes_same_commit_without_failure_or_slot_leak(self):
        from drover import disk, ci
        from drover.ledger import ci as slots, ops
        repo, commit, prog, conn, calls, service = self.fixture()
        real_read = disk.read
        self.free = 49
        with patch.object(disk, 'read', side_effect=lambda cfg=None, **kw: real_read(self.cfg, reader=lambda p: self.free)):
            with self.assertRaises(ci.CIInterrupted):
                service.run(prog, repo.repo, commit, conn=conn)
            request, = (self.root / 'runs').glob('ci-*.json')
            saved = json.loads(request.read_text())
            self.assertEqual(saved['commit'], commit)
            run_id = saved['context']['spec']['run_id']
            self.assertEqual(slots.lease_queue(conn), [])
            self.assertEqual(ops.list_runs(conn, 'nonexistent'), [])
            self.free = 180  # Resolver: next healthy physical reading and ordinary retry.
            receipt = service.run(prog, repo.repo, commit, conn=conn)
        self.assertTrue(receipt['passed'])
        self.assertEqual(receipt['sha'], commit)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]['run_id'], run_id)
        self.assertEqual(slots.lease_queue(conn), [])
        self.assertFalse(request.exists())
        self.assertEqual(ops.events(conn, kind='gate-failed'), [])
