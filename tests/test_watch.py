import json
import tempfile
import unittest
from pathlib import Path

from drover import config, watch
from drover.ledger import connect, disk_notices, ops, ops_panes, landing

NOW = 1_800_000_000.0
CFG = {'watch': dict(config.DEFAULTS['watch'])}
HOLD = {'verdict': 'hold', 'reasons': ['disk: C: has 2.1 GB free']}
GO = {'verdict': 'go', 'reasons': []}


class Watch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = connect(Path(self.tmp.name) / 'l.db')
        self.addCleanup(self.conn.close)

    def open_questions(self):
        return [q for q in self.conn.execute("SELECT * FROM questions WHERE status='open'")]

    def review(self, hours_ago):
        epic = ops.create_epic(self.conn, '', 'e')['id']
        task = ops.create_task(self.conn, 't', 'r', epic_id=epic)['id']
        at = ops.stamp(ops.clock().__class__.fromtimestamp(NOW - hours_ago * 3600, ops.clock().tzinfo))
        ops.event(self.conn, 'status', 'system', {'from': 'gated', 'to': 'reviewed'}, task_id=task, at=at)
        self.conn.execute("UPDATE tasks SET status='reviewed', updated=? WHERE id=?", (at, task))
        self.conn.commit()
        return task

    def batch(self, gates, red):
        landing.record_batch(self.conn, dict(batch=[], outcome='held', tip='x', gates=gates, gate_s=[1] * gates, red=red,
                                              bisect_gates=0, decisions=0, dropped=[], landed=[]))

    def clear(self, start, resources=GO):      # three intervals with the condition gone
        for i in range(3):
            ids = watch.tick(self.conn, CFG, resources, start + i * 700)
        return ids

    def test_flapping_condition_keeps_one_notice(self):
        for i in range(8):
            watch.tick(self.conn, CFG, HOLD if i % 2 == 0 else GO, NOW + i * 700)
        self.assertEqual(len(ops.events(self.conn, kind='watch.notice')), 1)
        self.assertEqual(len(self.open_questions()), 1)

    def test_old_red_gates_do_not_hold_the_notice(self):
        self.batch(6, 6)
        self.assertEqual([n['id'] for n in watch.velocity(self.conn, CFG['watch'], NOW)], ['velocity:red-rate'])
        for _ in range(20):
            self.batch(1, 0)
        self.assertEqual(watch.velocity(self.conn, CFG['watch'], NOW), [])

    def test_hold_verdict_opens_a_warden_notice(self):
        ids = watch.tick(self.conn, CFG, HOLD, NOW)
        self.assertEqual(ids, ['warden:hold'])
        self.assertEqual(len(self.open_questions()), 1)

    def test_clear_verdict_retracts_it(self):
        watch.tick(self.conn, CFG, HOLD, NOW)
        ids = self.clear(NOW + 700)
        self.assertEqual(ids, [])
        self.assertEqual(self.open_questions(), [])

    def test_reviewed_task_stalled_24h_opens_velocity_notice(self):
        old, fresh = self.review(25), self.review(2)
        found = watch.velocity(self.conn, CFG['watch'], NOW)
        self.assertEqual([n['id'] for n in found], [f'velocity:stalled:{old}'])
        self.assertNotIn(fresh, found[0]['id'])

    def test_red_rate_over_half_needs_six_gates(self):
        self.batch(5, 5)
        self.assertEqual(watch.velocity(self.conn, CFG['watch'], NOW), [])
        self.batch(1, 0)      # six gates, 5 red
        self.assertEqual([n['id'] for n in watch.velocity(self.conn, CFG['watch'], NOW)], ['velocity:red-rate'])

    def director_row(self, tokens, *, state='live', started_by='drover'):
        return ops_panes.record_agent(self.conn, pane_id='w1:p1', terminal_id='t1', kind='claude', seat='s', placement='windows',
                                      started_by=started_by, state=state, role_id='director:a', run_id=71, context_tokens=tokens)

    def context_notices(self):
        self.conn.execute('DELETE FROM settings WHERE key=?', (watch.STATE_KEY,))
        return [n for n in watch.tick(self.conn, CFG, None, NOW) if n.startswith('context:')]

    def test_a_director_over_300k_opens_the_context_notice(self):
        self.director_row(300001)
        self.assertEqual(self.context_notices(), ['context:71'])

    def test_a_director_at_or_under_300k_or_unread_opens_nothing(self):
        for tokens in (300000, None):
            self.conn.execute('DELETE FROM pane_agents')
            self.director_row(tokens)
            self.assertEqual(self.context_notices(), [])

    def test_only_a_live_drover_row_counts(self):
        self.director_row(400000, state='retired')
        self.assertEqual(self.context_notices(), [])
        self.conn.execute('DELETE FROM pane_agents')
        self.director_row(400000, started_by='owner')
        self.assertEqual(self.context_notices(), [])

    def test_run_over_300k_opens_context_notice(self):
        runs = [{'run_id': 'r1', 'task_id': 't1', 'context': 300001}, {'run_id': 'r2', 'task_id': 't2', 'context': 299999},
                {'run_id': 'r3', 'task_id': 't3', 'context': None}]
        self.assertEqual([n['id'] for n in watch.context(runs, CFG['watch'])], ['context:r1'])

    def test_a_notice_is_published_once(self):
        watch.tick(self.conn, CFG, HOLD, NOW)
        watch.tick(self.conn, CFG, HOLD, NOW + 700)
        self.assertEqual(len(self.open_questions()), 1)
        self.assertEqual(len(ops.events(self.conn, kind='watch.notice')), 1)

    def test_recurring_condition_opens_a_new_notice(self):
        watch.tick(self.conn, CFG, HOLD, NOW)
        self.clear(NOW + 700)
        self.assertEqual(watch.tick(self.conn, CFG, HOLD, NOW + 3000), ['warden:hold#1'])
        self.assertEqual(len(self.open_questions()), 1)
        self.assertEqual(len(ops.events(self.conn, kind='watch.notice')), 2)

    def test_owner_acknowledged_open_condition_is_not_republished(self):
        watch.tick(self.conn, CFG, HOLD, NOW)
        q = self.open_questions()[0]['id']
        ops.answer_question(self.conn, q, choice='Acknowledge', actor='owner')
        self.assertEqual(watch.tick(self.conn, CFG, HOLD, NOW + 700), ['warden:hold'])
        self.assertEqual(len(ops.events(self.conn, kind='watch.notice')), 1)
        self.assertEqual(self.open_questions(), [])

    def test_interval_is_honoured(self):
        watch.tick(self.conn, CFG, GO, NOW)
        self.assertEqual(watch.tick(self.conn, CFG, HOLD, NOW + 599), [])
        self.assertEqual(self.open_questions(), [])
        self.assertEqual(watch.tick(self.conn, CFG, HOLD, NOW + 600), ['warden:hold'])

    def test_failure_in_one_check_leaves_the_others(self):
        orig = watch.velocity
        watch.velocity = lambda *a: 1 / 0
        self.addCleanup(setattr, watch, 'velocity', orig)
        self.assertEqual(watch.tick(self.conn, CFG, HOLD, NOW), ['warden:hold'])

    def test_a_failed_check_keeps_its_open_notices(self):
        self.batch(6, 6)
        self.assertEqual(watch.tick(self.conn, CFG, GO, NOW), ['velocity:red-rate'])
        orig = watch.velocity
        watch.velocity = lambda *a: 1 / 0
        self.addCleanup(setattr, watch, 'velocity', orig)
        self.assertEqual(watch.tick(self.conn, CFG, GO, NOW + 700), ['velocity:red-rate'])

    def test_a_wsl_reading_without_wsl_makes_no_notice(self):
        self.assertEqual(watch.warden({'verdict': 'go', 'wsl': {'verdict': 'hold', 'reasons': ['wsl down']}}), [])

    def test_kinds_keep_separate_epics(self):
        disk_notices.publish(self.conn, dict(id='d1', kind='budget'))
        watch.tick(self.conn, CFG, HOLD, NOW)
        titles = {r['title'] for r in self.conn.execute('SELECT title FROM epics')}
        self.assertEqual(titles, {'Disk space', 'Watch'})


from unittest import mock
from tests.test_dispatcher import Base


class InDispatcher(Base):
    def test_a_hold_opens_a_notice_even_when_paused(self):
        ops.set_setting(self.conn, 'dispatch_enabled', 'false')
        self.verdict = {**self.verdict, 'verdict': 'hold', 'reasons': ['disk low']}
        self.d.tick()
        self.assertEqual(len(ops.events(self.conn, kind='watch.notice')), 1)

    def test_a_watch_failure_does_not_stop_dispatch(self):
        tid = self.task()
        with mock.patch('drover.watch.tick', side_effect=RuntimeError('boom')):
            out = self.d.tick()
        self.assertEqual(out['launched'], [tid])
