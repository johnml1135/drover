"""Stages 4 and 5 and the budget purge on the run: one clock, the one deadline row, and `can_mutate` before journal
`planned`. The clock is faked through `clock=`; git, processes and the journal are faked at their seams."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from drover import disk_budget, disk_removal, disk_worktrees, placement
from drover.disk_stage import StageClock
from tests.disk_fixture import MemoryCleanupState, agreeing_proof, listing_answers, served, stage_request
from tests.disk_fixture import stage_run as stage_run_for

MACHINE = dict(name='alpha', repo='/repo')
LATE = 10 ** 6      # far past any line
GONE = str(Path('/wt/gone').absolute())      # a removal's folder, already gone, in this host's spelling


def at(*times):
    """A clock reading 0 at the stage's start, then each of `times` in turn, holding the last."""
    readings = [0.0, *times]
    return lambda: readings.pop(0) if len(readings) > 1 else readings[0]


def late_factory(timeout_s, **options):
    """`serve`'s clock factory for a stage already past its line when it starts: 0, then LATE for ever."""
    return StageClock(timeout_s, clock=at(LATE), **options)


def gone_row(**changes):
    """A removal candidate that passes every check up to journal `planned`: its folder is already gone."""
    return dict(dict(path=GONE, repo='/repo', head='h', branch='refs/heads/b', reason_code='missing',
                     action='candidate', ignored=[], files=0, bytes=0, age_s=0), **changes)


class Removals(unittest.TestCase):
    """Stages 4 and 5 on the run: one clock, the one deadline row, and `can_mutate` before journal `planned`."""
    def served(self, stage, rows, **changes):
        """`serve` for `stage` over one repo listing `rows`, past its line from the start."""
        with tempfile.TemporaryDirectory() as state, listing_answers(rows), \
                patch.object(placement, 'cleanup_processes', return_value=SimpleNamespace(busy=lambda path: False)):
            value = dict(stage=stage, side=None, apply=False, now=1.0, drive=dict(path='/'), repos=[MACHINE],
                         paths=dict(worker_timeout_s=100), state_dir=state, tasks={}, **changes)
            return served(value, clock_factory=late_factory)

    def test_stage_4_and_5_stamp_at_their_deadline_as_today(self):
        """Ruling 7: an item, not an error, so the dry report's stamp (disk_cleanup.py:318) is allowed as today."""
        listed = [dict(worktree='/wt/a'), dict(worktree='/wt/b')]
        for stage, reason, left in ((4, 'settled_deadline', []), (5, 'output_deadline', ['/wt/a', '/wt/b'])):
            with self.subTest(stage=stage):
                answer = self.served(stage, listed)
                self.assertEqual(answer['errors'], [])
                self.assertTrue(answer['complete'])
                [row] = answer['items']
                self.assertEqual((row['reason_code'], row['action'], row['path'], row['worktrees'], row['repos']),
                                 (reason, 'report', '/repo', left, ['/repo'] if stage == 4 else []))

    def remove(self, clock, **changes):
        store, events, self.discarded = MemoryCleanupState(), [], []
        _, run = stage_request(dict(repos=[MACHINE], apply=True, now=1.0, drive=dict(path='/'), state_dir=None))
        run.clock = clock
        with listing_answers([dict(worktree=GONE)]), agreeing_proof(), \
                patch.object(disk_removal, 'inspect', return_value=gone_row(**changes)), \
                patch.object(placement, 'cleanup_contains', return_value=True), \
                patch.object(disk_removal, 'discard_at', side_effect=lambda *a, **k: (events.append('discard'),
                                                                                       self.discarded.append(k))):
            row = disk_removal.remove_worktree(run, gone_row(**changes), store=store)
        return row, [r['phase'] for r in store.journal], events

    def test_past_the_line_before_planned_keeps_removal_deadline(self):
        row, phases, events = self.remove(StageClock(100, clock=at(85)))
        self.assertEqual((row['action'], row['reason_code']), ('keep', 'removal_deadline'))
        self.assertEqual((phases, events), ([], []))         # no journal row, nothing touched
        row, phases, events = self.remove(StageClock(100, clock=at(10)))
        self.assertEqual((row['action'], phases, events), ('removed', ['planned', 'completed'], ['discard']))

    def test_the_removal_takes_the_mutation_cap(self):
        asked = []
        def step(name):
            return lambda *a, timeout_s: asked.append((name, timeout_s))
        with patch.object(disk_removal, 'rescue_at', side_effect=step('rescue')), \
                patch.object(disk_removal, 'unlock_at', side_effect=step('unlock')):
            row, _, _ = self.remove(StageClock(100, clock=at(10)), branch=None, side_lock='wsl')
        self.assertEqual(row['action'], 'removed')
        self.assertEqual((asked, self.discarded), ([('rescue', 4), ('unlock', 4)], [dict(timeout_s=4)]))     # 6.1:643

    def test_exempt_approved_removal_is_never_cut_off(self):
        row, phases, _ = self.remove(StageClock(100, clock=at(LATE), exempt=True))
        self.assertEqual((row['action'], phases), ('removed', ['planned', 'completed']))
        made = []

        def factory(timeout_s, **options):
            made.append(options.get('exempt', False))
            return StageClock(timeout_s, **options)
        for approved in ('/wt/a', None):
            with tempfile.TemporaryDirectory() as state, \
                    patch.object(placement, 'cleanup_processes', return_value=SimpleNamespace(busy=lambda path: False)):
                value = dict(stage=4, side=None, apply=False, now=1.0, drive=dict(path='/'), repos=[], paths={},
                             state_dir=state, tasks={}, **({'approved_path': approved} if approved else {}))
                served(value, clock_factory=factory)
        self.assertEqual(made, [True, False])           # serve makes an approved removal's clock exempt

    def test_checkpoint_never_runs_between_planned_and_completed(self):
        events, store = [], MemoryCleanupState()
        store.append = lambda record: events.append(record['phase'])
        run = stage_run_for(dict(repos=[MACHINE], apply=True, now=1.0, drive=dict(path='/'), state_dir=None))
        checkpoint = run.checkpoint
        run.checkpoint = lambda: (events.append('checkpoint'), checkpoint())
        listed = [dict(worktree=GONE), dict(worktree=GONE)]
        with listing_answers(listed), agreeing_proof(), \
                patch.object(placement, 'cleanup_processes', return_value=SimpleNamespace(busy=lambda path: False)), \
                patch.object(disk_worktrees, 'inspect', return_value=gone_row()), \
                patch.object(disk_removal, 'inspect', return_value=gone_row()), \
                patch.object(disk_worktrees, 'quarantine_observation', side_effect=lambda r, m, value, **_: dict(row=value)), \
                patch.object(disk_worktrees, 'registrations', return_value={}), \
                patch.object(disk_worktrees, 'unlisted', return_value=[]), \
                patch.object(placement, 'cleanup_contains', return_value=True), \
                patch.object(disk_removal, 'discard_at', side_effect=lambda *a, **k: events.append('discard')):
            disk_worktrees.settled(run, runtime=SimpleNamespace(state=store, evidence=None))
        self.assertEqual(events.count('planned'), 2)
        for start in (i for i, event in enumerate(events) if event == 'planned'):
            self.assertEqual(events[start:start + 3], ['planned', 'discard', 'completed'])
        first = events.index('completed')
        self.assertIn('checkpoint', events[first:events.index('planned', first)])     # between the two units

    def test_purge_reads_stopped_not_out_items(self):
        """A gathering cut off stops the run, and that, not a row's reason, makes the purge's outcome a deadline."""
        def gathering(stop):
            def rows(run):
                if stop:
                    run.stop('output_deadline', None, [], [])
                else:       # a row that only looks like the deadline row
                    run.add(dict(path='/repo', action='report', reason_code='output_deadline'))
                return iter(())
            return rows
        outcomes = []
        for stop in (True, False):
            run = stage_run_for(dict(stage=5, apply=True, now=1.0, drive=dict(path='/'), repos=[],
                                     budget=dict(target_gb=1, windows_paths=[])))
            with patch.object(disk_budget, 'SIDE', placement.cleanup_side()), \
                    patch.object(disk_budget, 'disposable_rows', gathering(stop)), \
                    patch.object(placement, 'wsl_used_gb', return_value=5.0):
                outcomes.append(disk_budget.purge(run)['budget']['status'])
        self.assertEqual(outcomes, ['deadline', 'shortfall'])



if __name__ == '__main__':
    unittest.main()
