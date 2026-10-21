#!/usr/bin/env python3
"""Run Drover's unittest modules in parallel worker processes and aggregate the result.

    python3 scripts/test.py                       # default tier, two cores left free, at most 16 workers (DROVER_TEST_JOBS overrides)
    python3 scripts/test.py --tier integration    # both tiers: the pre-landing run (sets DROVER_TESTS=integration)
    python3 scripts/test.py -j 4 tests.test_ci    # chosen modules, 4 workers
    python3 scripts/test.py --durations 10        # also list each worker's slowest tests

Each module runs as `python -m unittest <module>` in its own process (tests/__init__ gives every process its own
DROVER_HOME).  Exit status is 0 only if every module passed and at least one test ran.  Use the interpreter whose
packages you want tested: hub tests skip when fastapi is missing (on Windows use the hub venv's python.exe).
Modules are started longest first (timings from the previous run), so wall time is about the slowest module.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TIMINGS = Path(tempfile.gettempdir()) / 'drover-test-timings.json'
MODULE_TIMEOUT_S = 900
MAX_JOBS = 16
SLOW_MODULE_S = 60
SHARD_ABOVE_S = 40          # a module whose last run took longer than this is split across workers
SHARD_SIZE = 12             # tests per shard
HEAVY = {'tests.test_pipeline', 'tests.test_landing', 'tests.test_replans', 'tests.test_garden', 'tests.test_hub_runtime',
         'tests.test_waiting_consumers', 'tests.test_platforms', 'tests.test_waiting_characterization',
         'tests.test_replan_batches', 'tests.test_ci', 'tests.test_repo_entry'}  # before any timing exists
LIST_IDS = '''
import sys, unittest
def flat(s):
    for t in s:
        yield from flat(t) if isinstance(t, unittest.TestSuite) else [t]
for t in flat(unittest.defaultTestLoader.loadTestsFromName(sys.argv[1])):
    print(t.id())
'''


def default_jobs():
    """Workers: DROVER_TEST_JOBS, else the cores less two for the OS and the runner, between 1 and MAX_JOBS."""
    try:
        return max(1, int(os.environ['DROVER_TEST_JOBS']))
    except (KeyError, ValueError):
        return max(1, min(MAX_JOBS, (os.cpu_count() or 1) - 2))


def modules():
    return sorted(f'tests.{p.stem}' for p in (ROOT / 'tests').glob('test_*.py'))


def load_timings():
    try:
        return json.loads(TIMINGS.read_text())
    except (OSError, ValueError):
        return {}


def counts(output):
    ran = re.search(r'^Ran (\d+) tests? in', output, re.M)
    skipped = re.search(r'skipped=(\d+)', output.rsplit('Ran ', 1)[-1])
    return (int(ran.group(1)) if ran else 0), (int(skipped.group(1)) if skipped else 0)


def test_ids(name, env):
    """Every test id of a module, or None when it cannot be listed (it then runs whole)."""
    r = subprocess.run([sys.executable, '-c', LIST_IDS, name], cwd=ROOT, env=env, capture_output=True, text=True,
                       errors='replace', stdin=subprocess.DEVNULL)
    ids = r.stdout.split()
    return ids if r.returncode == 0 and ids else None


def run_module(name, env, durations, timeout, ids=None):
    start = time.monotonic()
    cmd = [sys.executable, '-m', 'unittest', *(ids or [name])] + (['--durations', str(durations)] if durations else [])
    try:
        r = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, errors='replace', timeout=timeout,
                           stdin=subprocess.DEVNULL)
        code, output = r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired as error:
        text = lambda b: b.decode(errors='replace') if isinstance(b, bytes) else (b or '')
        code, output = 124, f'TIMEOUT after {timeout}s\n' + text(error.stdout) + text(error.stderr)
    ran, skipped = counts(output)
    last = next((line for line in reversed(output.splitlines()) if line.strip()), '')
    if code != 0 and not ran and re.match(r'unittest\.case\.SkipTest: ', last):
        code, skipped = 0, 1       # the module skips itself at import (for example hub deps missing); discover reports a skip
    if ids and not ran and code == 0:
        code = 1
    return {'module': name, 'code': code, 'ran': ran, 'skipped': skipped, 'seconds': time.monotonic() - start,
            'output': output}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('modules', nargs='*', help='dotted test modules (default: every tests/test_*.py)')
    p.add_argument('-j', '--jobs', type=int, default=default_jobs())
    p.add_argument('--tier', choices=('default', 'integration'), default='default',
                   help="'integration' also runs the integration tier (sets DROVER_TESTS=integration)")
    p.add_argument('--durations', type=int, default=0, metavar='N', help='show each module worker its N slowest tests')
    p.add_argument('--timeout', type=int, default=MODULE_TIMEOUT_S, help='per-module wall limit in seconds')
    args = p.parse_args(argv)

    chosen = [m.removesuffix('.py').replace('/', '.').replace('\\', '.') for m in args.modules] or modules()
    env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GCM_INTERACTIVE': 'never', 'GIT_ASKPASS': '', 'PYTHONUNBUFFERED': '1'}
    if args.tier == 'integration':
        env['DROVER_TESTS'] = 'integration'
    else:
        env.pop('DROVER_TESTS', None)
    owned = not env.get('DROVER_TEST_TMPDIR')
    if owned:
        cutoff = time.time() - 24 * 60 * 60        # fixtures left by crashed runs
        for path in Path(tempfile.gettempdir()).glob('drover-fix-*'):
            try:
                if path.lstat().st_mtime < cutoff:
                    if path.is_symlink() or not path.is_dir():
                        path.unlink()
                    else:
                        shutil.rmtree(path)
            except OSError:
                pass
        # Not under a `.git`: a stray /tmp/.git would make every fixture look like repository content.
        base = next((b for b in (tempfile.gettempdir(), '/dev/shm', '/var/tmp') if os.path.isdir(b) and
                     not any((p / '.git').exists() for p in (Path(b).absolute(), *Path(b).absolute().parents))), None)
        env['DROVER_TEST_TMPDIR'] = tempfile.mkdtemp(prefix='drover-fix-gate-', dir=base)
    else:
        Path(env['DROVER_TEST_TMPDIR']).mkdir(parents=True, exist_ok=True)
    env['GIT_CEILING_DIRECTORIES'] = os.pathsep.join(
        x for x in (env.get('GIT_CEILING_DIRECTORIES'), str(Path(env['DROVER_TEST_TMPDIR']).absolute().parent)) if x)
    timings = load_timings()
    chosen.sort(key=lambda m: -timings.get(m, (ROOT / 'tests' / (m.split('.')[-1] + '.py')).stat().st_size / 1000
                                           if (ROOT / 'tests' / (m.split('.')[-1] + '.py')).exists() else 0))
    jobs = []
    for name in chosen:
        ids = None
        if args.jobs > 1 and (timings.get(name, 0) > SHARD_ABOVE_S or (name not in timings and name in HEAVY)):
            ids = test_ids(name, env)
        if ids and len(ids) > SHARD_SIZE:
            count = -(-len(ids) // SHARD_SIZE)
            size = -(-len(ids) // count)
            jobs += [(name, ids[i:i + size]) for i in range(0, len(ids), size)]
        else:
            jobs.append((name, None))
    print(f'{len(chosen)} modules in {len(jobs)} jobs, {args.jobs} workers, tier={args.tier}, python={sys.executable}',
          flush=True)
    results, lock, start = [], threading.Lock(), time.monotonic()

    def work(job):
        name, ids = job
        result = run_module(name, env, args.durations, args.timeout, ids)
        with lock:
            results.append(result)
            status = 'ok  ' if result['code'] == 0 else 'FAIL'
            print(f"{status} {result['module']:<36} {result['ran']:>4} tests {result['skipped']:>3} skipped "
                  f"{result['seconds']:7.1f}s", flush=True)
        return result

    try:
        with ThreadPoolExecutor(args.jobs) as pool:
            list(pool.map(work, jobs))
    finally:
        if owned:
            shutil.rmtree(env['DROVER_TEST_TMPDIR'], ignore_errors=True)
    wall = time.monotonic() - start
    try:
        totals = {}
        for r in results:
            totals[r['module']] = totals.get(r['module'], 0) + r['seconds']
        TIMINGS.write_text(json.dumps({**timings, **totals}))
    except OSError:
        pass

    failed = [r for r in results if r['code'] != 0]
    for r in failed:
        print(f"\n===== {r['module']} (exit {r['code']}) =====\n{r['output']}")
    if args.durations:
        for r in sorted(results, key=lambda r: -r['seconds']):
            tail = r['output'].split('Slowest test durations', 1)
            if len(tail) > 1:
                print(f"\n--- {r['module']} slowest ---" + tail[1].split('\n\n')[0].rstrip())
    ran, skipped = sum(r['ran'] for r in results), sum(r['skipped'] for r in results)
    slow = [r['module'] for r in results if r['seconds'] > SLOW_MODULE_S]
    print(f"\nRan {ran} tests ({skipped} skipped) in {len({r['module'] for r in results})} modules, wall {wall:.1f}s, "
          f"{len(failed)} failed modules" + (f"; modules over {SLOW_MODULE_S}s: {', '.join(slow)}" if slow else ''))
    if ran == 0 and not failed:
        print('no tests ran')
        return 1
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
