"""A burst starts a few at a time, so a tick stays short, and a queue for gates costs nothing while no slot is free."""
import json
import time
import unittest
import unittest.mock
from unittest.mock import patch

from drover import config, launch
from drover.ledger import ci as slots
from drover.ledger import ops
from tests.bypass_fixture import YankBase
from tests.test_dispatcher import Base, DONE, res

MANY = {'verdict': 'go', 'slots': 24, 'reasons': []}


class LaunchLimitConfig(unittest.TestCase):
    def test_default_and_range(self):
        self.assertEqual(config.validate({})['capacity']['launches_per_tick'], 4)
        self.assertEqual(config.validate({'capacity': {'launches_per_tick': 64}})['capacity']['launches_per_tick'], 64)
        for bad in (0, 65, 'four'):
            with self.assertRaises(config.ConfigError):
                config.validate({'capacity': {'launches_per_tick': bad}})


class LaunchesPerTick(YankBase):
    def setUp(self):
        self.clock = [time.time()]
        super().setUp()
        self.verdict = MANY

    def restart(self):
        return self.make(launcher=self.fl, scan=self.runners, scan_every_s=0, now=lambda: self.clock[0])

    def runners(self, want_wsl):
        return {row[0]: 'windows' for row in self.conn.execute('SELECT id FROM runs WHERE ended IS NULL')}

    def beat(self):
        return json.loads(ops.get_setting(self.conn, 'dispatcher_heartbeat')).get('not_dispatched')

    def burst(self, count=10):
        return [self.task(size='S') for _ in range(count)]

    def test_a_burst_launches_four_a_tick(self):
        tasks = self.burst()
        self.assertEqual(self.d.tick()['launched'], tasks[:4])
        self.assertEqual(self.beat(), {'launch-limit': 6})
        self.assertEqual(self.d.tick()['launched'], tasks[4:8])
        self.assertEqual(self.d.tick()['launched'], tasks[8:])

    def test_the_cap_still_binds_below_the_limit(self):
        self.burst(3)
        self.verdict = {**MANY, 'slots': 2}
        self.assertEqual(len(self.d.tick()['launched']), 2)
        self.assertEqual(self.beat(), {'no-slots': 1})

    def test_config_sets_the_limit(self):
        self.burst(3)
        with patch.dict(config.get()['capacity'], launches_per_tick=1):
            self.assertEqual(len(self.d.tick()['launched']), 1)

    def test_a_failed_launch_spends_its_turn(self):
        self.burst()
        self.fl.launch = unittest.mock.Mock(side_effect=launch.LaunchError('no runner'))
        self.assertEqual(self.d.tick()['launched'], [])
        self.assertEqual(self.fl.launch.call_count, 4)

    def test_reviews_and_planners_share_the_limit(self):
        self.burst()
        asked = {}
        def reviews(policy, admitted, room):
            asked['reviews'] = room
            return ['R-1']
        def planners(policy, admitted, room):
            asked['planners'] = room
            return ['P-1']
        self.d._start_reviews, self.d._start_replans = reviews, planners
        self.assertEqual(len(self.d.tick()['launched']), 2)
        self.assertEqual(asked, {'reviews': 4, 'planners': 3})

    def test_a_yank_is_served_at_the_start_of_the_next_tick(self):
        steps, launch_seconds = [], 15
        real_launch, real_cancel = self.fl.launch, self.fl.cancel
        def slow_launch(spec):
            self.clock[0] += launch_seconds
            steps.append('launch')
            return real_launch(spec)
        def cancel(run_id, nonce=None):
            steps.append('cancel')
            return real_cancel(run_id, nonce)
        self.fl.launch, self.fl.cancel = slow_launch, cancel
        first, *_ = self.burst()
        started = self.clock[0]
        self.d.tick()
        self.assertLessEqual(self.clock[0] - started, 4 * launch_seconds)
        run = self.run_of(first)
        self.yank(first)
        steps.clear()
        self.assertEqual(self.d.tick()['yanked'], [run['id']])
        self.assertEqual(steps[0], 'cancel')                    # before this tick's launches
        self.finish(0, res('done', DONE))
        self.d.tick()
        self.assert_stopped(first, run['id'])


class GateQueue(Base):
    def setUp(self):
        super().setUp()
        self.reads, self.heads = [], []
        def resources(**kw):
            if kw.get('new_workers') == 0 and not kw.get('with_wsl_memory'):
                self.reads.append(kw)
            return self.verdict
        self.d = self.make(resources=resources)
        head = self.pipe.current_head
        def counted():
            self.heads.append(1)
            return head()
        self.pipe.current_head = counted
        ops.set_setting(self.conn, 'gate_slots', '1')
        ops.set_setting(self.conn, 'gate_reserved', '0')

    def occupy_the_slot(self):
        blocker = self.task(ready=False, scope_files=['fixture/blocker'])
        ticket = slots.lease_request(self.conn, blocker, 'P0', holder='landing')
        self.assertIsNotNone(slots.lease_acquire(self.conn, ticket, slots=1, reserved=0))
        return ticket

    def workers_done(self, count):
        tasks = [self.task(size='S') for _ in range(count)]
        with patch.dict(config.get()['capacity'], launches_per_tick=count):
            self.d.tick()
        for index, _ in enumerate(tasks):
            self.finish(index, res('done', DONE))
        return tasks

    def test_no_free_slot_costs_no_resource_read_and_no_git(self):
        ticket = self.occupy_the_slot()
        tasks = self.workers_done(3)
        self.reads.clear(), self.heads.clear()
        with patch.object(self.fl, 'wsl_up', wraps=self.fl.wsl_up) as wsl_up:
            self.assertEqual(self.d.tick()['gated'], [])
            self.d.tick()
            self.assertEqual((self.reads, self.heads, wsl_up.call_count), ([], [], 0))
        waiting = [row['task_id'] for row in slots.lease_queue(self.conn) if row['slot'] is None]
        self.assertEqual(sorted(waiting), sorted(tasks))        # asked every tick, so each keeps its place
        tickets = dict(self.d.ci_runners.tickets)
        self.d.tick()
        self.assertEqual(self.d.ci_runners.tickets, tickets)
        slots.lease_release(self.conn, ticket)
        self.assertEqual(len(self.d.tick()['gated']), 1)
        self.assertEqual(len(self.reads), 1)

    def test_a_slot_held_for_a_check_that_fails_goes_back(self):
        tasks = self.workers_done(1)
        self.verdict = {**self.verdict, 'side_admission': {'windows': {'verdict': 'hold'}, 'wsl': {'verdict': 'hold'}}}
        self.d.tick()
        self.assertEqual(slots.lease_queue(self.conn), [])
        self.assertEqual(self.d.ci_runners.tickets, {})
        self.assertEqual(self.status(tasks[0]), 'running')


class DispatchReport(Base):
    def test_ready_tasks_a_director_has_not_picked_are_reported(self):
        epic = ops.create_epic(self.conn, 'p', 'E')['id']
        ops.set_epic_mode(self.conn, epic, 'director')
        self.task(epic_id=epic)
        self.d.tick()
        beat = json.loads(ops.get_setting(self.conn, 'dispatcher_heartbeat'))
        self.assertEqual(beat['not_dispatched'], {'awaiting-director': 1})


if __name__ == '__main__':
    unittest.main()
