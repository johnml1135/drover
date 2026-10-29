"""Owner recording of implementations landed outside Drover."""
from types import SimpleNamespace
from unittest import mock

from drover.ledger import ops
from tests.test_ledger import Base


SHA = 'a' * 40
BASE = 'b' * 40
PROG = {'name': 'p', 'repo': '/repo', 'default_branch': 'main', 'remote': 'origin',
        'landing': {'push': True}}


class OutsideLandingTests(Base):
    def setUp(self):
        super().setUp()
        self.repo = mock.patch('drover.repos.load_all', return_value={'p': PROG})
        self.git = mock.patch('drover.placement.git', side_effect=self.fake_git)
        self.repo.start()
        self.git.start()
        self.addCleanup(self.repo.stop)
        self.addCleanup(self.git.stop)

    def fake_git(self, repo, *args, **kwargs):
        self.assertEqual(repo, '/repo')
        if args[0] == 'cat-file':
            self.assertEqual(args, ('cat-file', '-t', SHA))
            return SimpleNamespace(returncode=0, stdout='commit\n')
        if args[0] == 'rev-parse':
            self.assertEqual(args, ('rev-parse', '--verify', '--quiet', 'refs/remotes/origin/main^{commit}'))
            return SimpleNamespace(returncode=0, stdout=BASE + '\n')
        if args[0] == 'merge-base':
            self.assertEqual(args, ('merge-base', '--is-ancestor', SHA, BASE))
            return SimpleNamespace(returncode=0, stdout='')
        raise AssertionError(args)

    def needs_owner(self):
        t = self.task()
        ops.set_status(self.conn, t['id'], 'needs-owner', 'worker', reason='landed elsewhere')
        return t['id']

    def test_owner_records_external_landing_in_durable_event(self):
        tid = self.needs_owner()
        result = ops.record_external_landing(self.conn, tid, SHA, 'origin/main', 'gate passed', 'review approved')
        self.assertEqual(result['status'], 'landed')
        self.conn.close()
        from drover.ledger import connect
        self.conn = connect(self.path)
        self.conns.append(self.conn)
        event = ops.events(self.conn, task_id=tid, kind='status')[-1]
        self.assertEqual(event['actor'], 'owner')
        self.assertEqual(event['data']['external_landing'], {
            'sha': SHA, 'base_ref': 'origin/main', 'base_sha': BASE,
            'gate': 'gate passed', 'review': 'review approved'})

    def test_invalid_evidence_and_non_owner_leave_task_unchanged(self):
        tid = self.needs_owner()
        before = ops.get_task(self.conn, tid), ops.events(self.conn, task_id=tid)
        cases = [
            (dict(sha=SHA[:8]), ValueError),
            (dict(sha='g' * 40), ValueError),
            (dict(base_ref='other/branch'), ValueError),
            (dict(gate=''), ValueError),
            (dict(review='  '), ValueError),
            (dict(actor='worker'), PermissionError),
        ]
        for changes, error in cases:
            with self.subTest(changes=changes):
                args = dict(sha=SHA, base_ref='origin/main', gate='gate passed', review='review approved')
                args.update(changes)
                with self.assertRaises(error):
                    ops.record_external_landing(self.conn, tid, **args)
                self.assertEqual((ops.get_task(self.conn, tid), ops.events(self.conn, task_id=tid)), before)
        with mock.patch('drover.placement.git', side_effect=lambda repo, *args, **kw:
                        SimpleNamespace(returncode=1 if args[0] == 'merge-base' else 0,
                                        stdout='commit' if args[0] == 'cat-file' else BASE)):
            with self.assertRaises(ValueError):
                ops.record_external_landing(self.conn, tid, SHA, 'origin/main', 'gate passed', 'review approved')
        self.assertEqual((ops.get_task(self.conn, tid), ops.events(self.conn, task_id=tid)), before)

    def test_repeat_is_idempotent_and_conflicting_record_refused(self):
        tid = self.needs_owner()
        args = (self.conn, tid, SHA, 'origin/main', 'gate passed', 'review approved')
        ops.record_external_landing(*args)
        before = ops.get_task(self.conn, tid), ops.events(self.conn, task_id=tid)
        self.assertEqual(ops.record_external_landing(*args), before[0])
        with self.assertRaises(ops.IllegalTransition):
            ops.record_external_landing(self.conn, tid, 'c' * 40, 'origin/main', 'gate passed', 'review approved')
        self.assertEqual((ops.get_task(self.conn, tid), ops.events(self.conn, task_id=tid)), before)

    def test_external_landing_replaces_cancel_workaround(self):
        tid = self.needs_owner()
        with self.assertRaises(ops.IllegalTransition):
            ops.set_status(self.conn, tid, 'landed', 'owner', reason='landed in ' + SHA)
        ops.record_external_landing(self.conn, tid, SHA, 'origin/main', 'gate passed', 'review approved')
        self.assertEqual(self.status(tid), 'landed')
        with self.assertRaises(ops.IllegalTransition):
            ops.set_status(self.conn, tid, 'cancelled', 'owner', reason='landed in ' + SHA)
        self.assertEqual(self.status(tid), 'landed')

    def test_landing_receipt_supports_existing_merged_result_consumers(self):
        from drover.ledger.landing import land_sha
        from drover.notifications.groups import accepted_result
        tid = self.needs_owner()
        ops.record_external_landing(self.conn, tid, SHA, 'origin/main', 'gate passed', 'review approved')
        self.assertEqual(land_sha(self.conn, tid), SHA)
        result = accepted_result(self.conn, tid, 'legacy-merged')
        self.assertEqual((result['commit'], result['tip']), (SHA, BASE))

    def test_uppercase_full_sha_is_canonicalized_for_repeat(self):
        tid = self.needs_owner()
        ops.record_external_landing(self.conn, tid, SHA.upper(), 'origin/main', 'gate passed', 'review approved')
        before = ops.events(self.conn, task_id=tid)
        ops.record_external_landing(self.conn, tid, SHA, 'origin/main', 'gate passed', 'review approved')
        self.assertEqual(ops.events(self.conn, task_id=tid), before)
        self.assertEqual(before[-2]['data']['external_landing']['sha'], SHA)

    def test_unverifiable_git_results_and_wrong_state_do_not_mutate(self):
        tid = self.needs_owner()
        before = ops.get_task(self.conn, tid), ops.events(self.conn, task_id=tid)
        for command in ('cat-file', 'rev-parse', 'merge-base'):
            def git(repo, *args, **kwargs):
                if args[0] == command:
                    return SimpleNamespace(returncode=2, stdout='')
                return self.fake_git(repo, *args, **kwargs)
            with self.subTest(command=command), mock.patch('drover.placement.git', side_effect=git):
                with self.assertRaises(ValueError):
                    ops.record_external_landing(self.conn, tid, SHA, 'origin/main', 'gate passed', 'review approved')
                self.assertEqual((ops.get_task(self.conn, tid), ops.events(self.conn, task_id=tid)), before)
        other = self.task()
        before = ops.get_task(self.conn, other['id']), ops.events(self.conn, task_id=other['id'])
        with self.assertRaises(ops.IllegalTransition):
            ops.record_external_landing(self.conn, other['id'], SHA, 'origin/main', 'gate passed', 'review approved')
        self.assertEqual((ops.get_task(self.conn, other['id']), ops.events(self.conn, task_id=other['id'])), before)

    def test_local_repository_uses_configured_branch(self):
        tid = self.needs_owner()
        local = {**PROG, 'landing': {'push': False}}
        def git(repo, *args, **kwargs):
            if args[0] == 'rev-parse':
                self.assertEqual(args[-1], 'refs/heads/main^{commit}')
                return SimpleNamespace(returncode=0, stdout=BASE)
            return self.fake_git(repo, *args, **kwargs)
        with mock.patch('drover.repos.load_all', return_value={'p': local}), mock.patch('drover.placement.git', side_effect=git):
            ops.record_external_landing(self.conn, tid, SHA, 'main', 'gate passed', 'review approved')
        self.assertEqual(self.status(tid), 'landed')
