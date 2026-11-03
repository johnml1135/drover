"""`StageRequest` and `StageClock`: what a stage is asked and how long it has. The clock is faked through `clock=`."""
import copy
import unittest

from drover import placement_paths
from drover.disk_stage import StageClock, StageRequest


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
        self.assertEqual([clock_at(t).can_mutate() for t in (0, 1440, 1500, 1500.1)], [True, True, True, False])
        self.assertEqual([clock_at(t, 100).can_mutate() for t in (79.9, 80, 80.1)], [True, True, False])
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


if __name__ == '__main__':
    unittest.main()
