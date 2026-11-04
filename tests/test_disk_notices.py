"""Cleanup notices survive transport failure and enter the existing Inbox once."""
import json
from pathlib import Path
from unittest.mock import patch

from drover import durable
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
        replace = durable.os.replace
        failed = []
        def fail_outbox(path, target):
            if Path(target).name == 'state.json' and 'outbox' in Path(path).read_text() and not failed:
                failed.append(1)
                raise OSError('fixture atomic replacement refused')
            return replace(path, target)
        with patch.object(durable.os, 'replace', fail_outbox):
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
        from drover import disk_cleanup, placement, process
        self.approve_report()
        rt = self.runtime()
        with patch.object(placement, 'IS_WINDOWS', True) as _placement_patch_1, patch('drover.placement_exec.IS_WINDOWS', _placement_patch_1), patch('drover.placement_git.IS_WINDOWS', _placement_patch_1), patch('drover.placement_host.IS_WINDOWS', _placement_patch_1), patch('drover.placement_paths.IS_WINDOWS', _placement_patch_1), patch('drover.placement_process.IS_WINDOWS', _placement_patch_1), patch('drover.placement_proof.IS_WINDOWS', _placement_patch_1), patch('drover.placement_tree.IS_WINDOWS', _placement_patch_1), patch('drover.placement_worker.IS_WINDOWS', _placement_patch_1), \
                patch.object(process.subprocess, 'run', side_effect=subprocess.TimeoutExpired(['powershell.exe'], 15)):
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
        from drover import disk_state
        from drover.ledger import connect, ops, disk_notices
        payload = self.file(self.report_root / 'retained')
        result = self.run_cleanup()
        # A build-roots run's report is journalled beside the full one, which it never replaces.
        disk_state.save_file(self.state, 'last-build-roots.json', dict(mode='build-roots', applied=True, errors=[],
                                                                       items=[dict(path='u', action='removed')]))
        conn = connect(self.root / 'inventory.db')
        self.addCleanup(conn.close)
        disk_notices.replay(conn, self.cfg, self.now)
        disk_notices.replay(conn, self.cfg, self.now)
        events = ops.events(conn, kind='disk.report')
        self.assertEqual(sorted(e['data']['report']['mode'] for e in events), sorted([result['mode'], 'build-roots']))
        [full] = [e for e in events if e['data']['report']['mode'] == result['mode']]
        self.assertEqual(full['data']['report']['items'], result['items'])
        self.assertTrue(any(i['path'] == str(payload) for i in full['data']['report']['items']))
        self.assertEqual(ops.open_questions(conn), [])

    def test_a_build_roots_report_is_recorded_only_when_it_removed_or_failed_and_each_file_is_read_once(self):
        import os
        from drover import disk_state
        from drover.ledger import connect, disk_notices
        conn = connect(self.root / 'builds.db')
        self.addCleanup(conn.close)
        def recorded():
            return [json.loads(row[0])['report'] for row in
                    conn.execute("SELECT data FROM task_events WHERE kind='disk.report' ORDER BY id")]
        def report(at, **rows):
            path = disk_state.save_file(self.state, 'last-build-roots.json', dict(
                mode='build-roots', applied=True, items=rows.get('items', []), errors=rows.get('errors', []), at=at))
            os.utime(self.state / 'disk' / 'last-build-roots.json', (at, at))
        report(1_000, items=[dict(path='u', action='keep', reason_code='build_root_recent')])
        disk_notices.replay(conn, self.cfg, self.now)
        self.assertEqual(recorded(), [], 'nothing removed and nothing failed: no event')
        report(2_000, items=[dict(path='u', action='removed', reason_code='build_root_idle')])
        disk_notices.replay(conn, self.cfg, self.now)
        report(3_000, errors=[dict(path='r', error='denied')])
        disk_notices.replay(conn, self.cfg, self.now)
        self.assertEqual([r['at'] for r in recorded()], [2_000, 3_000])
        with patch('drover.ledger.ops.events', side_effect=AssertionError('re-read')),                 patch('drover.ledger.ops.event', side_effect=AssertionError('re-recorded')):
            disk_notices.replay(conn, self.cfg, self.now)      # unchanged files: neither parsed nor looked up again
        self.assertEqual(len(recorded()), 2)

    def test_snapshot_marks_every_nonterminal_task_as_owned(self):
        from drover.ledger import connect, ops, states, disk_notices
        conn = connect(self.root / 'ownership.db')
        self.addCleanup(conn.close)
        expected = {}
        for state in (states.READY, states.BLOCKED, states.FAILED, states.NEEDS_OWNER, states.CANCELLED):
            task = ops.create_task(conn, state, 'fixture', scope_files=['fixture'])
            ops.approve(conn, task['id'])
            if state != states.READY:
                if state == states.FAILED:
                    ops.set_status(conn, task['id'], states.BLOCKED, reason='fixture', actor='owner')
                ops.set_status(conn, task['id'], state, reason='fixture', actor='owner')
            expected[task['id']] = state not in states.TERMINAL
        disk_notices.replay(conn, self.cfg, self.now)
        snapshot = json.loads((self.state / 'disk' / 'tasks.json').read_text())
        self.assertEqual({t['id']: t['live'] for t in snapshot['tasks']}, expected)
        # disk_notices :50 catches OSError: a lease renewal whose git times out (GitFailed) skips, and the snapshot
        # is still written.
        import subprocess
        from drover import ownership, placement, placement_git
        from tests.fakeci import git_answers
        live = next(iter(expected))
        conn.execute('UPDATE tasks SET worktree=? WHERE id=?', (str(self.root / 'wt'), live))
        def timed_out(repo, *args, timeout):
            raise subprocess.TimeoutExpired(['git', *args], timeout)
        with git_answers(timed_out), patch.object(ownership, 'owned_by', return_value=True), \
                patch.object(placement, 'worktree_metadata', placement_git.worktree_metadata):
            disk_notices.replay(conn, self.cfg, self.now)
        snapshot = json.loads((self.state / 'disk' / 'tasks.json').read_text())
        self.assertEqual({t['id']: t['live'] for t in snapshot['tasks']}, expected)
        self.assertIn(str(self.root / 'wt'), snapshot['active_worktrees'])


def usage(*, partial, bytes_used=1):
    """A stage 6 answer holding one `budget_usage` row of `bytes_used`, far under a 1 GiB budget."""
    row = dict(path='/r/budget', action='report', reason_code='budget_usage', repo='budget', identity='i', bytes=bytes_used,
               owner=None, files=1, age_s=0)
    return dict(items=[row], errors=[], reclaimed_bytes=0, peak_intermediate_bytes=0, budget=None, complete=True,
                partial=partial)


class PartialUsage(MemoryWorktreeCase):
    """A cut-off stage 6 lists only part of a repo's usage, so its short rows must neither close nor open a budget
    episode. The mark is in `result`, set at both call sites, and read again by `retry_notices`."""
    def setUp(self):
        super().setUp()
        from drover import config, disk_state
        repo = self.new_repo('notice-budget')
        repo.commit('.drover/config.toml', config.dumps({'schema_version': 1}))
        (self.repos_dir / 'budget.toml').write_text(config.dumps(dict(
            name='budget', repo=str(repo.repo), worktrees=str(repo.root / 'trees'), budget_gb=1)))
        self.approve_report()
        disk_state.update(self.state, lambda state: state.setdefault('budget_episodes', {}).update(budget='budget:open'))

    def episode(self):
        from drover import disk_state
        return disk_state.read(self.state).get('budget_episodes', {}).get('budget')

    def applied(self, answer, free):
        """An applied run whose stage 6 answers `answer`, or `answer(side)`; every other stage says nothing."""
        def handler(side, request):
            if request['stage'] != 6:
                return dict(items=[], errors=[], reclaimed_bytes=0)
            return json.loads(json.dumps(answer(side) if callable(answer) else answer))
        self.free = free
        return self.run_cleanup(apply=True, handler=handler)

    def test_partial_stage_6_leaves_a_budget_episode_alone_at_both_call_sites(self):
        # free 80 runs stage 6 through _serve_stage; free 180 skips the stages, so stage 6 is the direct call only.
        for free in (80., 180.):
            result = self.applied(usage(partial=True), free)
            self.assertTrue(result['budget_usage_partial'], free)
            self.assertEqual(self.episode(), 'budget:open', free)
        self.applied(usage(partial=False), 180.)
        self.assertIsNone(self.episode(), 'the same rows, whole, close the episode')

    def test_a_partial_stage_6_opens_no_episode_either(self):
        from drover import disk_state
        disk_state.update(self.state, lambda state: state['budget_episodes'].clear())
        self.applied(usage(partial=True, bytes_used=2 * 1024 ** 3), 180.)
        self.assertIsNone(self.episode())

    def test_the_mark_survives_retry_notices(self):
        from drover import disk_cleanup, disk_cleanup_notices
        with patch.object(disk_cleanup, '_notices', side_effect=BlockingIOError('state busy')):
            self.applied(usage(partial=True), 180.)
        pending = list((self.state / 'disk').glob('pending-notices-*.json'))
        self.assertEqual(len(pending), 1)
        self.assertTrue(json.loads(pending[0].read_text())['result']['budget_usage_partial'])
        disk_cleanup_notices.retry_notices(self.cfg)
        self.assertEqual(list((self.state / 'disk').glob('pending-notices-*.json')), [])
        self.assertEqual(self.episode(), 'budget:open')

    def test_missing_usage_is_unknown_not_zero(self):                       # O7
        # One side's direct stage 6 fails: the other side's rows are not the repo's whole usage.
        def one_side(side):
            if side == 'wsl':
                raise OSError('worker unavailable')
            return usage(partial=False)
        result = self.applied(one_side, 180.)
        self.assertIn('budget_unavailable', [e.get('reason_code') for e in result['errors']])
        self.assertEqual(self.episode(), 'budget:open')
