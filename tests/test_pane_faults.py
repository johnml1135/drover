"""A bug in pane code reaches the log and the owner and costs no tick; an expected failure stays quiet."""
import logging
import sqlite3
import subprocess
from unittest import mock

from drover import pane_close, pane_compaction, pane_fault, pane_launch, placement
from drover.herdr_client import HerdrRefused, HerdrUnavailable
from drover.launch import AdmissionHeld
from drover.ledger import disk_notices, ops_panes
from drover.panes import PaneService
from drover.pane_send import PaneRefused
from tests.pane_compaction_support import Overnight
from tests.panes_support import ROLE, Resident

BUGS = (AttributeError, NameError, TypeError, KeyError)
SECRET = 'C:/secret text'
TITLE = 'A bug stopped a pane step'


def busy(code=sqlite3.SQLITE_BUSY):
    error = sqlite3.OperationalError(SECRET)
    error.sqlite_errorcode = code
    return error


def each_bug(check):
    """One test per class of mistake, each on a fresh ledger."""
    for bug in BUGS:
        def test(self, bug=bug):
            check(self, bug)
        yield f'test_{check.__name__}_{bug.__name__}', test


class Faults:
    def faults(self):
        return [event['data']['notice'] for event in self.port.events(kind='pane.notice')
                if event['data']['notice']['title'] == TITLE]

    def told_once(self, logged, bug):
        text = ''.join(logged.output)
        self.assertIn(bug.__name__, text)
        self.assertIn('File "', text)           # the traceback's frames
        self.assertNotIn(SECRET, text)
        self.assertEqual(len(self.faults()), 1)
        self.assertNotIn(SECRET, self.faults()[0]['body'])


class Upkeep(Faults, Resident):
    def a_step(self, bug):
        self.launched()
        wake, handoff = self.hooks()
        wake.side_effect = bug(SECRET)
        with self.assertLogs('drover.panes', 'ERROR') as logged:
            self.panes.maintain(self.run_row, self.guard)
            self.panes.maintain(self.run_row, self.guard)
        self.told_once(logged, bug)
        self.assertEqual(handoff.call_count, 2)

    locals().update(each_bug(a_step))

    def test_each_expected_failure_is_a_warning_and_no_notice(self):
        self.launched()
        wake, _ = self.hooks()
        for error in (HerdrUnavailable(SECRET), HerdrRefused('pane_busy', SECRET), PaneRefused('busy'), AdmissionHeld(SECRET),
                      OSError(SECRET), TimeoutError(SECRET), subprocess.TimeoutExpired('x', 1), busy(),
                      busy(sqlite3.SQLITE_LOCKED), busy(517)):
            with self.subTest(type(error).__name__):
                wake.side_effect = error
                with self.assertLogs('drover.panes', 'WARNING') as logged:
                    self.panes.maintain(self.run_row, self.guard)
                self.assertEqual({record.levelno for record in logged.records}, {logging.WARNING})
                self.assertNotIn(SECRET, ''.join(logged.output))
                self.assertEqual(self.faults(), [])

    def test_a_ledger_that_is_really_locked_is_expected(self):
        path = self.cwd / 'locked.db'
        holder, waiter = sqlite3.connect(path), sqlite3.connect(path, timeout=0)
        self.addCleanup(holder.close)
        self.addCleanup(waiter.close)
        holder.execute('CREATE TABLE t(x)')
        holder.execute('BEGIN EXCLUSIVE')
        with self.assertRaises(sqlite3.OperationalError) as caught:
            waiter.execute('SELECT * FROM t')
        self.assertTrue(pane_fault.expected(caught.exception))

    def test_a_query_that_cannot_run_is_a_bug_not_a_busy_ledger(self):
        self.launched()
        wake, _ = self.hooks()
        wake.side_effect = lambda *a: self.conn.execute('SELECT * FROM no_such_table')
        with self.assertLogs('drover.panes', 'ERROR'):
            self.panes.maintain(self.run_row, self.guard)
        self.assertEqual(len(self.faults()), 1)

    def test_a_notice_that_cannot_be_published_costs_nothing(self):
        self.launched()
        wake, handoff = self.hooks()
        wake.side_effect = KeyError(SECRET)
        with mock.patch.object(disk_notices, 'publish', side_effect=TypeError(SECRET)), self.assertLogs('drover.panes', 'ERROR') as logged:
            self.panes.maintain(self.run_row, self.guard)
        handoff.assert_called_once()
        self.assertTrue(any('fault notice failed' in line for line in logged.output))


class WhatTheRuntimeHides(Faults, Resident):
    """`directors.py` turns any exception into a quiet hold or `unknown`, so the adapter tells the owner first."""

    def test_a_bug_in_the_probe_is_unknown_and_told(self):
        self.launched()
        with mock.patch.object(PaneService, 'probe', side_effect=KeyError(SECRET)), self.assertLogs('drover.panes', 'ERROR') as logged:
            self.assertEqual(self.adapter.probe(self.spec_), 'unknown')
        self.told_once(logged, KeyError)

    def test_a_bug_in_a_lapse_check_is_no_and_told(self):
        self.launched()
        for name in ('revivable', 'overdue'):
            with self.subTest(name), mock.patch.object(PaneService, name, side_effect=AttributeError(SECRET)):
                with self.assertLogs('drover.panes', 'ERROR'):
                    self.assertIs(getattr(self.adapter, name)(self.spec_), False)
        self.assertEqual(len(self.faults()), 1)

    def test_a_bug_in_a_sweep_is_told(self):
        self.build()
        with mock.patch.object(ops_panes, 'open_agents', side_effect=TypeError(SECRET)), self.assertLogs('drover.panes', 'ERROR') as logged:
            self.adapter.sweep()
        self.told_once(logged, TypeError)

    def test_a_bug_in_a_launch_is_told_and_the_role_is_held(self):
        self.build()
        self.turns()
        self.wake()
        with mock.patch.object(pane_launch, 'start', side_effect=NameError(SECRET)), self.assertLogs('drover.panes', 'ERROR') as logged:
            self.tick()
        self.told_once(logged, NameError)
        self.assertEqual(self.role()['held_reason'], 'launch-effect-unknown')

    def test_a_held_launch_is_no_bug(self):
        self.build()
        self.turns()
        self.wake()
        with mock.patch.object(pane_launch, 'start', side_effect=AdmissionHeld('herdr-unavailable')):
            self.tick()
        self.assertEqual(self.faults(), [])


class PrivateFiles(Faults, Resident):
    def launch_with(self, error):
        self.build()
        self.turns()
        self.wake()
        with mock.patch.object(pane_launch, '_write_files', side_effect=error):
            self.tick()
        return ops_panes.open_agents(self.conn)[-1]

    def test_a_bug_ends_the_launch_and_is_told(self):
        with self.assertLogs('drover.pane_launch', 'ERROR') as logged:
            self.assertEqual(self.launch_with(AttributeError(SECRET))['state'], 'failed')
        self.told_once(logged, AttributeError)

    def test_a_disk_error_ends_the_launch_and_is_no_bug(self):
        with self.assertLogs('drover.pane_launch', 'WARNING'):
            self.assertEqual(self.launch_with(OSError(SECRET))['state'], 'failed')
        self.assertEqual(self.faults(), [])


class Compaction(Faults, Overnight):
    def a_resume(self, bug):
        self.up()
        self.at(0)
        self.comes_back(boots=False)
        with mock.patch.object(pane_launch, 'resume', side_effect=bug(SECRET)), self.assertLogs(pane_compaction.log, 'ERROR') as logged:
            self.at(900)
        self.told_once(logged, bug)
        self.assertEqual(self.agent()['reason'], 'resume-error')

    locals().update(each_bug(a_resume))

    def test_a_bug_in_the_whole_tick_is_told_and_leaves_no_exception(self):
        self.up()
        with mock.patch.object(pane_compaction, 'tick', side_effect=KeyError(SECRET)), self.assertLogs('drover.panes', 'ERROR') as logged:
            self.assertIsNone(self.panes.compaction_tick(self.hold_))
        self.told_once(logged, KeyError)


class Reads(Faults, Resident):
    def test_a_bug_reading_a_checkout_is_not_an_unreadable_checkout(self):
        def broken(*args):
            raise AttributeError(SECRET)
        with self.assertRaises(AttributeError):
            pane_close.worktree_facts('.', broken)

    def test_git_failing_is_an_unreadable_checkout(self):
        for error in (OSError(SECRET), subprocess.TimeoutExpired('git', 1), ValueError(SECRET)):
            with self.subTest(type(error).__name__):
                self.assertIsNone(pane_close.worktree_facts('.', mock.Mock(side_effect=error)))

    def test_a_bug_in_the_trust_check_is_not_an_untrusted_repo(self):
        with mock.patch.object(placement, 'to_wsl_path', side_effect=AttributeError(SECRET)), self.assertRaises(AttributeError):
            pane_launch.pane_files.repo_trusted('.', dict(name='s', backend='codex'), read=mock.Mock())


class LeftRunning(Faults, Resident):
    def rows(self):
        common = dict(terminal_id='t', kind='claude', seat='s', placement='windows', cwd='c', name='n')
        for number, (state, by) in enumerate((('live', 'drover'), ('compaction', 'drover'), ('retired', 'drover'),
                                              ('failed', 'drover'), ('gone', 'drover'), ('live', 'owner'))):
            ops_panes.record_agent(self.conn, pane_id=f'p{number}', state=state, started_by=by, role_id=ROLE, **common)

    def test_each_running_director_is_one_notice_on_every_restart(self):
        self.rows()
        pane_fault.left_running(self.port)
        pane_fault.left_running(self.port)
        notices = [event['data']['notice'] for event in self.port.events(kind='pane.notice')]
        self.assertEqual([notice['title'] for notice in notices], ['A director is still in a pane'] * 2)
        self.assertEqual(sorted(notice['body'].split(': ')[0].split()[-1] for notice in notices), ['p0', 'p1'])
        self.assertIn('directors-rollout.md', notices[0]['body'])

    def test_a_ledger_with_no_pane_says_nothing(self):
        pane_fault.left_running(self.port)
        self.assertEqual(self.port.events(kind='pane.notice'), [])
