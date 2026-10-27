import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from drover.ledger import ci as gate
from drover.ledger import db as ledger_db, ops
from unittest import mock
import io
from tests.gitfix import Repo

class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'l.db'
        self.conn = ledger_db.connect(self.db)
        self.addCleanup(self.conn.close)

    def acq(self, t, **kw):
        return gate.lease_acquire(self.conn, t, **{'slots': 2, 'reserved': 0, **kw})

    def test_lower_rank_first_then_fifo(self):
        a = gate.lease_request(self.conn, 'a', 'P3')
        b = gate.lease_request(self.conn, 'b', 'P2')
        c = gate.lease_request(self.conn, 'c', 'P2')
        d = gate.lease_request(self.conn, 'd', 'P0')
        self.assertEqual(self.acq(a, slots=1), None)       # d, b, c outrank a
        self.assertEqual(self.acq(c, slots=1), None)
        self.assertEqual(self.acq(d, slots=1), 0)
        self.assertEqual(self.acq(b, slots=1), None)       # slot taken
        gate.lease_release(self.conn, d)
        self.assertEqual(self.acq(c, slots=1), None)       # b is earlier than c
        self.assertEqual(self.acq(b, slots=1), 0)
        gate.lease_release(self.conn, b)
        self.assertEqual(self.acq(c, slots=1), 0)
        gate.lease_release(self.conn, c)
        self.assertEqual(self.acq(a, slots=1), 0)

    def test_reserved_slot_only_for_top_ranks(self):
        low = gate.lease_request(self.conn, 'low', 'P2')
        top = gate.lease_request(self.conn, 'top', 'P1')
        self.assertEqual(self.acq(low, slots=2, reserved=1), 0)
        low2 = gate.lease_request(self.conn, 'low2', 'P2')
        self.assertEqual(self.acq(low2, slots=2, reserved=1), None)  # slot 1 is reserved
        self.assertEqual(self.acq(top, slots=2, reserved=1), 1)

    def test_stale_ticket_frees_slot(self):
        a = gate.lease_request(self.conn, 'a', 'P2', at=100)
        self.assertEqual(self.acq(a, slots=1, at=100), 0)
        b = gate.lease_request(self.conn, 'b', 'P2', at=150)
        self.assertEqual(self.acq(b, slots=1, ttl=60, at=150), None)
        self.assertEqual(self.acq(b, slots=1, ttl=60, at=200), 0)
        with self.assertRaises(gate.SlotExpired):
            self.acq(a, slots=1, at=200)

    def test_uncertain_execution_retains_pinned_ticket_past_expiry(self):
        a = gate.lease_request(self.conn, 'uncertain', 'P2', at=100)
        self.assertEqual(self.acq(a, slots=1, at=100), 0)
        gate.lease_pin(self.conn, a)
        b = gate.lease_request(self.conn, 'next', 'P2', at=1000)
        self.assertIsNone(self.acq(b, slots=1, at=1000))
        self.assertEqual([r['id'] for r in gate.lease_queue(self.conn)], [a, b])
        gate.lease_release(self.conn, a)
        self.assertEqual(self.acq(b, slots=1, at=1000), 0)

    def test_v14_preserves_existing_ticket_and_ordinary_expiry(self):
        from drover.ledger.schema import MIGRATIONS
        old = ledger_db.connect(Path(self.tmp.name) / 'v13.db', migrate_db=False)
        self.addCleanup(old.close)
        ledger_db.migrate(old, MIGRATIONS[:13])
        ticket = old.execute("INSERT INTO gate_tickets(task_id,rank,holder,requested,heartbeat) VALUES('ordinary',3,'gate',100,100)").lastrowid
        old.execute('UPDATE gate_tickets SET slot=0 WHERE id=?', (ticket,))
        issued = old.execute("INSERT INTO gate_tickets(task_id,rank,holder,requested,heartbeat) VALUES('group-ci',2,?,100,100)",
                             ('ci-' + 'a' * 32,)).lastrowid
        old.execute('UPDATE gate_tickets SET slot=1 WHERE id=?', (issued,))
        ledger_db.migrate(old)
        rows = {row['id']: row for row in gate.lease_queue(old)}
        self.assertEqual(rows[ticket]['pinned'], 0)
        self.assertEqual(rows[ticket]['slot'], 0)
        self.assertEqual(rows[issued]['pinned'], 1)
        ledger_db.migrate(old)
        new = gate.lease_request(old, 'next', 'P2', at=1000)
        self.assertEqual(gate.lease_acquire(old, new, slots=2, reserved=0, at=1000), 0)
        self.assertEqual({row['id'] for row in gate.lease_queue(old)}, {issued, new})

    def test_unacquired_or_expired_ticket_cannot_be_pinned(self):
        ticket = gate.lease_request(self.conn, 'waiting', 'P2')
        with self.assertRaises(gate.SlotExpired):
            gate.lease_pin(self.conn, ticket)
        gate.lease_release(self.conn, ticket)
        with self.assertRaises(gate.SlotExpired):
            gate.lease_pin(self.conn, ticket)

    def test_aging_lets_a_starved_ticket_overtake_newer_better_ranks(self):
        old = gate.lease_request(self.conn, 'old', 'P4', at=0)        # rank 5, waiting 3h -> effective 2
        new = gate.lease_request(self.conn, 'new', 'P3', at=10800)    # rank 4
        at = 10800
        self.assertEqual(self.acq(new, slots=1, at=at, ttl=1e9), None)
        self.assertEqual(self.acq(old, slots=1, at=at, ttl=1e9), 0)

    def test_aging_never_grants_the_reserved_slot(self):
        old = gate.lease_request(self.conn, 'old', 'P4', at=0)
        old2 = gate.lease_request(self.conn, 'old2', 'P4', at=0)
        at = 36000
        self.assertEqual(self.acq(old, slots=2, reserved=1, at=at, ttl=1e9), 0)
        self.assertEqual(self.acq(old2, slots=2, reserved=1, at=at, ttl=1e9), None)   # slot 1 is reserved for P0/P1

    def test_eight_slots_let_eight_through(self):
        ts = [gate.lease_request(self.conn, f't{i}', 'P2') for i in range(9)]
        got = [self.acq(t, slots=8, reserved=0) for t in ts]
        self.assertEqual(sorted(got[:8]), list(range(8)))
        self.assertIsNone(got[8])
        gate.lease_release(self.conn, ts[0])
        self.assertEqual(self.acq(ts[8], slots=8, reserved=0), got[0])

    def test_reserved_rule_unchanged_at_eight_slots(self):
        low = [gate.lease_request(self.conn, f'l{i}', 'P3') for i in range(8)]
        got = [self.acq(t, slots=8, reserved=1) for t in low]
        self.assertEqual(sorted(g for g in got if g is not None), list(range(7)))   # slot 7 is the reserve
        top = gate.lease_request(self.conn, 'top', 'P1')
        self.assertEqual(self.acq(top, slots=8, reserved=1), 7)

    def test_lease_limits_clamp(self):
        self.assertEqual(gate.lease_limits(8, 1), (8, 1))
        self.assertEqual(gate.lease_limits('0', '-3'), (1, 0))
        self.assertEqual(gate.lease_limits(99, 99), (32, 31))
        self.assertEqual(gate.lease_limits(3, 5), (3, 2))
        with self.assertRaises(ValueError):
            gate.lease_limits('many', 1)


class RunnerSettings(LeaseTests):
    def test_defaults_then_settings_then_clamp(self):
        self.assertEqual(gate.configured_limits(lambda k, d: ops.get_setting(self.conn, k, d)), (5, 1))
        ops.set_setting(self.conn, "gate_slots", "8")
        ops.set_setting(self.conn, "gate_reserved", "2")
        self.assertEqual(gate.configured_limits(lambda k, d: ops.get_setting(self.conn, k, d)), (8, 2))
        ops.set_setting(self.conn, "gate_slots", "100")
        ops.set_setting(self.conn, "gate_reserved", "100")
        self.assertEqual(gate.configured_limits(lambda k, d: ops.get_setting(self.conn, k, d)), (32, 31))

    def test_invalid_falls_back_and_logs_once(self):
        ops.set_setting(self.conn, "gate_slots", "lots")
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.assertEqual(gate.configured_limits(lambda k, d: ops.get_setting(self.conn, k, d)), (5, 1))
            self.assertEqual(gate.configured_limits(lambda k, d: ops.get_setting(self.conn, k, d)), (5, 1))
        self.assertEqual(err.getvalue().count("gate_slots"), 1)

    def test_lease_honours_slots_setting(self):
        ops.set_setting(self.conn, "gate_slots", "8")
        ops.set_setting(self.conn, "gate_windows_slots", "8")
        ops.set_setting(self.conn, "gate_reserved", "0")
        tasks = [{"id": f"t{i}", "priority": "P2"} for i in range(9)]
        runners = gate.Runners(self.conn)
        got = [runners.reserve(t) for t in tasks]
        self.assertEqual(sorted(got[:8]), list(range(8)))
        self.assertIsNone(got[8])

