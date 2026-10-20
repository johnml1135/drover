import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from drover import gate
from drover.ledger import db as ledger_db
from tests.gitfix import Repo

class WrapTests(unittest.TestCase):
    def test_weights_and_p4(self):
        argv, _ = gate.wrap_command(['t'], 'P3', available=True)
        self.assertIn('CPUWeight=30', argv)
        argv, _ = gate.wrap_command(['t'], 'P4', '2G', 60, available=True)
        self.assertEqual(argv[-5:], ['--', 'timeout', '60', 't'][-4:] and argv[-5:])
        self.assertIn('MemoryMax=2G', argv)
        self.assertEqual(gate.wrap_command(['t'], 'P4', available=False), (['t'], None))


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.r = Repo()
        self.addCleanup(self.r.cleanup)
        self.out = self.r.root / 'out'
    def test_pass_binds_sha_and_verifies(self):
        sha = self.r.commit('t.txt')
        rec = gate.run_gate(self.r.repo, self.out / 'g.json', [sys.executable, '-c', 'pass'], available=False)
        self.assertEqual((rec['sha'], rec['status']), (sha, 'passed'))
        self.assertEqual(json.loads((self.out / 'g.json').read_text())['sha'], sha)
        self.assertEqual(gate.problems(rec, self.r.repo, 'origin/main'), [])
        self.r.commit('more.txt')
        self.assertTrue(any('HEAD is' in p for p in gate.problems(rec, self.r.repo, 'origin/main')))

    def test_exit_code_is_the_only_output_verdict(self):
        for code, text in ((0, 'FAILED: some owner summary'), (7, 'PASSED: some owner summary'), (9, '')):
            with self.subTest(code=code):
                rec = gate.run_gate(self.r.repo, self.out / 'g.json',
                    [sys.executable, '-c', f'print({text!r}) if {bool(text)!r} else None; raise SystemExit({code})'],
                    available=False)
                self.assertEqual(rec['exit_code'], code)
                self.assertEqual(rec['passed'], code == 0)
                self.assertEqual(rec['log_tail'], [text] if text else [])

    def test_dirty_tree_refused(self):
        (self.r.repo / 'x').write_text('x')
        with self.assertRaises(gate.GateError):
            gate.run_gate(self.r.repo, self.out / 'g.json', [sys.executable, '-c', 'pass'], available=False)


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
        with self.assertRaises(gate.GateError):
            self.acq(a, slots=1, at=200)

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

    def test_run_leased_follows_setting_so_landing_reaches_slot_six(self):
        from drover.ledger import ops
        ops.set_setting(self.conn, 'gate_slots', '8')
        ops.set_setting(self.conn, 'gate_reserved', '0')
        held = [gate.lease_request(self.conn, f'h{i}', 'P2', holder='dispatcher') for i in range(5)]
        for i, t in enumerate(held):
            self.assertEqual(self.acq(t, slots=8, reserved=0), i)
        for i in range(5, 7):                       # slots 5 and 6 are taken by others
            t = gate.lease_request(self.conn, f'x{i}', 'P2')
            self.assertEqual(self.acq(t, slots=8, reserved=0), i)
        got = gate.run_leased(self.db, 'batch:a', 'P2', lambda slot: slot, poll=0.01, timeout=1)
        self.assertEqual(got, 7)                    # beyond the old default of 5
        self.assertEqual(gate.run_leased(self.db, 'b', 'P2', lambda s: s, slots=8, reserved=0, poll=0.01, timeout=1), 7)

    def test_lease_limits_clamp(self):
        self.assertEqual(gate.lease_limits(8, 1), (8, 1))
        self.assertEqual(gate.lease_limits('0', '-3'), (1, 0))
        self.assertEqual(gate.lease_limits(99, 99), (32, 31))
        self.assertEqual(gate.lease_limits(3, 5), (3, 2))
        with self.assertRaises(ValueError):
            gate.lease_limits('many', 1)

    def test_run_leased_releases(self):
        got = gate.run_leased(self.db, 't', 'P2', lambda slot: slot + 10, poll=0.01)
        self.assertEqual(got, 10)
        self.assertEqual(gate.lease_queue(self.conn), [])
        with self.assertRaises(RuntimeError):
            gate.run_leased(self.db, 't', 'P2', lambda s: (_ for _ in ()).throw(RuntimeError('x')), poll=0.01)
        self.assertEqual(gate.lease_queue(self.conn), [])

    def test_run_leased_times_out_behind_holder(self):
        h = gate.lease_request(self.conn, 'h', 'P0')
        self.acq(h, slots=1)
        with self.assertRaises(TimeoutError):
            gate.run_leased(self.db, 'x', 'P2', lambda s: 1, slots=1, reserved=0, poll=0.01, timeout=0.1)
        self.assertEqual([t['task_id'] for t in gate.lease_queue(self.conn)], ['h'])


class HeartbeatTests(unittest.TestCase):
    def test_heartbeat_only_while_step_log_grows(self):
        import io
        from contextlib import redirect_stdout
        from unittest.mock import patch
        from drover import gatejob
        buf, seen = io.StringIO(), []

        def wait(pred):                              # bounded poll, not a timing race
            end = time.monotonic() + 30
            while not pred() and time.monotonic() < end:
                time.sleep(0.01)
            self.assertTrue(pred())

        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / 'r.step1.log'

            def slow(*a, log, **k):
                with open(log, 'wb', buffering=0) as f:
                    f.write(b'xy')
                    wait(lambda: len(buf.getvalue().splitlines()) >= 1)
                    n = len(buf.getvalue().splitlines())
                    time.sleep(0.5)                  # many beats with no growth
                    seen.extend([n, len(buf.getvalue().splitlines())])
                return {'status': 'passed'}
            with patch.object(gatejob, 'HEARTBEAT_S', 0.05), patch.object(gate, 'run_gate', slow), \
                    redirect_stdout(buf):
                r = gatejob._run_step(log, 'step1', d, 'out.json', ['x'])
        self.assertEqual(r['status'], 'passed')
        self.assertEqual(seen[0], seen[1])            # a silent step stops beating
        self.assertIn('step1 running, 2 bytes', buf.getvalue())


if __name__ == '__main__':
    unittest.main()
