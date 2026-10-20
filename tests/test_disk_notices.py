"""Cleanup notices survive transport failure and enter the existing Inbox once."""
import json
from pathlib import Path

from tests.disk_fixture import DiskCase


class Notices(DiskCase):
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
        from tests.gitfix import Repo
        repo = Repo()
        self.addCleanup(repo.cleanup)
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
        repo.git('worktree', 'add', '-q', '-b', 'budget-topic', wt, 'HEAD')
        repo.git('worktree', 'lock', '--reason', 'wsl', wt)
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
