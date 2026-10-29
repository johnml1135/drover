"""Worker continuation decisions, budgets, prompts and same-session resumes."""
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from tests.test_dispatcher import Base, DONE, res
from drover import brief, decide, placement, runner, task_context
from drover.ledger import ops


class WorkerContinuationTests(Base):
    def setUp(self):
        super().setUp()
        self.allow['codex'] = False
        self.states = []
        self.choice = 'option_2'
        self.d.decide_fn = self.decider

    def decider(self, name, state, rule_answer=None, **kw):
        if name != 'worker_continue':
            return super().decide(name, state, rule_answer)
        state = json.loads(state)
        criteria = json.loads(Path(kw['criteria_path']).read_text())['questions'][name]
        self.assertEqual(criteria['mode'], 'live')
        answer = self.choice if self.choice in criteria['criteria'] else rule_answer
        self.states.append(state)
        return {'acted': True, 'answer': answer, 'model': 'fake-decider', 'confidence': 1}

    def stop(self, index, kind='plan', detail='Should I use A or B?', status='blocked', session=True):
        record = {'status': status, 'sha': '', 'commit_message': '',
                  'blocker': {'kind': kind, 'detail': detail}, 'note': ''}
        result = res(status, json.dumps(record))
        if session:
            # Exercise real initialization-event parsing rather than the fake result shortcut.
            spec = self.fl.specs[index]
            directory = runner.run_dir(self.pipe.runs_dir, spec['run_id'])
            directory.mkdir(parents=True, exist_ok=True)
            event = ({'type': 'thread.started', 'thread_id': 'same-session'} if spec['backend'] == 'codex'
                     else {'type': 'system', 'subtype': 'init', 'session_id': 'same-session'})
            (directory / runner.OUT_FILE).write_text(json.dumps(event) + '\n', encoding='utf-8')
        self.finish(index, result)
        self.d.reap()

    def test_same_session_decision_then_success_without_owner(self):
        tid = self.task(acceptance=['Choose B and verify the change'])
        self.d.tick()
        self.stop(0)
        self.assertEqual(self.status(tid), 'ready')
        decision = self.events('worker-decision')[0]['data']
        self.assertEqual(decision['decision'], 'Use B.')
        self.assertIsInstance(decision['decision_id'], int)
        self.assertEqual(self.states[0]['task']['acceptance'], ['Choose B and verify the change'])
        self.d = self.make()  # A hub restart must retain route, session, budget and prompt context.
        self.d.decide_fn = self.decider
        self.d.tick()
        first, resumed = self.fl.specs
        self.assertEqual(resumed['run_config']['_drover_continue'],
                         {'backend': first['backend'], 'session': 'same-session'})
        for field in ('backend', 'seat', 'model', 'effort', 'placement', 'cwd'):
            self.assertEqual(resumed[field], first[field])
        prompt = resumed['system_append'] + resumed['user_prompt']
        self.assertIn('Use B.', prompt)
        self.assertIn(task_context.KEEP_GOING, prompt)
        # Fake worker completes only after receiving the resolved choice and verification mandate.
        self.finish(1, res('done', DONE))
        self.d.reap()
        self.assertEqual(self.port.list_runs(tid)[-1]['outcome'], 'done')
        self.assertEqual(ops.open_questions(self.conn), [])

    def test_codex_same_session_continuation(self):
        self.allow.update(codex=True, claude=False)
        self.test_same_session_decision_then_success_without_owner()

    def test_owner_choice_asks_once_without_resuming_even_if_model_disagrees(self):
        tid = self.task()
        self.d.tick()
        self.stop(0, 'new_dependency', 'May I install package X?')
        for _ in range(3):
            self.d.tick()
        self.assertEqual(self.status(tid), 'needs-owner')
        self.assertEqual(len(self.fl.specs), 1)
        questions = ops.open_questions(self.conn)
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0]['title'], 'May I install package X?')
        # Repeating a finish after its transaction committed must not duplicate the question.
        spec = self.fl.specs[0]
        self.d._continue_worker(tid, spec['run_id'], spec, self.fl.handles[0].result,
                                {'tokens_in': 10, 'tokens_out': 5, 'tokens_cached': 1})
        self.assertEqual(len(self.events('question-asked')), 1)
        self.assertEqual(len(self.events('worker-decision')), 1)

    def test_configurable_exhaustion_preserves_history_across_retry_markers(self):
        self.port.set_setting(task_context.CONTINUE_SETTING, '2')
        tid = self.task()
        self.d.tick()
        for index in range(3):
            self.stop(index)
            if index < 2:
                request = self.port.waiting.request_replan(tid, 'blocker')
                self.port.waiting._request_done(request['id'], 'revised')
                self.assertEqual(self.port.get_task(tid)['retry_after_run'],
                                 self.fl.specs[index]['run_id'])
                self.d.tick()
        for _ in range(3):
            self.d.tick()
        self.assertEqual(len(self.fl.specs), 3)
        self.assertEqual(len(self.states), 2)
        questions = ops.open_questions(self.conn)
        self.assertEqual(len(questions), 1)
        self.assertIn('budget exhausted', questions[0]['title'])
        history = json.loads(questions[0]['body'].split('\n', 1)[1])
        self.assertEqual([h['attempt'] for h in history], [1, 2, 3])
        self.assertEqual([h['decision'] for h in history[:2]], ['Use B.', 'Use B.'])
        self.assertEqual(len(self.events('worker-decision')), 3)

    def test_owner_answer_is_durable_and_reaches_resumed_worker_and_decider(self):
        tid = self.task()
        self.d.tick()
        self.stop(0, 'owner_only', 'Which interface is approved?')
        question = ops.open_questions(self.conn)[0]
        ops.answer_question(self.conn, question['id'], text='Use interface B; preserve the public API.')
        self.d = self.make()
        self.d.decide_fn = self.decider
        self.d.tick()
        self.assertEqual(len(self.fl.specs), 2)
        resumed = self.fl.specs[1]
        self.assertEqual(resumed['run_config']['_drover_continue']['session'], 'same-session')
        self.assertIn('Use interface B; preserve the public API.', resumed['system_append'] + resumed['user_prompt'])
        self.assertEqual(len(self.events('worker-owner-decision')), 1)
        self.d._continuation_history(self.port.get_task(tid))
        self.assertEqual(len(self.events('worker-owner-decision')), 1)
        self.stop(1, detail='Targeted checks are still failing.')
        history = self.states[-1]['history']
        self.assertEqual(history[0]['answer'], 'owner')
        self.assertEqual(history[1]['decision'], 'Use interface B; preserve the public API.')

    def test_zero_budget_and_unverified_stops_are_bounded(self):
        self.port.set_setting(task_context.CONTINUE_SETTING, '0')
        tid = self.task()
        self.d.tick()
        with patch.object(task_context, 'repository_rules', side_effect=AssertionError('budget already spent')):
            self.stop(0, status='no-record')
        self.d.tick()
        self.assertEqual(self.status(tid), 'needs-owner')
        self.assertEqual(len(ops.open_questions(self.conn)), 1)
        self.assertEqual(self.states, [])
        self.assertEqual(len(self.fl.specs), 1)

    def test_unverified_worker_is_resumed_to_verify(self):
        tid = self.task()
        self.d.tick()
        self.stop(0, detail='Targeted checks have not run.', status='no-record')
        self.d.tick()
        self.assertEqual(len(self.fl.specs), 2)
        self.assertEqual(self.events('worker-decision')[0]['data']['answer'], 'verify')
        self.assertEqual(ops.open_questions(self.conn), [])
        self.assertEqual(self.status(tid), 'running')

    def test_initial_worker_brief_requires_continuing_until_verified(self):
        self.task()
        self.d.tick()
        self.assertEqual(len(self.fl.specs), 1)
        self.assertIn(task_context.worker_instructions(), self.fl.specs[0]['user_prompt'])
        self.assertIn(task_context.KEEP_GOING, self.fl.specs[0]['user_prompt'])

    def test_optional_notes_trim_before_mandatory_instructions(self):
        tid = self.task(size='S')
        ops.add_comment(self.conn, 'task', tid, 'owner', 'x' * 30000)
        self.d.tick()
        self.assertEqual(len(self.fl.specs), 1)
        spec = self.fl.specs[0]
        self.assertIn(task_context.KEEP_GOING, spec['system_append'] + spec['user_prompt'])
        self.assertLessEqual(brief.estimate_tokens(spec['system_append'] + spec['user_prompt']),
                             brief.BUDGET_TOKENS['S'])
        self.stop(0)
        self.d.tick()
        self.assertEqual(len(self.fl.specs), 2)
        self.assertIn('Use B.', self.fl.specs[1]['user_prompt'])

    def test_model_selects_concrete_choice_through_real_decider_client(self):
        choices = task_context.continuation_choices({'detail': 'Should I use A or B?'})
        criteria = Path(self.tmp.name) / 'criteria.json'
        criteria.write_text(json.dumps(task_context.continuation_criteria(choices)))
        response = {'answers': {'worker_continue': {'choice': 'option_2', 'confidence': 1}}}
        with patch.object(decide, 'call_systemone', return_value=response) as call:
            result = decide.decide('worker_continue', '{}', criteria_path=criteria,
                                   device='gpu', base='http://fake-decider', recorder=lambda r: None)
        self.assertTrue(result['acted'])
        self.assertEqual(choices[result['answer']], 'Use B.')
        self.assertEqual(call.call_args.args[1]['questions']['worker_continue']['criteria']['option_2'],
                         'Use B.')

    def test_concrete_alternatives_and_routine_fallback(self):
        for detail in ('Should I use A or B?', 'Whether to use A or B?', 'A or B?',
                       'Should the cache be per-task or global?'):
            with self.subTest(detail=detail):
                choices = task_context.continuation_choices({'detail': detail})
                self.assertEqual(list(choices), ['option_1', 'option_2'])
                self.assertNotIn(' or ', choices['option_2'])
        self.assertEqual(task_context.continuation_choices({'detail': 'Checks failed.'}),
                         task_context.CONTINUE_DECISIONS)

    def test_runner_resume_argv_preserves_flags_and_targets_only_named_session(self):
        codex = ['codex', 'exec', '--json', '-m', 'gpt-6.1-terra', '-s', 'workspace-write', '-']
        self.assertEqual(runner.resume_argv(codex, 'codex', 'same-session'),
                         [*codex[:-1], 'resume', 'same-session', '-'])
        claude = ['claude', '-p', '--model', 'sonnet', '--output-format', 'stream-json']
        self.assertEqual(runner.resume_argv(claude, 'claude', 'same-session'),
                         [*claude, '--resume', 'same-session'])
        for invalid in ('--latest', '../other-session', '', None):
            with self.subTest(session=invalid), self.assertRaises(ValueError):
                runner.resume_argv(codex, 'codex', invalid)

    def test_repository_rules_read_on_owning_placement(self):
        root = Path(self.tmp.name)
        (root / 'AGENTS.md').write_text('Root rules', encoding='utf-8')
        (root / 'src').mkdir()
        (root / 'src' / 'CLAUDE.md').write_text('Scoped rules', encoding='utf-8')
        task = {'scope_files': ['src/file.py:1-20', '../outside/file']}
        self.assertEqual(task_context.repository_rules(task, root),
                         {'AGENTS.md': 'Root rules', 'src/CLAUDE.md': 'Scoped rules'})
        with patch.object(placement, 'routes_through_wsl', return_value=True), \
                patch.object(placement.WSL, 'command', return_value=['owning-wsl-reader']) as command, \
                patch.object(task_context.subprocess, 'run', return_value=subprocess.CompletedProcess(
                    [], 0, '{"AGENTS.md": "WSL rules"}', '')) as execute:
            self.assertEqual(task_context.repository_rules(task, '/home/worker/repo'),
                             {'AGENTS.md': 'WSL rules'})
            self.assertEqual(command.call_args.args[0][-1], '/home/worker/repo')
            self.assertEqual(execute.call_args.args[0], ['owning-wsl-reader'])
            self.assertEqual(json.loads(execute.call_args.kwargs['input']), task['scope_files'])
