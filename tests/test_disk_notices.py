"""Cleanup notices survive transport failure and enter the existing Inbox once."""
import json
from pathlib import Path
from unittest.mock import patch

from tests.disk_memory import MemoryWorktreeCase


class Notices(MemoryWorktreeCase):
    def setUp(self):
        super().setUp()
        (self.repos_dir / 'alpha.toml').unlink()
        mapping = patch('drover.placement.physical_disk_path', return_value=str(self.root))
        mapping.start()
        self.addCleanup(mapping.stop)

    def durable_notices(self):
        found = {}
        def visit(value):
            if isinstance(value, dict):
                if 'id' in value and value.get('kind') in ('budget', 'exhaustion'):
                    found[value['id']] = value
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        for path in (self.state / 'disk').rglob('*'):
            if path.is_file() and path.suffix in ('.json', '.jsonl'):
                for line in ([path.read_text()] if path.suffix == '.json' else path.read_text().splitlines()):
                    try:
                        visit(json.loads(line))
                    except ValueError:
                        pass
        return list(found.values())

    def outbox(self):
        values = []
        for path in (self.state / 'disk').rglob('*'):
            if path.is_file() and path.suffix in ('.json', '.jsonl'):
                for line in ([path.read_text()] if path.suffix == '.json' else path.read_text().splitlines()):
                    try:
                        values.append(json.loads(line))
                    except ValueError:
                        pass
        return json.dumps(values)

    def test_exhaustion_only_toast_and_durable_inbox(self):
        from drover import disk_cleanup
        from drover.ledger import connect, ops, disk_notices
        self.approve_report()
        # Every earlier/non-applied result is silent, even if free space stays low.
        for mode, apply, free in (('on-demand', False, 80), ('daily', True, 80),
                                 ('triggered', True, 180), ('triggered', True, None)):
            self.free = free
            self.toasts.clear()
            disk_cleanup.run(self.cfg, mode=mode, apply=apply, runtime=self.runtime())
            self.assertEqual(self.toasts, [], (mode, apply, free))
        self.free = 80
        rt = self.runtime()
        def broken_toast(notice):
            self.assertIn(notice['id'], self.outbox(), 'outbox precedes transport')
            self.toasts.append(notice)
            raise OSError('no interactive desktop')
        rt = disk_cleanup.CleanupRuntime(rt.now, rt.read, rt.call, broken_toast)
        result = disk_cleanup.run(self.cfg, mode='triggered', apply=True, runtime=rt)
        self.assertTrue(result['exhausted'])
        self.assertEqual(len(self.toasts), 1)
        notice = self.toasts[0]
        self.assertIn(notice['id'], self.outbox())
        conn = connect(self.root / 'inbox.db')
        self.addCleanup(conn.close)
        first = disk_notices.publish(conn, notice)
        self.assertEqual(disk_notices.publish(conn, notice), first)
        self.assertEqual(len(ops.events(conn, kind='disk.notice')), 1)
        epics = ops.list_epics(conn)
        self.assertEqual(len(epics), 1)
        self.assertEqual(epics[0]['repo'], '')
        self.assertEqual(len(ops.open_questions(conn)), 1)
        self.assertEqual(ops.events(conn, kind='disk.notice')[0]['actor'], 'disk')
        self.assertEqual(ops.list_tasks(conn), [], 'notice does not create a waiting task')
        disk_cleanup.run(self.cfg, mode='triggered', apply=True, runtime=self.runtime())
        self.assertEqual(len(self.toasts), 1, 'same exhausted drive episode is deduplicated')

    def test_budget_alert_and_duplicate_replay(self):
        from drover.ledger import connect, ops, disk_notices
        from drover import config, disk_cleanup, ownership
        repo = self.new_repo('notice-budget')
        repo.commit('.drover/config.toml', config.dumps({'schema_version': 1}))
        trees = repo.root / 'budget.worktrees'
        machine = {'name': 'fixture', 'repo': str(repo.repo), 'worktrees': str(trees),
                   'remote': 'origin', 'default_branch': 'main', 'budget_gb': 0.00075}
        (self.repos_dir / 'fixture.toml').write_text(config.dumps(machine))
        (repo.repo / 'owned payload').write_bytes(b'x' * (512 * 1024))
        outside = self.root / 'outside payload'
        outside.write_bytes(b'z' * (2 * 1024 * 1024))
        link = repo.repo / 'outside link'
        try:
            link.symlink_to(outside)
        except OSError:
            pass
        self.approve_report()
        self.run_cleanup(apply=True, real=True)
        self.assertFalse(any(n.get('kind') == 'budget' for n in self.durable_notices()),
                         'external symlink target is not charged to the repo')
        wt = trees / 'second physical directory'
        repo.add(wt, branch='budget-topic', lock=self.side)
        ownership.tag(str(wt), 'fixture', 'session:budget', self.now + 86400, now=self.now)
        (wt / 'second payload').write_bytes(b'y' * (512 * 1024))
        self.run_cleanup(apply=True, real=True)
        notices = [n for n in self.durable_notices() if n.get('kind') == 'budget']
        self.assertEqual(len(notices), 1, 'sum distinct physical directories against the repo budget')
        notice = notices[0]
        self.assertEqual(notice['repo'], 'fixture')
        self.assertIn(notice['id'], self.outbox())
        self.run_cleanup(apply=True, real=True)
        self.assertEqual(len([n for n in self.durable_notices() if n.get('kind') == 'budget']), 1)
        self.assertEqual(outside.stat().st_size, 2 * 1024 * 1024)
        conn = connect(self.root / 'budget.db')
        self.addCleanup(conn.close)
        first = disk_notices.publish(conn, notice)
        for _ in range(3):
            self.assertEqual(disk_notices.publish(conn, dict(notice)), first)
        self.assertEqual(len(ops.events(conn, kind='disk.notice')), 1)
        self.assertEqual(len(ops.open_questions(conn)), 1)
        self.assertEqual(len(ops.list_epics(conn)), 1)
        other = {**notice, 'id': 'budget:other:episode-1', 'repo': 'other'}
        disk_notices.publish(conn, other)
        self.assertEqual(len(ops.open_questions(conn)), 2)
        self.assertEqual(len(ops.list_epics(conn)), 1, 'one machine-wide Disk space epic')
        self.assertIsNone(ops.get_setting(conn, 'dispatch_enabled'))
        self.assertEqual(ops.list_tasks(conn), [])

    def test_failed_outbox_write_does_not_consume_notice_episode(self):
        from drover import disk_cleanup
        self.approve_report()
        replace = Path.replace
        failed = []
        def fail_outbox(path, target):
            if Path(target).name == 'state.json' and 'outbox' in path.read_text() and not failed:
                failed.append(1)
                raise OSError('fixture atomic replacement refused')
            return replace(path, target)
        with patch.object(Path, 'replace', fail_outbox):
            failed_result = disk_cleanup.run(self.cfg, mode='triggered', apply=True, runtime=self.runtime())
        self.assertTrue(any(e.get('reason_code') == 'notice_persistence_failed' for e in failed_result['errors']))
        disk_cleanup.run(self.cfg, mode='triggered', apply=True, runtime=self.runtime())
        self.assertEqual(len(self.toasts), 1)
        self.assertEqual(len(self.durable_notices()), 1)

    def test_daily_healthy_budget_is_independent_of_exhaustion(self):
        from drover import config
        repo = self.new_repo('notice-budget')
        repo.commit('.drover/config.toml', config.dumps({'schema_version': 1}))
        (self.repos_dir / 'budget.toml').write_text(config.dumps(dict(name='budget', repo=str(repo.repo),
                   worktrees=str(repo.root / 'trees'), budget_gb=0.0001)))
        (repo.repo / 'payload').write_bytes(b'x' * 512 * 1024)
        self.free = 180
        self.approve_report()
        result = self.run_cleanup(mode='daily', apply=True)
        self.assertFalse(result['exhausted'])
        self.assertEqual(len([n for n in self.durable_notices() if n['kind'] == 'budget']), 1)

    def test_healthy_tracker_read_rearms_exhaustion_notice(self):
        from drover import disk, disk_cleanup
        self.approve_report()
        disk_cleanup.run(self.cfg, mode='triggered', apply=True, runtime=self.runtime())
        self.free = 180
        disk.tick(self.cfg, reader=lambda p: self.free, enqueue=lambda: None)
        self.free = 80
        disk_cleanup.run(self.cfg, mode='triggered', apply=True, runtime=self.runtime())
        self.assertEqual(len(self.toasts), 2)

    def test_windows_toast_timeout_preserves_durable_delivery(self):
        import subprocess
        from drover import disk_cleanup, placement
        self.approve_report()
        rt = self.runtime()
        with patch.object(placement, 'IS_WINDOWS', True) as _placement_patch_1, patch('drover.placement_exec.IS_WINDOWS', _placement_patch_1), patch('drover.placement_git.IS_WINDOWS', _placement_patch_1), patch('drover.placement_host.IS_WINDOWS', _placement_patch_1), patch('drover.placement_paths.IS_WINDOWS', _placement_patch_1), patch('drover.placement_process.IS_WINDOWS', _placement_patch_1), patch('drover.placement_proof.IS_WINDOWS', _placement_patch_1), patch('drover.placement_tree.IS_WINDOWS', _placement_patch_1), patch('drover.placement_worker.IS_WINDOWS', _placement_patch_1), \
                patch.object(subprocess, 'run', side_effect=subprocess.TimeoutExpired(['powershell.exe'], 15)):
            answer = disk_cleanup.run(self.cfg, apply=True, runtime=disk_cleanup.CleanupRuntime(
                rt.now, rt.read, rt.call, placement.disk_toast))
        self.assertTrue(answer['exhausted'])
        self.assertEqual(len(self.durable_notices()), 1)
        from drover import disk_state
        self.assertFalse(next(iter(disk_state.read(self.state)['outbox'].values()))['toasted'])

    def test_background_report_items_survive_and_replay_to_hub(self):
        from drover import disk_cleanup, disk_state
        from drover.ledger import connect, ops, disk_notices
        item = dict(path=str(self.root / 'open tree'), action='keep', reason_code='merged_live_task',
                    task_id='T-open', repo='fixture')
        def worker(side, request):
            answer = {'items': [], 'errors': [], 'reclaimed_bytes': 0}
            if side == 'wsl' and request['stage'] == 6:
                answer['items'].append(item)
            return answer
        result = self.run_cleanup(apply=False, handler=worker)
        saved = json.loads((self.state / 'disk' / 'last-report.json').read_text())
        self.assertEqual(saved['items'], result['items'])
        conn = connect(self.root / 'replay.db')
        self.addCleanup(conn.close)
        disk_notices.replay(conn, self.cfg, self.now)
        disk_notices.replay(conn, self.cfg, self.now)
        notices = [e['data']['notice'] for e in ops.events(conn, kind='disk.notice')]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]['items'][0]['task_id'], 'T-open')
        self.assertEqual(self.toasts, [])

    def test_general_inventory_is_exposed_through_hub_replay(self):
        from drover.ledger import connect, ops, disk_notices
        payload = self.file(self.report_root / 'retained')
        result = self.run_cleanup()
        conn = connect(self.root / 'inventory.db')
        self.addCleanup(conn.close)
        disk_notices.replay(conn, self.cfg, self.now)
        disk_notices.replay(conn, self.cfg, self.now)
        events = ops.events(conn, kind='disk.report')
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['data']['report']['items'], result['items'])
        self.assertTrue(any(i['path'] == str(payload) for i in events[0]['data']['report']['items']))
        self.assertEqual(ops.open_questions(conn), [])

    def test_snapshot_marks_every_nonterminal_task_as_owned(self):
        from drover.ledger import connect, ops, states, disk_notices
        conn = connect(self.root / 'ownership.db')
        self.addCleanup(conn.close)
        expected = {}
        for state in (states.READY, states.BLOCKED, states.FAILED, states.NEEDS_OWNER, states.CANCELLED):
            task = ops.create_task(conn, state, 'fixture')
            ops.approve(conn, task['id'])
            if state != states.READY:
                if state == states.FAILED:
                    ops.set_status(conn, task['id'], states.BLOCKED, reason='fixture', actor='owner')
                ops.set_status(conn, task['id'], state, reason='fixture', actor='owner')
            expected[task['id']] = state not in states.TERMINAL
        disk_notices.replay(conn, self.cfg, self.now)
        snapshot = json.loads((self.state / 'disk' / 'tasks.json').read_text())
        self.assertEqual({t['id']: t['live'] for t in snapshot['tasks']}, expected)
