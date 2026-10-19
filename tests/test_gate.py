import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from drover import gate
from drover.ledger import db as ledger_db
from tests.gitfix import Repo, sh

MAP = {'always': [], 'fallback': ['P.All'], 'patterns': {'src/a/*': ['P.A'], 'src/b/*': ['P.B'], 'docs/*': []}}

DOTNET = """\
  Passed Acme.A.Tests.Ok.Fine [1 ms]
  Failed Acme.A.Tests.FooTests.Does_thing [35 ms]
  Error Message:
   Assert.Equal() Failure: Expected 1, Actual 2
  Stack Trace:
     at Acme.A.Tests.FooTests.Does_thing() in /w/repo/tests/A/FooTests.cs:line 42
  Failed Acme.B.Tests.BarTests.Other(x: 3) [2 ms]
  Error Message:
   boom
Failed!  - Failed:     2, Passed:     1, Skipped:     0, Total:     3
/w/repo/src/A/Thing.cs(12,5): error CS1002: ; expected [/w/repo/src/A/A.csproj]
"""
PYTEST = """\
FAILED tests/test_x.py::TestA::test_b - AssertionError: 1 != 2
ERROR tests/test_y.py::test_c
2 failed, 3 passed in 0.1s
"""
UNITTEST = """\
FAIL: test_one (tests.test_m.MyTests.test_one)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/usr/lib/python3.12/unittest/case.py", line 58, in testPartExecutor
  File "/w/repo/tests/test_m.py", line 9, in test_one
    self.assertEqual(1, 2)
AssertionError: 1 != 2

----------------------------------------------------------------------
Ran 3 tests in 0.0s
"""


class ParseTests(unittest.TestCase):
    def test_dotnet_with_diff(self):
        f = gate.parse_failures(DOTNET, ['tests/A/FooTests.cs', 'src/A/Thing.cs'])
        by = {x['test']: x for x in f}
        self.assertEqual(by['Acme.A.Tests.FooTests.Does_thing']['file'], 'tests/A/FooTests.cs')
        self.assertTrue(by['Acme.A.Tests.FooTests.Does_thing']['in_diff'])
        self.assertIn('Expected 1', by['Acme.A.Tests.FooTests.Does_thing']['error'])
        other = by['Acme.B.Tests.BarTests.Other']
        self.assertFalse(other['in_diff'])
        self.assertEqual(other['error'], 'boom')
        self.assertTrue(by['build']['in_diff'])
        self.assertIn('CS1002', by['build']['error'])

    def test_class_name_guesses_file(self):
        f = gate.parse_failures('  Failed N.BarTests.M [1 ms]\n', ['t/BarTests.cs'])
        self.assertEqual((f[0]['file'], f[0]['in_diff']), ('t/BarTests.cs', True))

    def test_pytest_summary(self):
        f = gate.parse_failures(PYTEST, ['tests/test_x.py'])
        self.assertEqual([x['file'] for x in f], ['tests/test_x.py', 'tests/test_y.py'])
        self.assertEqual([x['in_diff'] for x in f], [True, False])
        self.assertEqual(f[0]['error'], 'AssertionError: 1 != 2')

    def test_unittest(self):
        f = gate.parse_failures(UNITTEST, ['tests/test_m.py'])
        self.assertEqual(len(f), 1)
        self.assertTrue(f[0]['in_diff'])
        self.assertEqual(f[0]['error'], 'AssertionError: 1 != 2')

    def test_unparsed(self):
        f = gate.parse_failures('segfault\n')
        self.assertTrue(gate.unparsed(f, 1))
        self.assertFalse(gate.unparsed(f, 0))


class ScopeTests(unittest.TestCase):
    def test_projects_fallback_and_empty_pattern(self):
        self.assertEqual(gate.compute_scope(['src/a/x.cs'], MAP)['projects'], ['P.A'])
        r = gate.compute_scope(['src/a/x', 'weird'], MAP)
        self.assertEqual(r['projects'], ['P.A', 'P.All'])
        self.assertTrue(r['fallback_used'])
        r = gate.compute_scope(['docs/x'], MAP)
        self.assertEqual((r['projects'], r['fallback_used']), ([], False))

    def test_default_map_loads(self):
        self.assertEqual(gate.compute_scope(['src/anything.py'], gate.load_map())['projects'], [])
        self.assertEqual(gate.project_of('Any.Tests.Example', []), '')
        self.assertEqual(gate.project_of('Any.Tests.Example', [], r'^(Any\.Tests)'), 'Any.Tests')

    def test_scope_for_diff(self):
        r = Repo()
        self.addCleanup(r.cleanup)
        r.commit('src/b/y.cs', 'y')
        m = r.root / 'm.json'
        m.write_text(json.dumps(MAP))
        self.assertEqual(gate.scope_for(r.repo, 'origin/main', m)['projects'], ['P.B'])


class PathTests(unittest.TestCase):
    def test_dot_directories_and_single_dot_slash(self):
        self.assertTrue(gate.path_matches('.github/x', '.github/x'))
        self.assertTrue(gate.path_matches('/w/repo/.github/x', '.github/x'))
        self.assertTrue(gate.path_matches('a/b', './a/b'))
        self.assertFalse(gate.path_matches('github/x', '.github/x'))
        self.assertFalse(gate.path_matches('.github/x', '..github/x'))

    def test_component_aware(self):
        self.assertTrue(gate.path_matches('lib/util.py', 'lib/util.py'))
        self.assertFalse(gate.path_matches('lib/util.py', 'util.py'))        # relative: must be equal
        self.assertTrue(gate.path_matches('/w/repo/lib/util.py', 'lib/util.py'))
        self.assertFalse(gate.path_matches('/w/repo/xlib/util.py', 'lib/util.py'))
        self.assertTrue(gate.path_matches('C:\\w\\repo\\lib\\util.py', 'lib/util.py'))

    def test_longest_match_and_unique_stem(self):
        f = gate.parse_failures('FAILED /w/r/lib/util.py::t - x\n', ['util.py', 'lib/util.py'])
        self.assertEqual(f[0]['file'], 'lib/util.py')
        f = gate.parse_failures('  Failed N.FooTests.M [1 ms]\n', ['a/FooTests.cs', 'b/FooTests.cs'])
        self.assertEqual((f[0]['file'], f[0]['in_diff']), (None, False))


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
        script = self.r.root / 'gate.py'
        script.write_text('import sys\nfor c in sys.argv[2:]:\n    print(f"  Failed {c}.Method [1 ms]")\n'
                          'print(f"Failed!  - Failed:     {len(sys.argv)-2}, Passed:     5, Skipped:     0, Total:     9")\n'
                          'sys.exit(1)\n')
        rerun = self.r.root / 'rerun.py'
        rerun.write_text('import sys, pathlib\nn = sys.argv[1]\n'
                         'if (pathlib.Path(sys.argv[2]) / n).exists():\n'
                         '    print(f"  Passed {n}.Method [1 ms]")\n'
                         '    print("Passed!  - Failed:     0, Passed:     3, Skipped:     0, Total:     3"); sys.exit(0)\n'
                         'print("Failed!  - Failed:     1, Passed:     0, Skipped:     0, Total:     1"); sys.exit(1)\n')
        self.script, self.rerun, self.flags = script, rerun, self.r.root / 'flags'
        self.flags.mkdir()

    def gate(self, classes, passing=(), n=2):
        for c in passing:
            (self.flags / c).write_text('ok')
        return gate.run_gate(self.r.repo, self.out / 'g.json', [sys.executable, self.script, '-All', *classes],
                             retry_failed=n, retry_command=f'{sys.executable} {self.rerun} {{class}} {self.flags}',
                             available=False)

    def test_pass_binds_sha_and_verifies(self):
        sha = self.r.commit('t.txt')
        rec = gate.run_gate(self.r.repo, self.out / 'g.json', [sys.executable, '-c', 'pass'], available=False)
        self.assertEqual((rec['sha'], rec['status']), (sha, 'passed'))
        self.assertEqual(json.loads((self.out / 'g.json').read_text())['sha'], sha)
        self.assertEqual(gate.problems(rec, self.r.repo, 'origin/main'), [])
        self.r.commit('more.txt')
        self.assertTrue(any('HEAD is' in p for p in gate.problems(rec, self.r.repo, 'origin/main')))

    def test_dirty_tree_refused(self):
        (self.r.repo / 'x').write_text('x')
        with self.assertRaises(gate.GateError):
            gate.run_gate(self.r.repo, self.out / 'g.json', [sys.executable, '-c', 'pass'], available=False)

    def test_flake_recorded_and_still_failed_class(self):
        rec = self.gate(['N.A', 'N.B'], passing=['N.A', 'N.B'])
        self.assertEqual((rec['status'], rec['flakes']), ('passed_with_flakes', ['N.A', 'N.B']))
        self.assertEqual(gate.problems(rec, self.r.repo, 'origin/main'), [])
        rec = self.gate(['N.A', 'N.C'], passing=['N.A'])
        self.assertEqual(rec['status'], 'failed')
        self.assertEqual(len(rec['retries']['N.C']), 2)

    def test_unsafe_class_never_run(self):
        rec = gate.run_gate(self.r.repo, self.out / 'g.json', [sys.executable, self.script, 'N.A;rm'],
                            retry_failed=1, retry_command=f'{sys.executable} {self.rerun} {{class}} {self.flags}',
                            available=False)
        self.assertEqual(rec['status'], 'failed')

    def test_repo_toml_project_pattern_controls_retry_project(self):
        import io
        import os
        import tomllib
        from unittest.mock import patch
        from drover import config
        private = self.r.root / 'repos'
        private.mkdir()
        (private / 'example.toml').write_text('repo="' + self.r.repo.as_posix() + '"\n'
                                             '[gate]\nproject_pattern="^(N[.]A)"\n')
        (self.flags / 'N.A').write_text('ok')
        command = [sys.executable, str(self.script), '-All', 'N.A']
        with patch.dict(os.environ, {'DROVER_REPO_CONFIGS_DIR': str(private)}), patch('sys.stdout', new_callable=io.StringIO):
            config.reset()
            self.addCleanup(config.reset)
            code = gate.main(['run', '--repo-name', 'example', '--repo', str(self.r.repo),
                              '--out', str(self.out / 'from-config.json'), '--retry-failed', '1',
                              '--retry-command', f'{sys.executable} {self.rerun} {{class}} {self.flags} {{project}}',
                              '--json', '--', *command])
        self.assertEqual(code, 0)
        receipt = json.loads((self.out / 'from-config.json').read_text())
        self.assertEqual(receipt['retries']['N.A'][0]['project'], 'N.A')

    def test_retry_needs_class_template(self):
        with self.assertRaises(gate.GateError):
            gate.run_gate(self.r.repo, self.out / 'g.json', [sys.executable, '-c', 'pass'], retry_failed=1, retry_command='echo hi')

    def test_flake_in_changed_file_refused(self):
        rec = {'version': 1, 'status': 'passed_with_flakes', 'flakes': ['N.FooTests']}
        self.r.commit('FooTests.cs', 'public class FooTests {}\n')
        sha = self.r.git('rev-parse', 'HEAD')
        self.assertTrue(gate.flake_problems(rec, self.r.repo, 'origin/main', sha))

    def test_chain_refuses_non_full_parent(self):
        self.r.commit('a.txt')
        parent = {'version': 1, 'sha': self.r.git('rev-parse', 'HEAD'), 'passed': True, 'tree_unchanged': True,
                  'command': ['t', '-All', '-Project', 'X'], 'scope': None}
        self.r.commit('src/a/q.cs')
        scoped = {'sha': self.r.git('rev-parse', 'HEAD'), 'command': ['t', 'P.A']}
        m = self.r.root / 'm.json'
        m.write_text(json.dumps(MAP))
        found = gate.chain_problems(scoped, parent, self.r.repo, 'origin/main', m)
        self.assertTrue(any('full gate' in f for f in found))
        parent['command'] = ['t', '-All']
        self.assertEqual(gate.chain_problems(scoped, parent, self.r.repo, 'origin/main', m), [])
        scoped['command'] = ['t']
        self.assertTrue(gate.chain_problems(scoped, parent, self.r.repo, 'origin/main', m))


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


if __name__ == '__main__':
    unittest.main()
