import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from drover import allowance as al

NOW = 1_800_000_000.0
HERE = Path(__file__).resolve().parent.parent
SCRIPT = HERE / 'scripts' / 'statusline-tee.sh'


def snap(state, sid, five=20, seven=40, ts=NOW - 60, five_reset=NOW + 3600, seven_reset=NOW + 3 * 86400):
    d = Path(state) / 'allowance'
    d.mkdir(parents=True, exist_ok=True)
    rl = {'five_hour': dict(used_percentage=five, resets_at=five_reset),
          'seven_day': dict(used_percentage=seven, resets_at=seven_reset)}
    (d / f'claude-{sid}.json').write_text(json.dumps(dict(rate_limits=rl, ts=ts, session_id=sid, model={'id': 'opus'})))


def rollout(home, name, lines, day='2026/10/14'):
    d = Path(home) / '.codex' / 'sessions' / day
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text('\n'.join(lines) + '\n')
    return d / name


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def tc(used, mins=10080, reset=NOW + 86400, ts=None):
    ts = ts or iso(NOW - 60)
    return json.dumps(dict(timestamp=ts, type='event_msg', payload=dict(
        type='token_count', rate_limits=dict(primary=dict(used_percent=used, window_minutes=mins, resets_at=reset), secondary=None))))


class Base(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(); self.addCleanup(t.cleanup)
        self.state = str(Path(t.name) / 'state'); self.home = str(Path(t.name) / 'home')
        Path(self.home).mkdir()


class ReadClaude(Base):
    def test_newest_snapshot_wins(self):
        snap(self.state, 'a', seven=10, ts=NOW - 600); snap(self.state, 'b', seven=55, ts=NOW - 30)
        r = al.read_claude(self.state, NOW)
        self.assertEqual((r['source'], r['windows']['seven_day']['used_pct']), ('live', 55))

    def test_stale_and_unknown(self):
        self.assertEqual(al.read_claude(self.state, NOW)['source'], 'unknown')
        snap(self.state, 'a', ts=NOW - 3601)
        self.assertEqual(al.read_claude(self.state, NOW)['source'], 'stale')

    def test_malformed_ignored(self):
        snap(self.state, 'good', seven=30)
        d = Path(self.state) / 'allowance'
        (d / 'claude-bad.json').write_text('{nope')
        (d / 'claude-shape.json').write_text(json.dumps(dict(ts=NOW, rate_limits=dict(five_hour=dict(used_percentage='x', resets_at=1)))))
        (d / 'claude-range.json').write_text(json.dumps(dict(ts=NOW, rate_limits=dict(seven_day=dict(used_percentage=140, resets_at=NOW + 9)))))
        r = al.read_claude(self.state, NOW)
        self.assertEqual(r['windows']['seven_day']['used_pct'], 30)

    def test_expired_window_dropped(self):
        snap(self.state, 'a', five_reset=NOW - 1)
        self.assertNotIn('five_hour', al.read_claude(self.state, NOW)['windows'])


class ReadCodex(Base):
    def test_tail_event_and_window_mapping(self):
        rollout(self.home, 'rollout-a.jsonl', [tc(5), 'garbage', tc(12, 300, NOW + 900), tc(7)])
        r = al.read_codex(self.home, NOW)
        self.assertEqual(r['windows']['seven_day']['used_pct'], 7)
        self.assertEqual(r['source'], 'live')
        self.assertEqual(al.read_codex(self.home, NOW + 3000)['source'], 'stale')

    def test_large_file_reads_only_tail(self):
        pad = [json.dumps(dict(type='x', pad='y' * 1000))] * 600  # ~600KB > TAIL_BYTES
        rollout(self.home, 'rollout-big.jsonl', [tc(99)] + pad + [tc(21)])
        with mock.patch('builtins.open', wraps=open) as o:
            r = al.read_codex(self.home, NOW)
        self.assertEqual(r['windows']['seven_day']['used_pct'], 21)
        self.assertTrue(o.called)

    def test_old_event_beyond_tail_not_found(self):
        pad = [json.dumps(dict(type='x', pad='y' * 1000))] * 600
        rollout(self.home, 'rollout-big.jsonl', [tc(99)] + pad)
        self.assertEqual(al.read_codex(self.home, NOW)['source'], 'unknown')

    def test_file_vanishing_is_skipped(self):
        f = rollout(self.home, 'rollout-a.jsonl', [json.dumps({k: v for k, v in json.loads(tc(5)).items() if k != 'timestamp'})])
        real = Path.stat
        def boom(self_, *a, **k):
            if self_.name == f.name: raise FileNotFoundError
            return real(self_, *a, **k)
        with mock.patch.object(Path, 'stat', boom):
            self.assertEqual(al.read_codex(self.home, NOW)['source'], 'unknown')

    def test_vanishing_during_tail_read(self):
        rollout(self.home, 'rollout-a.jsonl', [tc(5)])
        with mock.patch.object(al, '_tail_lines', side_effect=FileNotFoundError):
            self.assertEqual(al.read_codex(self.home, NOW)['source'], 'unknown')

    def test_missing_malformed_unknown(self):
        self.assertEqual(al.read_codex(self.home, NOW)['source'], 'unknown')
        rollout(self.home, 'rollout-a.jsonl', ['{"payload":{"type":"token_count","rate_limits":"bad"}}', tc(500)])
        self.assertEqual(al.read_codex(self.home, NOW)['source'], 'unknown')


class Fallback(Base):
    def test_manual_fills_unknown_and_stale(self):
        al.set_manual('claude', 62, self.state, NOW)
        r = al.read_plan('claude', self.state, self.home, NOW)
        self.assertEqual((r['source'], r['windows']['seven_day']['used_pct']), ('manual', 38))
        snap(self.state, 'a', ts=NOW - 4000)
        self.assertEqual(al.read_plan('claude', self.state, self.home, NOW)['source'], 'manual')
        snap(self.state, 'a', ts=NOW - 5)
        self.assertEqual(al.read_plan('claude', self.state, self.home, NOW)['source'], 'live')

    def test_stale_without_manual_stays_stale(self):
        snap(self.state, 'a', ts=NOW - 4000)
        self.assertEqual(al.read_plan('claude', self.state, self.home, NOW)['source'], 'stale')

    def test_set_validates(self):
        with self.assertRaises(ValueError): al.set_manual('claude', 101, self.state)
        with self.assertRaises(ValueError): al.set_manual('nope', 5, self.state)


class Learn(unittest.TestCase):
    def test_fit(self):
        samples = [(x, 3.0 * x) for x in range(1, 13)]
        r = al.fit_rate(samples)
        self.assertEqual((r['source'], r['n']), ('fit', 12)); self.assertAlmostEqual(r['rate'], 3.0)

    def test_prior_ratios(self):
        o, s, h = (al.fit_rate([], 'claude', m)['rate'] for m in ('opus-5.5', 'sonnet-5.5', 'haiku-5.5'))
        self.assertAlmostEqual(o, 2 * s); self.assertAlmostEqual(o, 40 * h)
        sol, luna = (al.fit_rate([(1, 1)] * 9, 'codex', m) for m in ('gpt-6.1-sol', 'gpt-6-luna'))
        self.assertEqual(sol['source'], 'prior'); self.assertAlmostEqual(sol['rate'], 20 * luna['rate'])

    def test_bad_samples_dropped_and_unknown_model(self):
        r = al.fit_rate([(float('nan'), 1), (0, 5), (1, None)] + [(1, 2)] * 10)
        self.assertEqual((r['n'], r['rate']), (10, 2.0))
        with self.assertRaises(ValueError): al.fit_rate([], 'claude', 'mystery')


@unittest.skipUnless(os.name == 'posix', 'bash script')
class Tee(Base):
    def run_tee(self, payload, *args):
        env = dict(os.environ, DROVER_STATE=self.state)
        return subprocess.run(['bash', str(SCRIPT), *args], input=payload, capture_output=True, text=True, env=env)

    def test_writes_subset_and_prints_line(self):
        payload = json.dumps(dict(session_id='s/1', cwd='/x', model={'id': 'm'}, secret='no', transcript_path='t',
                                  rate_limits=dict(five_hour=dict(used_percentage=23.5, resets_at=NOW))))
        r = self.run_tee(payload)
        self.assertEqual(r.returncode, 0); self.assertEqual(r.stdout.strip(), '5h 24%')
        d = json.loads((Path(self.state) / 'allowance' / 'claude-s_1.json').read_text())
        self.assertEqual(sorted(d), ['cwd', 'model', 'rate_limits', 'session_id', 'ts'])
        self.assertEqual([p.name for p in (Path(self.state) / 'allowance').iterdir()], ['claude-s_1.json'])

    def test_wrapped_command_gets_original_stdin(self):
        payload = json.dumps(dict(session_id='w', extra=1))
        r = self.run_tee(payload, 'cat')
        self.assertEqual(r.stdout, payload)

    def test_no_python_passes_through(self):
        import shutil
        bindir = Path(self.state).parent / 'bin'; bindir.mkdir()
        for tool in ('cat', 'sh', 'bash', 'dirname'):
            (bindir / tool).symlink_to(shutil.which(tool))
        env = dict(os.environ, PATH=str(bindir), DROVER_STATE=self.state)
        r = subprocess.run([shutil.which('bash'), str(SCRIPT), 'cat'], input='{"x":1}', capture_output=True, text=True, env=env)
        self.assertEqual((r.returncode, r.stdout), (0, '{"x":1}'))

    def test_garbage_input_does_not_fail(self):
        r = self.run_tee('not json')
        self.assertEqual(r.returncode, 0); self.assertEqual(r.stdout.strip(), 'allowance n/a')


class FiveHourFormula(unittest.TestCase):
    def setUp(self):
        self.state, self.home = tempfile.mkdtemp(), tempfile.mkdtemp()


    def test_floor_follows_hours_to_reset(self):
        self.assertEqual(al.five_hour_floor(0), 5.0)
        self.assertEqual(al.five_hour_floor(1.5), 12.5)
        self.assertEqual(al.five_hour_floor(5), 30.0)
        self.assertEqual(al.five_hour_floor(None), 30.0)
        self.assertEqual(al.five_hour_floor(2, 1.0, 2.0), 5.0)


if __name__ == '__main__':
    unittest.main()
