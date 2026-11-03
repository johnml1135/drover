"""The scenes of the other stages: temp (2), caches (3), disposable paths and the budget purge (5), the report (6).

Each is built once and served dry, then applied, as stage 4's are; `tests.disk_trace` runs them. Every path a stage must keep
is one it would otherwise take: a scene that holds nothing worth deleting proves nothing. The paths a stage keeps with no change
under way share a tree, one row each, for the time a tree costs; what changes mid-stage has its own tree, and no dry run.
"""
import shutil
import subprocess

from tests.disk_fixture import DAY
from tests.disk_trace_scene import FEATURE, TRACKS_BIN, active, task_at

TIMEOUT = subprocess.TimeoutExpired(['trace-cache'], 120)


def on(stage, build, **options):
    """A scene served at `stage`; what `build` returns, when a dict, joins `options`."""
    def scene(s):
        answer = build(s)
        return {**options, 'stage': stage, **(answer if isinstance(answer, dict) else {})}
    return scene


def mid(stage, build, **options):
    """A scene whose change lands in a removal, so a dry run would only repeat the plain scene's."""
    return on(stage, build, applied_only=True, **options)


def declare_temp(s, root):
    root.mkdir(parents=True, exist_ok=True)
    s.cfg['disk_cleanup']['temp_roots'] = [str(root)]
    return root


def temp(s, *names, age=8 * DAY):
    for name in names:
        s.file(s.temp_root / name, age=age)
    return s.temp_root / names[0]


def temp_kept(s):
    """Stale but for one thing, each; the state directory and worktree root have scenes of their own, as they are roots."""
    temp(s, 'recent', age=DAY)
    temp(s, 'recent_inside/old')
    temp(s, 'recent_inside/new', age=DAY)
    temp(s, 'git/.git/HEAD', 'venv/.venv/x', 'repository/sub/HEAD')
    s.links.update((temp(s, 'link'), temp(s, 'holds_link/a')))
    s.elsewhere.add(temp(s, 'holds_other_drive/a'))
    s.busy_paths.add(str(temp(s, 'busy')))
    return active(temp(s, 'live'))


def temp_in_a_repository(s):
    s.file(s.root / 'held/HEAD')
    declare_temp(s, s.root / 'held/sub')
    s.file(s.root / 'held/sub/old')


def temp_under_a_worktree_root(s):
    declare_temp(s, s.folder('stray').parent)


def temp_in_the_state_directory(s):
    s.file(declare_temp(s, s.state / 'scratch') / 'old')


def temp_goes_busy(s):
    path = temp(s, 'old')
    s.fresh = lambda: s.busy_paths.add(str(path))


def temp_root_becomes_a_link(s):
    """Ancestors are proven once, so only the fresh `safe_path` of the entry sees its root turn into a link."""
    temp(s, 'old')
    s.fresh = lambda: s.links.add(s.temp_root)


def temp_grows(s):
    path = temp(s, 'old/a')
    s.fresh = lambda: s.file(path.parent / 'b', 'more than it was')


def temp_busy_with_reason(s):
    s.busy_paths.add(str(temp(s, 'old')))
    s.process_reason = 'process visibility unavailable'


def process_reports(s, stage_scene, **facts):
    stage_scene(s)
    s.process_facts.update(facts)


def budget_for_root(s):
    temp(s, 'old')
    s.cfg['disk_cleanup']['temp_root_budget_s'] = -1


TEMP = dict(
    temp_stale=on(2, lambda s: temp(s, 'old')),
    temp_stale_folder=on(2, lambda s: temp(s, 'old/a', 'old/b/c')),
    temp_kept=on(2, temp_kept),
    temp_in_a_repository=on(2, temp_in_a_repository),
    temp_under_a_worktree_root=on(2, temp_under_a_worktree_root),
    temp_holds_a_worktree_root=on(2, lambda s: s.folder('stray') and declare_temp(s, s.root)),
    temp_in_the_state_directory=on(2, temp_in_the_state_directory),
    temp_busy_with_reason=on(2, temp_busy_with_reason),
    temp_goes_busy=mid(2, temp_goes_busy),
    temp_grows=mid(2, temp_grows),
    temp_root_becomes_a_link=mid(2, temp_root_becomes_a_link),
    temp_root_partial=on(2, budget_for_root),
    temp_deadline_at_the_start=on(2, lambda s: temp(s, 'old'), timeout=0),
    temp_root_on_another_drive=on(2, lambda s: s.elsewhere.add(temp(s, 'old').parent)),
    temp_root_is_a_link=on(2, lambda s: s.links.add(temp(s, 'old').parent)),
    temp_process_out_of_scope=on(2, lambda s: process_reports(s, lambda s: temp(s, 'old'), out_of_scope=2)),
    temp_process_query_timeout=on(2, lambda s: process_reports(
        s, lambda s: temp(s, 'old'), query_failure=dict(reason_code='process_query_timeout', error='query timed out'))),
)


def cache_folder(s, *names, age=40 * DAY, root='cache', **entry):
    folder = s.root / root
    for name in names:
        s.file(folder / name, age=age)
    folder.mkdir(exist_ok=True)
    s.cfg['disk_cleanup']['caches'].append(dict(path=str(folder), placement=s.side, **entry))
    return folder


def cache_command(s, returncode=0, raises=None, side=None):
    """A declared command with a real effect: it takes one file, so `deleted` shows whether it ran."""
    folder = s.root / 'tool'
    marker = s.file(folder / 'cached')

    def run(argv):
        if raises:
            raise raises
        if not returncode:
            marker.unlink()
        return subprocess.CompletedProcess(argv, returncode, 'said', '')
    s.commands['trace-cache'] = run
    s.cfg['disk_cleanup']['caches'].append(dict(cwd=str(folder), argv=['trace-cache', 'clean'], placement=side or s.side))


def cache_old_and_new(s):
    cache_folder(s, 'old')
    s.file(s.root / 'cache/new', age=DAY)


def cache_at_level(s, free=None):
    cache_folder(s, 'a', age=DAY)
    s.file(s.root / 'cache/b', age=2 * DAY)
    s.free_gb = free or s.free_gb


def cache_kept(s):
    """Each declared path is judged whole, so each has its own entry; none of them may lose a file."""
    s.cfg['disk_cleanup']['caches'].append(dict(path=str(s.common().parent), placement=s.side))
    cache_folder(s, 'old', root='worktrees/alpha.worktrees/stray')
    cache_folder(s, 'old', 'sub/HEAD', root='holds_repository')
    cache_folder(s, 'old', '.venv/x', root='holds_venv')
    s.links.add(cache_folder(s, 'old', root='holds_link') / 'old')
    s.elsewhere.add(cache_folder(s, 'old', root='holds_other_drive') / 'old')
    return active(cache_folder(s, 'old', root='live') / 'old')


def cache_goes_busy(s):
    path = cache_folder(s, 'old') / 'old'
    s.fresh = lambda: s.busy_paths.add(str(path))


def cache_becomes(s, change):
    path = cache_folder(s, 'old') / 'old'
    s.fresh = lambda: change(path)


def cache_rewritten(s):
    cache_becomes(s, lambda path: s.file(path, 'a different size', age=40 * DAY))


def cache_busy_with_reason(s):
    s.busy_paths.add(str(cache_folder(s, 'old') / 'old'))
    s.process_reason = 'process visibility unavailable'


CACHES = dict(
    cache_command=on(3, cache_command),
    cache_command_fails=on(3, lambda s: cache_command(s, 1)),
    cache_command_times_out=on(3, lambda s: cache_command(s, raises=TIMEOUT)),
    cache_command_other_side=on(3, lambda s: cache_command(s, side=s.other_side)),
    cache_command_other_drive=on(3, lambda s: cache_command(s) or s.elsewhere.add(s.root / 'tool')),
    cache_by_age=on(3, cache_old_and_new),
    cache_deadline_at_the_start=on(3, lambda s: cache_command(s) or cache_folder(s, 'old'), timeout=0),   # command first
    cache_by_declared_age=on(3, lambda s: cache_folder(s, 'old', age=2 * DAY, max_age_days=1)),
    cache_warn_level=on(3, cache_at_level, level='warn'),
    cache_critical_level=on(3, cache_at_level, level='critical'),
    cache_healthy_level=on(3, cache_at_level, level='ok'),
    cache_daily_ignores_level=on(3, cache_at_level, level='critical', mode='daily'),
    cache_stops_once_free=on(3, lambda s: cache_at_level(s, [10., 200.]), level='hold'),
    cache_free_unknown=on(3, lambda s: cache_at_level(s, [None]), level='hold'),
    cache_kept=on(3, cache_kept),
    cache_busy=on(3, lambda s: s.busy_paths.add(str(cache_folder(s, 'old') / 'old'))),
    cache_busy_with_reason=on(3, cache_busy_with_reason),
    cache_goes_busy=mid(3, cache_goes_busy),
    cache_becomes_a_link=mid(3, lambda s: cache_becomes(s, s.links.add)),
    cache_leaves_the_drive=mid(3, lambda s: cache_becomes(s, s.elsewhere.add)),
    cache_rewritten=mid(3, cache_rewritten),
    cache_process_visibility=on(3, lambda s: process_reports(
        s, lambda s: cache_folder(s, 'old'), unreadable=[dict(pid=7, name='x.exe')])),
    cache_process_query_fails=on(3, lambda s: process_reports(
        s, lambda s: cache_folder(s, 'old'), query_failure=dict(reason_code='process_visibility_unavailable', error='no query'))),
)


def output(s, name='w', files=('bin/x',), declared=('bin',), **options):
    """A worktree holding ignored files under a declared disposable path, each older than the idle limit."""
    s.declared[:] = declared
    s.exclude(*[pattern.split('/')[0] for pattern in declared])
    return s.worktree(name, extra=files, **options)


def output_glob_in_an_untracked_folder(s):
    """Report-only status collapses the folder, so the stage must ask again to see that only one file is declared."""
    s.declared[:] = ['out/*.bin']
    s.worktree('w', extra=['out/a.bin', 'out/b.txt'])


def output_local_declared(s):
    s.machines[0]['local_disposable'] = ['bin']
    s.exclude('bin')
    s.worktree('w', extra=['bin/x'])


def output_rejected(s):
    """Every worktree here holds a declared path that is old enough, and is rejected as a whole."""
    shutil.rmtree(output(s, 'gone'))
    s.file(s.common().parent / 'bin/x')
    output(s, 'unsafe', at=s.root / 'elsewhere' / '.venv' / 'unsafe')
    output(s, 'other_side', lock=s.other_side)
    output(s, 'note', lock='a note')
    s.elsewhere.add(output(s, 'other_drive'))
    s.busy_paths.add(str(output(s, 'busy')))
    s.file(output(s, 'recent') / 'bin/x', age=60)
    s.links.add(output(s, 'link') / 'bin' / 'x')
    s.unreadable.add(output(s, 'unreadable') / 'bin' / 'x')


def output_kept(s):
    """Whole worktrees that pass, each with one path the unit's own checks keep."""
    s.elsewhere.add(output(s, 'other_drive') / 'bin')
    output(s, 'holds_git', files=('bin/inner/.git/HEAD',))
    output(s, 'holds_repository', files=('bin/inner/HEAD',))
    output(s, 'holds_config', files=('bin/inner/config',))
    output(s, 'undeclared', files=('cache/y',))
    s.exclude('bin', 'cache')
    live, inside = output(s, 'live'), output(s, 'live_inside')
    return dict(tasks=dict(active_worktrees=[str(live), str(inside / 'bin' / 'x')]))


def output_recent(s):
    s.file(output(s) / 'bin/x', age=60)


def output_nested(s):
    s.exclude('bin')
    s.declared[:] = ['bin']
    s.worktree('inner', at=s.worktree('w', extra=['bin/x']) / 'bin' / 'inner')


def output_tracked_changed(s):
    s.file(output(s, commit=TRACKS_BIN) / 'bin/tracked.txt', 'changed')


def output_staged(s):
    output(s)
    s.staged.add('bin/x')


def output_status_fails(s):
    output(s)
    s.refuse('status', 'fatal: status failed')


def output_listing_refused(s):
    output(s)
    s.refuse('worktree list', 'fatal: listing failed', s.common().parent)


def output_other_repo_unlisted(s):
    """Beta lists, alpha does not: nesting cannot be judged, so beta's disposable path stays too."""
    s.repo('beta')
    output(s, 'b', repo='beta')
    s.refuse('worktree list', 'fatal: listing failed', s.common().parent)


def jump_clock(s):
    s.clock = 1e9


def settled_past_the_line(s):
    """Stage 4: the line passes as a settled worktree's removal starts, so `can_mutate` keeps it before journal `planned`."""
    s.worktree('w')
    s.midstage = lambda request: jump_clock(s)


def output_deadline_between_units(s):
    """Two units in status order; the clock runs out once the first is judged."""
    s.declared[:] = ['bin', 'out']
    s.worktree('w', extra=['bin/x', 'out/y'])
    s.each_run = lambda: s.git.after.update({'ls-files': lambda: jump_clock(s)})


def output_deadline_between_worktrees(s):
    """The clock runs out as `a`'s one unit is judged: `b`, and the repo after this one, are named and left alone."""
    s.repo('beta')
    output(s, 'a')
    output(s, 'b')
    s.each_run = lambda: s.git.after.update({'ls-files': lambda: jump_clock(s)})


def at_removal(change):
    """`change(s, worktree)` lands as the removal starts, after the stage has judged the path."""
    def build(s):
        path = output(s)
        s.midstage = lambda request: change(s, path, request)
    return build


def after_status(change):
    """`change(s, worktree)` lands after the removal has read the listing and status, for its last look to catch."""
    return at_removal(lambda s, path, request: s.git.after.update(status=lambda: change(s, path, request)))


def goes_busy(s):
    path = output(s)
    s.fresh = lambda: s.busy_paths.add(str(path))


def clear_declared(s, path, request):
    s.declared.clear()


def rewrite(s, path, request):
    s.file(path / 'bin/x', 'a different size')


def registered_above(s, path, request):
    s.registered_only(path / 'bin', 'bin')


OUTPUTS = dict(
    output_removed=on(5, output),
    output_two_paths=on(5, lambda s: output(s, files=('bin/x', 'bin/y'))),
    output_untracked_declared=on(5, lambda s: s.declared.append('bin') or s.worktree('w', extra=['bin/x'])),
    output_glob_in_an_untracked_folder=on(5, output_glob_in_an_untracked_folder),
    output_local_declared=on(5, output_local_declared),
    output_rejected=on(5, output_rejected),
    output_kept=on(5, output_kept),
    output_task_at_path=on(5, lambda s: task_at(output(s))),
    output_idle=on(5, output_recent),
    output_idle_warn=on(5, output_recent, level='warn', applied_only=True),
    output_idle_queue=on(5, output_recent, level='queue', applied_only=True),
    output_idle_hold=on(5, output_recent, level='hold', applied_only=True),
    output_idle_critical=on(5, output_recent, level='critical', applied_only=True),
    output_nested=on(5, output_nested),
    output_tracked=on(5, lambda s: output(s, commit=TRACKS_BIN)),
    output_tracked_changed=on(5, output_tracked_changed),
    output_staged=on(5, output_staged),
    output_is_a_link=on(5, lambda s: s.links.add(output(s) / 'bin' / 'x'), level='hold'),
    output_folder_is_a_link=on(5, lambda s: s.links.add(output(s) / 'bin'), level='hold'),
    output_status_fails=on(5, output_status_fails),
    repository_unavailable=on(5, output_listing_refused),
    output_other_repo_unlisted=on(5, output_other_repo_unlisted),
    output_deadline_at_the_start=on(5, output, timeout=0),
    output_deadline_between_units=on(5, output_deadline_between_units),
    output_deadline_between_worktrees=on(5, output_deadline_between_worktrees),
    output_goes_busy=mid(5, goes_busy),
    output_moved_at_removal=mid(5, at_removal(lambda s, path, request: s.move('w'))),
    output_moved_after_status=mid(5, after_status(lambda s, path, request: s.move('w'))),
    output_tag_changed=mid(5, at_removal(lambda s, path, request: s.tag('w', 'alpha', 'session:other'))),
    output_declarations_changed=mid(5, at_removal(clear_declared)),
    output_rewritten=mid(5, at_removal(rewrite)),
    output_becomes_tracked=mid(5, at_removal(lambda s, path, request: s.staged.add('bin/x'))),
    output_registered_above=mid(5, after_status(registered_above)),
    output_repository_above=mid(5, after_status(lambda s, path, request: s.file(path / 'bin/HEAD'))),
    output_leaves_the_drive=mid(5, after_status(lambda s, path, request: s.elsewhere.add(path / 'bin' / 'x'))),
    output_goes_live=mid(5, after_status(lambda s, path, request: request['tasks']['active_worktrees'].append(str(path / 'bin/x')))),
)


def budget(windows=(), target=1):
    return dict(target_gb=target, windows_paths=None if windows is None else list(windows))


def purge(s, *used, windows=(), files=('bin/x',)):
    """Two worktrees, `old` idle longest. `used` is what each reading of used space says, the last repeating."""
    s.used_gb = list(used)
    output(s, 'old', files=files)
    s.file(output(s, 'new', files=files) / files[0], age=3 * DAY)
    return dict(budget=budget(windows))


def budget_deadline_mid_purge(s):
    answer = purge(s, 5.)
    s.midstage = lambda request: jump_clock(s)
    return answer


def budget_windows_process(s):
    s.used_gb = [5.]
    return dict(budget=budget([str(output(s) / 'bin')]))


def budget_idle_at_critical(s):
    s.used_gb = [5.]
    output_recent(s)
    return dict(budget=budget(), level='critical')


def budget_tracked_in_folder(s):
    s.used_gb = [5.]
    output(s, commit=TRACKS_BIN)
    return dict(budget=budget())


def budget_becomes_tracked(s):
    """A whole folder is one unit, so the target itself is never listed: only the check of each path sees one newly tracked."""
    s.used_gb = [5.]
    output(s, files=('bin/a', 'bin/b'))
    s.midstage = lambda request: s.staged.add('bin/a')
    return dict(budget=budget(), applied_only=True)


def budget_wrong_side(s):
    answer = purge(s, 5.)
    s.budget_side = s.other_side
    return answer


BUDGETS = dict(
    budget_met=on(5, lambda s: purge(s, 5., 1.)),
    budget_not_needed=on(5, lambda s: purge(s, .5)),
    budget_shortfall=on(5, lambda s: purge(s, 5.)),
    budget_whole_folder=on(5, lambda s: purge(s, 5., 1., files=('bin/a', 'bin/b'))),
    budget_deadline_mid_purge=on(5, budget_deadline_mid_purge),
    budget_deadline_gathering=on(5, lambda s: purge(s, 5.), timeout=0),
    budget_becomes_tracked=on(5, budget_becomes_tracked),
    budget_used_unknown=on(5, lambda s: purge(s, None)),
    budget_used_lost_midway=on(5, lambda s: purge(s, 5., None)),
    budget_used_nonsense=on(5, lambda s: purge(s, float('inf'))),
    budget_processes_unknown=on(5, lambda s: purge(s, 5., windows=None)),
    budget_windows_process=on(5, budget_windows_process),
    budget_idle_at_critical=on(5, budget_idle_at_critical),
    budget_tracked_in_folder=on(5, budget_tracked_in_folder),
    budget_wrong_side=on(5, budget_wrong_side),
)


def report_rows(s):
    """One tree holding each row kind, and each reason a kind is left out. The budget counts this machine's worktrees alone."""
    s.machines[0]['budget_gb'] = 1
    s.worktree('w')
    s.worktree('untracked', extra=['out/x'])
    output(s, 'ignored')
    s.worktree('other_side', lock=s.other_side, extra=['out/x'])
    s.elsewhere.add(s.worktree('other_drive'))
    s.unreadable.add(s.worktree('partial') / 'README.md')
    s.worktree('task_merged', owner='drover:T1')
    s.worktree('task_unmerged', commit=FEATURE, owner='drover:T2')
    s.worktree('task_elsewhere', owner='drover:T3', lock=s.other_side)
    s.folder('stray')
    s.file(s.report_root / 'item')
    return dict(tasks=dict(tasks=[dict(id=f'T{n}', terminal=False, live=True) for n in (1, 2, 3)]))


def report_status_fails(s):
    s.worktree('w')
    s.refuse('status', 'fatal: status failed')


def report_listing_refused(s):
    s.worktree('w')
    s.refuse('worktree list', 'fatal: listing failed', s.common().parent)


def report_deadline_between_worktrees(s):
    """The clock runs out at the main checkout's status read: `a`, and the repo after this one, are named."""
    s.repo('beta')
    s.worktree('a')
    s.each_run = lambda: s.git.after.update(status=lambda: jump_clock(s))


REPORTS = dict(
    report_rows=on(6, report_rows),
    report_status_fails=on(6, report_status_fails),
    report_root_is_a_link=on(6, lambda s: s.file(s.report_root / 'item') and s.links.add(s.report_root)),
    report_repo_error=on(6, report_listing_refused),
    report_deadline_at_the_start=on(6, report_rows, timeout=0),
    report_deadline_between_worktrees=on(6, report_deadline_between_worktrees),
)
STAGE_SCENES = {**TEMP, **CACHES, 'settled_past_the_line': mid(4, settled_past_the_line), **OUTPUTS, **BUDGETS, **REPORTS}
