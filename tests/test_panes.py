"""A director's pane: who it belongs to, and what a read of it proves. Herdr here is always the fake."""
import ast
import unittest
from pathlib import Path

from drover import pane_handoff, pane_launch, pane_send, pane_wake
from drover.ledger import ops_panes
from drover.pane_send import PaneRefused
from tests.pane_launch_support import LONE_SHELL
from tests.pane_send_support import CLAUDE_IDLE, PANE, WRITE_VERBS
from tests.panes_support import ROLE, Resident

ROOT = Path(__file__).resolve().parents[1]


def builds_a_client(tree):
    return any(isinstance(n, ast.Call) and (getattr(n.func, 'id', None) or getattr(n.func, 'attr', None)) in ('Herdr', 'from_config')
               for n in ast.walk(tree))


class Rules(Resident):
    def test_second_pane_role_is_held_multi_director_parked(self):
        self.build()
        self.turns()
        self.configure('director:b')
        self.notifications.register('director:b', allowed_models=['opus-5.5'], token_budget=100)
        self.wake()
        self.wake('director:b')
        result = self.tick()
        self.assertEqual(len(result['launched']), 1)
        self.assertEqual(self.role('director:b')['held_reason'], 'multi-director-parked')
        self.assertEqual(len(self.calls('tab', 'create')), 1)
        self.assertEqual([r['role_id'] for r in self.runtime.active_runs()], [ROLE])
        self.tick()
        self.assertEqual(len(self.calls('tab', 'create')), 1)

    def test_stale_generation_gets_no_pane_write(self):
        self.launched()
        wake, handoff = self.hooks()
        self.panes.maintain(self.run_row)
        wake.assert_called_once_with(self.panes, self.run_row)
        handoff.assert_called_once_with(self.panes, self.run_row)
        wake.reset_mock(), handoff.reset_mock()
        self.notifications.issue_session(ROLE, 'owner-replacement')
        self.panes.maintain(self.run_row)
        wake.assert_not_called()
        handoff.assert_not_called()

    def test_lease_grants_no_pane_write(self):
        self.build()
        session = self.notifications.issue_session(ROLE, 'owner-session')
        self.notifications.acquire(ROLE, session['session_id'])
        wake, handoff = self.hooks()
        self.panes.maintain(dict(run_id=99, role_id=ROLE, generation=session['generation'], spec={}))
        wake.assert_not_called()
        handoff.assert_not_called()
        with self.assertRaises(PaneRefused) as caught:
            pane_send.send(self.port, self.herdr, None, 'hello', 'wake')
        self.assertEqual(caught.exception.reason, 'no-row')
        self.assertEqual([c for c in self.fake.calls if tuple(c[:2]) in WRITE_VERBS], [])

    def test_changed_occupant_stops_wakes_and_keeps_the_pane(self):
        self.launched()
        wake, handoff = self.hooks()
        self.fake.agents[PANE]['agent_session'] = {'value': 'someone-else'}
        for _ in range(2):
            self.assertEqual(self.adapter.probe(self.spec_), 'alive')
            self.panes.maintain(self.run_row)
        wake.assert_not_called()
        handoff.assert_not_called()
        self.assertEqual(self.row_now()['state'], 'live')
        self.assertIn(PANE, self.fake.panes)
        self.assertEqual(self.calls('pane', 'close'), [])
        self.assertEqual(len([n for n in self.notices() if n['id'].startswith('pane-occupant:')]), 1)

    def test_a_different_kind_of_agent_is_a_changed_occupant(self):
        self.launched()
        wake, _ = self.hooks()
        self.fake.agents[PANE]['agent'] = 'codex'
        self.panes.maintain(self.run_row)
        wake.assert_not_called()

    def test_herdr_restart_renames_by_pane_and_cwd(self):
        self.launched()
        self.fake.panes[PANE]['cwd'] = str(self.cwd).upper() + '\\'
        self.fake.restart()
        self.assertEqual(self.adapter.probe(self.spec_), 'alive')
        self.assertEqual(self.calls('agent', 'rename')[-1], ['agent', 'rename', PANE, self.row['name']])
        self.assertEqual(self.fake.agents[PANE]['name'], self.row['name'])
        before = len(self.calls('agent', 'rename'))
        self.adapter.probe(self.spec_)
        self.assertEqual(len(self.calls('agent', 'rename')), before)

    def test_a_name_is_not_restored_in_another_folder(self):
        self.launched()
        self.fake.panes[PANE]['cwd'] = 'C:\\somewhere\\else'
        self.fake.restart()
        before = len(self.calls('agent', 'rename'))
        self.assertEqual(self.adapter.probe(self.spec_), 'alive')
        self.assertEqual(len(self.calls('agent', 'rename')), before)

    def test_one_launch_at_a_time_and_a_stuck_launch_leaves_the_tick_running(self):
        self.build(boots=False)
        self.configure('director:b')
        self.notifications.register('director:b', allowed_models=['opus-5.5'], token_budget=100)
        self.fake.hang = {('agent', 'start')}
        self.wake()
        self.wake('director:b')
        result = self.tick()        # returning at all is the point: nothing here waits without a bound
        self.assertEqual(len(self.calls('tab', 'create')), 1)
        self.assertEqual(len(self.calls('agent', 'start')), 1)
        self.assertEqual(ops_panes.open_agents(self.conn)[0]['state'], 'failed')
        self.assertIn('multi-director-parked', str(result['held']))
        limit = pane_launch.SHELL_PROMPT_S + pane_launch.SHELL_BACK_S + pane_launch.STARTUP_S + pane_launch.FIRST_TURN_S
        self.assertLessEqual(self.sleep.call_count, limit)

    def test_only_the_hub_constructs_a_herdr_client(self):
        found = {path.relative_to(ROOT).as_posix() for top in ('drover', 'hub') for path in (ROOT / top).rglob('*.py')
                 if builds_a_client(ast.parse(path.read_text(encoding='utf-8')))}
        self.assertEqual(found, {'drover/herdr_client.py', 'hub/dispatch.py'})
        dispatch = ast.parse((ROOT / 'hub/dispatch.py').read_text(encoding='utf-8'))
        owners = [n.name for n in ast.walk(dispatch) if isinstance(n, ast.FunctionDef) and builds_a_client(n)]
        self.assertEqual(owners, ['_director_adapter'])

    def test_owner_started_pane_has_no_row_and_gets_no_input(self):
        self.launched()
        owner = 'w1:owner'
        self.fake.panes[owner] = dict(pane_id=owner, terminal_id='t8', focused=False)
        self.fake.agents[owner] = dict(pane_id=owner, terminal_id='t8', agent='claude', agent_status='idle',
                                       agent_session={'value': 'mine'}, focused=False)
        self.fake.screens[owner] = CLAUDE_IDLE
        self.fake.calls.clear()
        self.tick()
        self.panes.maintain(self.run_row)
        self.assertIsNone(ops_panes.by_pane(self.conn, owner))
        self.assertEqual([c for c in self.fake.calls if owner in c], [])


class Probe(Resident):
    def test_probe_is_unknown_when_herdr_is_closed(self):
        self.launched()
        self.fake.closed = True
        self.assertEqual(self.adapter.probe(self.spec_), 'unknown')
        self.assertEqual(self.row_now()['state'], 'live')

    def test_any_refusal_but_the_missing_pane_code_changes_no_row(self):
        self.launched()
        for code in ('agent_not_found', 'internal_error', 'other'):
            with self.subTest(code):
                self.fake.refuse[('pane', 'get')] = code
                self.assertEqual(self.adapter.probe(self.spec_), 'unknown')
                self.assertEqual(self.row_now()['state'], 'live')

    def test_a_reply_without_a_terminal_changes_no_row(self):
        self.launched()
        self.fake.panes[PANE] = dict(pane_id=PANE)
        self.assertEqual(self.adapter.probe(self.spec_), 'unknown')
        self.assertEqual(self.row_now()['state'], 'live')

    def test_a_missing_pane_is_gone_and_the_row_moves(self):
        self.launched()
        del self.fake.panes[PANE]
        self.assertEqual(self.adapter.probe(self.spec_), 'gone')
        self.assertEqual(self.row_now()['state'], 'gone')
        self.assertEqual(self.port.events(kind='pane-gone')[-1]['data']['pane_id'], PANE)

    def test_a_changed_terminal_is_gone_and_the_pane_is_no_longer_drovers(self):
        self.launched()
        self.fake.panes[PANE]['terminal_id'] = 't9'
        self.assertEqual(self.adapter.probe(self.spec_), 'gone')
        self.assertIsNone(ops_panes.by_pane(self.conn, PANE))
        self.assertEqual(self.calls('pane', 'close'), [])

    def test_a_row_in_compaction_is_alive_without_a_read(self):
        self.launched()
        ops_panes.update_agent(self.conn, self.row['id'], state='compaction')
        self.fake.closed = True
        self.fake.calls.clear()
        self.assertEqual(self.adapter.probe(self.spec_), 'alive')
        self.assertEqual(self.fake.calls, [])

    def test_an_agent_that_left_its_pane_changes_no_row_until_the_pane_closes(self):
        self.launched()
        del self.fake.agents[PANE]
        self.fake.processes = LONE_SHELL
        for _ in range(2):
            self.assertEqual(self.adapter.probe(self.spec_), 'unknown')
        self.assertEqual(self.row_now()['state'], 'live')
        self.assertEqual(len([n for n in self.notices() if n['id'] == f"pane-agent-left:{self.row['id']}"]), 1)
        del self.fake.panes[PANE]
        self.tick()
        self.assertEqual(self.outcome(), 'pane-gone')
        self.assertEqual(self.role()['replacements_used'], 0)

    def test_a_forgotten_agent_with_a_process_stays_alive(self):
        self.launched()
        del self.fake.agents[PANE]
        self.assertEqual(self.adapter.probe(self.spec_), 'alive')
        self.assertEqual(self.row_now()['state'], 'live')

    def test_a_lone_shell_with_an_agent_on_record_is_not_proof_it_left(self):
        self.launched()
        self.fake.processes = LONE_SHELL
        self.assertEqual(self.adapter.probe(self.spec_), 'alive')
        self.assertEqual(self.row_now()['state'], 'live')

    def test_any_other_refusal_of_the_agent_read_changes_no_row(self):
        self.launched()
        self.fake.refuse[('agent', 'get')] = 'internal_error'
        self.assertEqual(self.adapter.probe(self.spec_), 'unknown')
        self.assertEqual(self.row_now()['state'], 'live')

    def test_a_timed_out_agent_read_is_unknown(self):
        self.launched()
        self.fake.hang = {('agent', 'get')}
        self.assertEqual(self.adapter.probe(self.spec_), 'unknown')
        self.assertEqual(self.row_now()['state'], 'live')

    def test_a_retired_row_is_gone_though_an_agent_still_shows(self):
        self.launched()
        ops_panes.update_agent(self.conn, self.row['id'], state='retired')
        self.assertEqual(self.adapter.probe(self.spec_), 'gone')

    def test_a_run_with_no_row_is_gone(self):
        self.build()
        self.assertEqual(self.adapter.probe(dict(run_id=404)), 'gone')

    def test_the_newest_row_of_a_run_speaks_for_it(self):
        self.launched()
        fresh = ops_panes.record_agent(self.conn, **{k: self.row[k] for k in (
            'pane_id', 'terminal_id', 'kind', 'seat', 'placement', 'started_by', 'role_id', 'run_id', 'cwd', 'name')})
        self.assertEqual(ops_panes.by_run(self.conn, self.rid)['id'], fresh['id'])
        ops_panes.update_agent(self.conn, self.row['id'], state='gone')
        self.assertEqual(ops_panes.by_run(self.conn, self.rid)['id'], fresh['id'])
        ops_panes.update_agent(self.conn, fresh['id'], state='gone')
        self.assertIsNone(ops_panes.by_run(self.conn, self.rid))


class Handle(Resident):
    def test_poll_is_none_while_the_agent_lives(self):
        self.launched()
        self.assertIsNone(self.adapter.adopt(self.spec_).poll())
        ops_panes.update_agent(self.conn, self.row['id'], state='compaction')
        self.assertIsNone(self.adapter.adopt(self.spec_).poll())

    def test_poll_says_how_the_row_ended(self):
        self.launched()
        handle = self.adapter.adopt(self.spec_)
        ops_panes.update_agent(self.conn, self.row['id'], state='failed')
        self.assertEqual(handle.poll(), dict(status='launch-failed', usage={}))
        ops_panes.update_agent(self.conn, self.row['id'], state='retired')
        self.assertEqual(handle.poll(), dict(status='pane-gone', usage={}))
        ops_panes.update_agent(self.conn, self.row['id'], state='gone')
        self.assertEqual(handle.poll(), dict(status='pane-gone', usage={}))

    def test_a_finished_session_is_never_resumed_here(self):
        self.launched()
        self.assertIs(self.adapter.verify_resume(self.spec_), False)

    def test_capabilities_are_as_declared(self):
        self.build()
        self.assertEqual(self.adapter.capabilities, dict(resident=True, live_offer=True, verified_resume=True, fresh_start=True,
                                                         enforced_token_cap=False, authenticated_processing=True,
                                                         secure_handoff=True))
        self.assertEqual(self.adapter.session_ttl, 86400)

    def test_stop_is_one_interrupt_to_the_pane(self):
        self.launched()
        self.fake.screens[PANE], self.fake.agents[PANE]['agent_status'] = CLAUDE_IDLE, 'idle'
        self.assertIs(self.adapter.stop(self.spec_), True)
        self.assertEqual(self.calls('pane', 'send-keys'), [['pane', 'send-keys', PANE, 'ctrl+c']])

    def test_stop_that_is_refused_is_not_a_request(self):
        self.launched()
        self.fake.closed = True
        self.assertIs(self.adapter.stop(self.spec_), False)
        self.assertIs(self.adapter.stop(dict(run_id=404)), False)

    def test_the_compaction_hook_reports_nothing_to_do_yet(self):
        self.build()
        self.assertEqual(self.panes.compaction_tick(dict(id='h1', phase='stop')),
                         dict(id='h1', phase='stop', settled=True, agents=[]))

    def test_the_task_11_and_12_hooks_are_empty(self):
        self.assertIsNone(pane_wake.deliver(None, None))
        self.assertIsNone(pane_handoff.maintain(None, None))


if __name__ == '__main__':
    unittest.main()
