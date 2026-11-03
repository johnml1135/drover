"""`StageRequest` and `StageClock`: what a stage is asked and how long it has. The clock is faked through `clock=`."""
import copy
import io
import json
import unittest
from pathlib import Path

from drover import disk_inventory, disk_paths, placement_paths
from drover.disk_stage import StageAbandoned, StageClock, StageRequest, StageRun, read_rows


def at(*times):
    """A clock reading 0 at the stage's start, then each of `times` in turn, holding the last."""
    readings = [0.0, *times]
    def clock():
        return readings.pop(0) if len(readings) > 1 else readings[0]
    return clock


def clock_at(elapsed, timeout_s=1800, **options):
    return StageClock(timeout_s, clock=at(elapsed), **options)


def todays_translation(request, side, convert):
    """disk_runtime.py:32, :36 and :39-58 at 16a3623, verbatim but for `convert` passed in and `placement` read as
    `placement_paths`: the oracle `for_side` must match. Frozen here, because Task 33 replaces that code with
    `for_side` itself."""
    placement = placement_paths
    request = copy.deepcopy(request)
    request['side'] = side
    request['state_dir'] = convert(request['state_dir'])
    snapshot = request.get('tasks') or {}
    if snapshot:
        snapshot['active_worktrees'] = [convert(p) for p in snapshot.get('active_worktrees', [])]
        for task in snapshot.get('tasks', []):
            if task.get('worktree'):
                task['worktree'] = convert(task['worktree'])
    for repo in request['repos']:
        for key in ('repo', 'worktrees'):
            if repo.get(key):
                repo[key] = convert(repo[key])
    for key in ('temp_roots', 'report_roots'):
        request['paths'][key] = [convert(p) for p in request['paths'].get(key, [])
                                 if placement.cleanup_path_side(p) == side]
    request['paths']['caches'] = [c for c in request['paths'].get('caches', [])
                                  if placement.cleanup_path_side(c.get('path', c.get('cwd'))) == side]
    for cache in request['paths'].get('caches', []):
        key = 'path' if 'path' in cache else 'cwd'
        cache[key] = convert(cache[key])
    return request


def wire(**changes):
    """Today's request dict for stage 2 on Windows, as disk_cleanup.py:236 builds it, with paths that cross sides."""
    value = dict(
        stage=2, side='windows', mode='daily', apply=False, now=1000.0,
        drive=dict(name='H', path='H:\\', free_gb=10.0, level='warn'),
        repos=[dict(name='Pan Glöss', repo='H:\\repos\\Pan Glöss', worktrees='H:\\repos\\Pan Glöss.worktrees'),
               dict(name='native', repo='/home/john/repos/naïve', worktrees=None),
               dict(name='bare')],
        paths=dict(worker_timeout_s=1800,
                   temp_roots=['C:\\Users\\jöhn\\Temp dir', '/tmp/drover tmp', '\\\\?\\H:\\scratch ü'],
                   report_roots=['H:\\reports here', '/var/reports'],
                   caches=[dict(path='C:\\Users\\jöhn\\.cache\\pip x'), dict(cwd='/home/john/proj é', command='uv'),
                           dict(cwd='H:\\repos\\Pan Glöss', command='cargo')],
                   worktree_output_idle_s=600),
        state_dir='C:\\Users\\jöhn\\.drover state',
        tasks=dict(active_worktrees=['H:\\repos\\Pan Glöss.worktrees\\t 1', '/home/john/wt é'],
                   tasks=[dict(id=1, worktree='H:\\repos\\Pan Glöss.worktrees\\t 1'), dict(id=2, worktree=None),
                          dict(id=3)]),
        _inventory_id='inv-1', _worker_id='tok-1')
    value.update(changes)
    return value


class Clock(unittest.TestCase):
    def test_late_at_the_80_percent_line_never_when_exempt(self):
        self.assertEqual([StageClock(100, clock=at(t)).late() for t in (79.9, 80)], [False, True])
        self.assertFalse(StageClock(100, clock=at(10_000), exempt=True).late())

    def test_left_and_remaining_never_fall_below_zero(self):
        self.assertEqual((clock_at(0, 100).left(), clock_at(0, 100).remaining()), (100, 80))
        self.assertEqual((clock_at(90, 100).left(), clock_at(90, 100).remaining()), (10, 0))
        self.assertEqual((clock_at(150, 100).left(), clock_at(150, 100).remaining()), (0, 0))

    def test_exempt_remaining_is_left(self):
        for elapsed in (0, 79, 90, 150):
            clock = clock_at(elapsed, 100, exempt=True)
            self.assertEqual(clock.remaining(), clock.left(), elapsed)
        self.assertEqual(clock_at(90, 100, exempt=True).remaining(), 10)

    def test_patience_table(self):       # worker timeout 1800, cap 60, by elapsed
        for elapsed, want in ((0, 60), (1439, 10), (1795, 5), (1800, 1)):
            self.assertEqual(clock_at(elapsed).patience(60), want, elapsed)
        self.assertEqual(clock_at(5000).patience(60), 1)

    def test_mutation_cap_is_fixed_for_the_run(self):
        self.assertEqual(StageClock(1800).mutation_cap(), 60); self.assertEqual(StageClock(100).mutation_cap(), 4)
        clock = StageClock(100, clock=at(0, 50, 99, 500))
        self.assertEqual([clock.mutation_cap() for _ in range(4)], [4, 4, 4, 4])
        self.assertEqual(StageClock(100, exempt=True).mutation_cap(), 4)

    def test_can_mutate_needs_five_capped_steps_left_and_is_true_when_exempt(self):
        self.assertEqual(StageClock.MUTATION_STEPS, 5)
        # No removal starts past the 80% line, even where five capped steps would still fit (1,440 to 1,500 s).
        self.assertEqual([clock_at(t).can_mutate() for t in (0, 1439.9, 1440, 1500)], [True, True, False, False])
        self.assertEqual([clock_at(t, 100).can_mutate() for t in (79.9, 80, 80.1)], [True, False, False])
        self.assertTrue(clock_at(1440, exempt=True).can_mutate())       # an approved removal is never cut off
        self.assertTrue(clock_at(1800, exempt=True).can_mutate())
        self.assertTrue(clock_at(10_000, exempt=True).can_mutate())

    def test_small_worker_timeout_stays_coherent(self):                  # Review Focus 4
        clock = StageClock(11); self.assertAlmostEqual(clock.mutation_cap(), 0.44); self.assertTrue(clock.can_mutate())
        # Five capped steps fit exactly in the margin, so no mutation is allowed to start past the 80% line.
        self.assertEqual([clock_at(t, 11).can_mutate() for t in (8.7, 9.0)], [True, False])
        self.assertAlmostEqual(clock_at(8.7, 11).patience(60), 2.3)       # left() bounds it below the 10 s floor
        self.assertEqual(clock_at(11, 11).patience(60), 1)

    def test_within_gives_a_budget_under_the_stage_clock(self):
        readings = [0.0]
        clock = StageClock(100, clock=lambda: readings[0])
        budget = clock.within(30)
        for now, late, remaining in ((29.9, False, 0.1), (30, True, 0)):
            readings[0] = now
            self.assertEqual(budget.late(), late, now)
            self.assertAlmostEqual(budget.remaining(), remaining)
        self.assertEqual((budget.left(), budget.mutation_cap()), (clock.left(), clock.mutation_cap()))
        readings[0] = 70
        near_line = clock.within(30)        # the stage's own line comes first
        self.assertEqual((near_line.late(), near_line.remaining()), (False, 10))
        readings[0] = 80
        self.assertEqual((near_line.late(), clock.late()), (True, True))
        readings[0] = 0
        exempt = StageClock(100, clock=lambda: readings[0], exempt=True).within(30)
        readings[0] = 30
        self.assertTrue(exempt.late())      # a per-root budget is a budget even under an approved removal's clock


class Request(unittest.TestCase):
    def test_wire_keys_are_todays(self):
        request = StageRequest.from_wire(wire())
        self.assertLessEqual({'_worker_id', '_inventory_id', 'paths', 'state_dir', 'stage', 'side', 'apply'},
                             set(request.to_wire()))
        self.assertEqual(request.to_wire(), wire())
        self.assertEqual((request.policy, request.worker_id, request.inventory_id), (wire()['paths'], 'tok-1', 'inv-1'))

    def test_absent_optional_keys_stay_absent_on_the_wire(self):
        today = wire(stage=4, apply=True, approved_path='H:\\repos\\Pan Glöss.worktrees\\t 1')
        for key in ('mode', '_inventory_id', '_worker_id'):
            del today[key]
        request = StageRequest.from_wire(today)
        self.assertEqual((request.mode, request.inventory_id, request.budget), (None, None, None))
        self.assertEqual(request.to_wire(), today)

    def test_from_wire_round_trips(self):
        budget = dict(target_gb=50, windows_paths=['H:\\x y'])
        request = StageRequest.from_wire(wire(stage=5, budget=budget))
        self.assertEqual(StageRequest.from_wire(request.to_wire()), request)
        self.assertIsInstance(request.repos, tuple)
        sent = request.to_wire()
        sent['paths']['temp_roots'].append('/elsewhere')
        sent['repos'][0]['repo'] = 'changed'
        self.assertEqual(request.to_wire(), wire(stage=5, budget=budget))      # the wire is a copy

    def test_for_side_matches_todays_translation_with_spaces_and_non_ascii(self):    # disk_runtime.py:39-59
        convert = placement_paths.to_wsl_path
        request = StageRequest.from_wire(wire())
        crossed = request.for_side('wsl', convert)
        self.assertEqual(crossed.to_wire(), todays_translation(wire(), 'wsl', convert))
        self.assertEqual(crossed.state_dir, '/mnt/c/Users/jöhn/.drover state')
        # Only the WSL side's roots and caches cross; repos and tasks are converted, not filtered.
        self.assertEqual((crossed.side, crossed.policy['temp_roots'], crossed.policy['caches']),
                         ('wsl', ['/tmp/drover tmp'], [dict(cwd='/home/john/proj é', command='uv')]))
        self.assertEqual(crossed.repos[0]['worktrees'], '/mnt/h/repos/Pan Glöss.worktrees')
        self.assertEqual(crossed.tasks['tasks'][0]['worktree'], '/mnt/h/repos/Pan Glöss.worktrees/t 1')
        self.assertEqual(request.to_wire(), wire())        # the original is untouched
        for other in (wire(tasks={}), wire(tasks=None, paths=dict(worker_timeout_s=60)), wire(repos=[])):
            self.assertEqual(StageRequest.from_wire(other).for_side('wsl', convert).to_wire(),
                             todays_translation(other, 'wsl', convert))
        mark = lambda path: '<' + path + '>'
        self.assertEqual(request.for_side('windows', mark).to_wire(), todays_translation(wire(), 'windows', mark))

    def test_decide_view_is_a_plain_dict_with_approved_path(self):
        request = StageRequest.from_wire(wire())
        view = request.decide_view()
        self.assertIs(type(view), dict)
        self.assertIsNone(view['approved_path'])
        self.assertEqual((view['now'], view['apply'], view['repos']), (1000.0, False, wire()['repos']))
        self.assertEqual(dict(view, approved_path=None)['stage'], 2)
        approved = StageRequest.from_wire(wire(stage=4, approved_path='H:\\t 1')).decide_view()
        self.assertEqual(approved['approved_path'], 'H:\\t 1')
        view['repos'][0]['repo'] = 'changed'
        self.assertEqual(request.repos[0]['repo'], 'H:\\repos\\Pan Glöss')


ROOTS = ('/var/reports', '/var/reports/deep')
WHOLE = ('worktree_inventory', 'untracked_or_ignored', 'budget_usage', 'merged_live_task', 'process_out_of_scope',
         'process_visibility_unavailable', 'temp_root_elapsed', 'wsl_vhdx', 'wsl_vhdx_unavailable')


def root_of(row):
    """compact's root step at 16a3623 (disk_inventory.py:118-128) over ROOTS, as disk_side will pass it in."""
    if row.get('reason_code') in WHOLE:
        return None
    path = Path(row['path'])
    return row.get('root') or str(max((Path(r) for r in ROOTS if Path(r) in (path, *path.parents)),
                                      key=lambda r: len(r.parts), default=path.parent))


def row(path, reason='declared_disposable', action='keep', **extra):
    return dict(dict(path=path, action=action, reason_code=reason, owner=None, files=1, age_s=5, bytes=10), **extra)


class Broken(io.BytesIO):
    def write(self, data):
        raise BrokenPipeError(32, 'Broken pipe')


def stage_run(limit=20, out=None, **changes):
    return StageRun(StageRequest.from_wire(wire(**changes)), StageClock(100, clock=at()), out=out or io.BytesIO(),
                    root_of=root_of, limit=limit)


def sent(run):
    return run._out.getvalue()


def lines(run):
    return [json.loads(line) for line in sent(run).splitlines()]


class Run(unittest.TestCase):
    def test_a_stage_killed_mid_run_keeps_its_settled_rows(self):
        run = stage_run()
        for name in 'abc':
            run.add(row('/var/reports/' + name))
        run.checkpoint()
        written = sent(run).splitlines(keepends=True)
        answer = read_rows(b''.join(written[:2]) + b'{"v":1,"ro')
        self.assertEqual((len(answer.items), answer.complete, answer.partial), (2, False, True))
        self.assertEqual([item['path'] for item in answer.items], ['/var/reports/a', '/var/reports/b'])

    def test_a_cut_inside_a_multibyte_character_is_a_partial_last_line(self):
        whole = b'{"v":1,"row":{"path":"/var/reports/a","action":"keep","reason_code":"x"}}\n'
        cut = '{"v":1,"row":{"path":"/var/ü'.encode('utf-8')[:-1]
        answer = read_rows(whole + cut)
        self.assertEqual(([item['path'] for item in answer.items], answer.complete), (['/var/reports/a'], False))

    def test_a_malformed_complete_line_raises_value_error(self):
        for output in (b'{"v":1,"row":\n', b'\xff\xfe\n', b'{"v":1,"row":{}}\n\n', b'[1]\n'):
            with self.assertRaises(ValueError, msg=output):
                read_rows(output)

    def test_a_line_with_no_v_key_raises_value_error(self):          # the old single-object answer
        for output in (b'{"items":[],"errors":[],"reclaimed_bytes":0}\n', b'{"v":2,"row":{}}\n', b'{"v":1}\n',
                       b'{"v":1,"rows":[]}\n'):
            with self.assertRaises(ValueError, msg=output):
                read_rows(output)

    def test_nothing_at_all_is_not_complete(self):
        answer = read_rows(b'')
        self.assertEqual((answer.items, answer.errors, answer.reclaimed_bytes, answer.complete, answer.partial),
                         ([], [], 0, False, True))
        self.assertEqual((answer.peak_intermediate_bytes, answer.budget), (0, None))

    def test_add_is_the_only_way_in_and_checkpoint_sends_only_settled_rows(self):
        run = stage_run()
        self.assertFalse(hasattr(run, 'items'))
        first = row('/var/reports/\udcff odd')                  # a non-UTF-8 name, as os.fsdecode gives it
        run.add(first)
        first['bytes'] = 999                                    # a row added is settled: later changes do not cross
        self.assertEqual(sent(run), b'')
        run.checkpoint()
        self.assertTrue(sent(run).isascii())
        self.assertEqual(lines(run), [dict(v=1, row=row('/var/reports/\udcff odd'), root=str(Path('/var/reports')))])
        run.add(row('/var/reports/two', reason='temp_root_elapsed', action='report'))
        run.error(dict(path='/x', reason_code='scope', error='no'))
        run.checkpoint()
        run.checkpoint()                                        # nothing new: nothing sent
        self.assertEqual(lines(run)[1:], [{'v': 1, 'row': row('/var/reports/two', 'temp_root_elapsed', 'report')},
                                          {'v': 1, 'error': dict(path='/x', reason_code='scope', error='no')}])
        self.assertEqual(read_rows(sent(run)).items[1]['path'], '/var/reports/\udcff odd')

    def test_add_caps_groups_at_limit_and_tallies_more(self):
        run = stage_run(limit=2)
        run.add(row('/var/reports/d', dirty=list('abcde'), ignored=['x'], reason='kept_dirty'))
        for number in range(5):
            run.add(row('/var/reports/deep/%d' % number, bytes=number))
            run.add(row('/elsewhere/%d' % number, reason='worktree_inventory'))
        run.checkpoint()
        written = lines(run)
        self.assertEqual(written[0]['row'], row('/var/reports/d', dirty=['a', 'b'], ignored=['x'], reason='kept_dirty',
                                                dirty_count=5, ignored_count=1))
        whole = [line for line in written if 'row' in line and line['row']['reason_code'] == 'worktree_inventory']
        self.assertEqual(len(whole), 5)
        grouped = [line['row']['path'] for line in written if line.get('root') == str(Path('/var/reports/deep'))]
        self.assertEqual(grouped, ['/var/reports/deep/0', '/var/reports/deep/1'])
        self.assertEqual([line['more'] for line in written if 'more' in line], [dict(
            reason_code='declared_disposable', root=str(Path('/var/reports/deep')), action='keep', count=3, files=3,
            bytes=9, age_s=5, sample=[row('/var/reports/deep/4', bytes=4), row('/var/reports/deep/3', bytes=3)])])

    def test_read_rows_aggregates_by_stamped_root(self):
        items = [row('/var/reports/deep/%02d' % n, bytes=n % 7, age_s=n) for n in range(25)]
        items[3:3] = [row('/var/reports/w', reason='worktree_inventory', dirty=list(range(30)))]
        items += [row('/var/reports/%d' % n, action='removed', ignored=['i'] * n) for n in range(22)]
        items += [row('/var/reports/x', root='/own/root')]
        items += [row('/var/reports/deep/%02d' % n, bytes=n) for n in range(25, 30)]
        run = stage_run(limit=disk_inventory.LIMIT)
        for number, item in enumerate(items):
            run.add(item)
            if number % 9 == 0:
                run.checkpoint()                     # the `more` tally is a running total, so the last one stands
        run.finish()
        request = dict(paths=dict(temp_roots=list(ROOTS), report_roots=[]), repos=[])
        today = disk_inventory.compact(items, request)
        self.assertEqual(read_rows(sent(run)).items, today)
        self.assertEqual([r['count'] for r in today if 'count' in r], [30, 22])

    def test_a_failed_checkpoint_raises_stage_abandoned_past_every_except(self):
        self.assertFalse(issubclass(StageAbandoned, Exception))
        for out in (Broken(), io.BytesIO()):
            run = stage_run(out=out)
            if not isinstance(out, Broken):
                out.close()                          # a closed pipe raises ValueError, not OSError
            run.add(row('/var/reports/a'))
            with self.assertRaises(StageAbandoned):
                try: run.checkpoint()
                except Exception: self.fail('held by except Exception')

    def test_stop_writes_one_row_and_sets_partial_and_stopped(self):
        run = stage_run()
        machine, left = dict(repo='/var/reports/r1'), [dict(worktree='/w1'), dict(worktree='/w2')]
        later = [dict(repo='/var/reports/r2')]
        self.assertFalse(run.stopped)
        run.stop('settled_deadline', machine, left, later)
        run.stop('settled_deadline', machine, left, later)
        self.assertTrue(run.stopped)
        run.finish()
        answer = read_rows(sent(run))
        self.assertEqual(answer.items, [disk_paths.deadline_row({}, machine, left, later, 'settled_deadline')])
        self.assertEqual((answer.complete, answer.partial), (True, True))

    def test_stop_with_no_machine_writes_the_drive_path(self):
        run = stage_run()
        run.stop('output_deadline', None, [], [dict(repo='/var/reports/r2')])
        self.assertEqual(read_rows(sent(run)).items, [dict(
            path='H:\\', action='report', reason_code='output_deadline', owner=None, files=0, age_s=0, bytes=0,
            count=1, worktrees=[], repos=['/var/reports/r2'])])

    def test_reclaimed_uses_end_when_complete_and_last_line_otherwise(self):
        run = stage_run()
        run.reclaimed(100)
        run.checkpoint()
        run.reclaimed(50)
        self.assertEqual(run.reclaimed_bytes, 150)
        run.checkpoint()
        self.assertEqual([line['reclaimed'] for line in lines(run)], [100, 150])
        self.assertEqual(read_rows(sent(run)).reclaimed_bytes, 150)
        partial = b'{"v":1,"reclaimed":10}\n'
        self.assertEqual(read_rows(partial).reclaimed_bytes, 10)
        self.assertEqual(read_rows(partial + b'{"v":1,"end":{"reclaimed_bytes":30}}\n').reclaimed_bytes, 30)

    def test_end_carries_peak_and_budget(self):
        run = stage_run()
        run.error(dict(reason_code='stage_error'))
        run.scope_errors.append(dict(reason_code='scope_error'))      # today's order: the stage's errors, then scope's
        run.reclaimed(7)
        run.finish(peak_intermediate_bytes=4096, budget=dict(status='met', used_gb=1))
        answer = read_rows(sent(run))
        self.assertEqual((answer.peak_intermediate_bytes, answer.budget, answer.reclaimed_bytes),
                         (4096, dict(status='met', used_gb=1), 7))
        self.assertEqual(answer.errors, [dict(reason_code='stage_error'), dict(reason_code='scope_error')])
        self.assertEqual((answer.complete, answer.partial), (True, False))
        self.assertEqual(lines(run)[-1], {'v': 1, 'end': dict(reclaimed_bytes=7, partial=False,
                                                              peak_intermediate_bytes=4096,
                                                              budget=dict(status='met', used_gb=1))})


if __name__ == '__main__':
    unittest.main()
