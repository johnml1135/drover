"""Durable replan requests, bounded records, graph replacement and dispatch."""
import copy
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from drover import brief, config, contract, launch, pipeline, repos
from drover.ledger import connect, db, ops, states
from drover.port import DirectPort
from tests.test_waiting import LedgerCase, logical, require
from tests.test_pipeline import result
from tests.rcw_fixture import PipelineBase
from tests.tiers import integration

from tests.test_replans import ROUTE, REVISE, OWNER, SPLIT, inspected, PlannerDispatchCase, time_now


class Records(PipelineBase):
    def test_parser_bounds_and_brief(self):
        parse = require(self, 'drover.contract', 'parse_replan_record')
        assemble = lambda task, runs, notes, inputs: require(self, 'drover.brief', 'assemble_replans')([
            dict(task=task, runs=runs, notes=notes, trigger=None, inputs=inputs)])
        for record in (REVISE, OWNER, SPLIT):
            self.assertEqual(parse('Planner observations\n' + json.dumps(record) + '\n'), record)
        invalid = [
            {}, {'action': 'delete'}, {**REVISE, 'branch': 'main'}, {**OWNER, 'question': ''},
            {**OWNER, 'question': 'x' * 2001}, {**REVISE, 'goal': 'x' * 12001},
            {**REVISE, 'acceptance': ['a'] * 31}, {**REVISE, 'acceptance': ['a' * 1001]},
            {**REVISE, 'scope_files': ['../secret']}, {**REVISE, 'scope_files': ['/etc/passwd']},
            {**REVISE, 'scope_files': ['C:\\owner']}, {**REVISE, 'scope_files': ['x' * 241]},
            {**SPLIT, 'tasks': SPLIT['tasks'][:1]}, {**SPLIT, 'tasks': SPLIT['tasks'] * 5},
            {**SPLIT, 'tasks': [SPLIT['tasks'][0], {**SPLIT['tasks'][1], 'key': 'a'}]},
            {**SPLIT, 'tasks': [{**SPLIT['tasks'][0], 'deps': ['a']}, SPLIT['tasks'][1]]},
            {**SPLIT, 'tasks': [{**SPLIT['tasks'][0], 'deps': ['unknown']}, SPLIT['tasks'][1]]},
            {**SPLIT, 'tasks': [SPLIT['tasks'][0], {**SPLIT['tasks'][1], 'deps': ['a', 'a']}]},
            {**SPLIT, 'tasks': [{**SPLIT['tasks'][0], 'key': 'Upper'}, SPLIT['tasks'][1]]},
            {'nested': OWNER},
        ]
        for record in invalid:
            with self.subTest(record=repr(record)[:150]):
                self.assertIsNone(parse(json.dumps(record)))
        for text in (json.dumps(OWNER) + '\ntrailing text', '{bad json',
                     json.dumps(OWNER) + json.dumps(OWNER), json.dumps({**OWNER, 'question': 'é' * 20000})):
            self.assertIsNone(parse(text))
        self.assertEqual(parse('x' * 40000 + '\n' + json.dumps(OWNER)), OWNER)
        task = self.get(self.task(goal='MANDATORY GOAL', acceptance=['MANDATORY ACCEPTANCE'], scope_files=['src/*']))
        inputs = {'repo': 'p', 'branch': 'topic', 'head': 'a' * 40, 'dirty_digest': 'b' * 64,
                  'checkout': str(self.fix.repo), 'read_checkouts': []}
        built = assemble(task, [{'id': n, 'note': 'history' * 1000, 'gate_receipt': None} for n in range(50)],
                         ['finding' * 1000] * 50, inputs)
        prompt = built['user_prompt']
        self.assertIn('MANDATORY GOAL', prompt)
        self.assertIn('MANDATORY ACCEPTANCE', prompt)
        self.assertIn('topic', prompt)
        self.assertIn('a' * 40, prompt)
        self.assertIn('read-only', (built['system_append'] + prompt).lower())
        self.assertIn('needs-owner', prompt)
        self.assertLessEqual(built['tokens'], 15000)
        with self.assertRaises(brief.BriefTooLarge):
            assemble({**task, 'goal': 'required spec ' * 100000}, [], [], inputs)

    @integration
    def test_inputs_detect_dirty_and_branch_changes(self):
        method = require(self, 'drover.replan', 'Replanning')
        self.assertTrue(callable(method), 'Replanning interface is absent')
        cfg = config.validate({'paths': {'state_dir': str(Path(self.tmp.name) / 'state')}})
        config.use(cfg)
        self.addCleanup(config.reset)
        tid = self.task()
        checkout = Path(self.pipe.worktree(self.get(tid)))
        initial = inspected(self, self.get(tid))
        self.assertEqual(initial['head'], self.fix.git('rev-parse', 'HEAD', cwd=checkout))
        head = initial['head']
        digests = [initial['dirty_digest']]
        for filename, content, staged in [('base.txt', 'changed', False), ('base.txt', 'changed twice', False),
                                          ('base.txt', 'staged now', True), ('new.txt', 'one', False),
                                          ('new.txt', 'two', False)]:
            (checkout / filename).write_text(content)
            if staged:
                self.fix.git('add', filename, cwd=checkout)
            current = inspected(self, self.get(tid))
            self.assertEqual(current['head'], head)
            self.assertNotEqual(current['dirty_digest'], digests[-1])
            digests.append(current['dirty_digest'])
        outside = Path(self.tmp.name) / 'outside'
        outside.write_text('owner bytes')
        link = checkout / 'link'
        try:
            link.symlink_to(outside)
        except OSError as exc:
            # Git on Windows can still exercise tracked/index/untracked digests above.
            self.assertIn(exc.errno, (1, 13, 1314))
        else:
            symlink = inspected(self, self.get(tid))
            outside.write_text('changed outside checkout')
            self.assertEqual(inspected(self, self.get(tid))['dirty_digest'], symlink['dirty_digest'])
            link.unlink()
            link.symlink_to(Path(self.tmp.name) / 'another')
            self.assertNotEqual(inspected(self, self.get(tid))['dirty_digest'], symlink['dirty_digest'])
        owner_head = self.fix.git('rev-parse', 'HEAD')
        owner_index = self.fix.git('ls-files', '--stage')
        self.fix.commit('new-head.txt', cwd=checkout)
        self.assertNotEqual(inspected(self, self.get(tid))['head'], head)
        read_repo = self.fix.repo
        read_task = self.task()
        before = inspected(self, self.get(read_task))
        self.fix.commit('read-moved.txt')
        after = inspected(self, self.get(read_task))
        self.assertNotEqual(before['read_checkouts'], after['read_checkouts'])
        owner_head = self.fix.git('rev-parse', 'HEAD')
        owner_index = self.fix.git('ls-files', '--stage')
        owner_status = self.fix.git('status', '--porcelain')
        missing = {**self.get(tid), 'worktree': str(Path(self.tmp.name) / 'missing')}
        fallback = inspected(self, missing)
        self.assertNotEqual(fallback['checkout'], str(read_repo))
        self.assertEqual(self.fix.git('rev-parse', 'HEAD'), owner_head)
        self.assertEqual(self.fix.git('ls-files', '--stage'), owner_index)
        self.assertEqual(self.fix.git('status', '--porcelain'), owner_status)

        # Same commit, different branch must still invalidate the planner input.
        branch_tid = self.task()
        checkout = self.pipe.worktree(self.get(branch_tid))
        branch_task = self.get(branch_tid)
        before = inspected(self, branch_task)
        self.fix.git('switch', '-c', 'different-branch', cwd=checkout)
        after = inspected(self, branch_task)
        self.assertEqual(before['head'], after['head'])
        self.assertNotEqual(before['branch'], after['branch'])
