"""One stage run's evidence, held once: the inventory, the listings with the stage clock's patience, and the process
proof with its report rows."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from drover import disk_authority, disk_evidence, disk_inventory, disk_worktrees, git_client, git_worktrees, ownership, placement, \
    placement_paths
from drover.disk_evidence import FreshProcesses, FreshRegistrations, StageEvidence
from drover.disk_inventory import Inventory
from drover.disk_stage import StageClock, StageRequest
from drover.git_worktrees import Listing, Proof, Refusal, RegistrationsUnknown
from tests.disk_fixture import listing_answers, staged
from tests.fakeci import git_answers
from tests.gitfix import clean_parent


def request(**changes):
    value = dict(stage=4, side='wsl', mode=None, apply=False, now=100.0, drive=dict(path='/mnt/h'),
                 repos=(dict(repo='/r/one'), dict(repo='/r/two')), policy=dict(worker_timeout_s=1800),
                 state_dir='/state', tasks={}, inventory_id='inv')
    value.update(changes)
    return StageRequest(**value)


def clock_at(times):
    """A StageClock started at times[0]; the test moves time by appending."""
    return StageClock(1800, clock=lambda: times[-1])


class Listings(unittest.TestCase):
    def test_listings_take_patience_when_each_listing_starts(self):
        """3M3: worker timeout 1800 and the listing's 400 s cap: 240 s at 1200 s elapsed, 10 s at the 80% line."""
        times, asked = [0.0], []

        def listing(repo, *, timeout_s, side=None):
            asked.append((repo, timeout_s))
            return Listing(repo, ())

        evidence = StageEvidence(request(), clock_at(times), processes=None)
        with patch.object(git_worktrees, 'listing', listing):
            times.append(1200.0)
            evidence.listed('/r/one')
            times.append(1440.0)
            evidence.listed('/r/two')
            evidence.listed('/r/one')                        # listed once: its first patience stands
        self.assertEqual(asked, [('/r/one', 240.0), ('/r/two', 10.0)])

    def test_listing_failure_is_raised_from_listed(self):
        def listing(repo, **_):
            if repo == '/r/two':
                raise RegistrationsUnknown('fatal: not a git repository')
            return Listing(repo, (dict(worktree='/w/a'),))

        evidence = StageEvidence(request(), clock_at([0.0]), processes=None)
        with patch.object(git_worktrees, 'listing', listing):
            self.assertEqual(evidence.row('/r/one', '/w/a'), dict(worktree='/w/a'))
            with self.assertRaisesRegex(RegistrationsUnknown, 'not a git repository'):
                evidence.listed('/r/two')
            with self.assertRaises(RegistrationsUnknown):
                evidence.every_registered()

    def test_took_and_repaired_reach_the_one_listing(self):
        evidence = StageEvidence(request(), clock_at([0.0]), processes=None)
        with patch.object(git_worktrees, 'listing', lambda repo, **_: Listing(repo, (dict(worktree=repo + '/w'),))):
            self.assertEqual(evidence.every_registered(),
                             tuple(placement_paths.cleanup_parts(p) for p in ('/r/one/w', '/r/two/w')))
            evidence.took('/r/one', '/r/one/w')
            evidence.repaired('/r/two', '/r/two/p')
            self.assertIsNone(evidence.row('/r/one', '/r/one/w'))
            self.assertEqual(len(evidence.listed('/r/one').rows), 0)


class Processes(unittest.TestCase):
    def test_processes_is_one_snapshot_and_process_rows_match_disk_side_21_32(self):
        """The rows today's disk_side.serve appended after compact (disk_side.py:21-32 at e09ef3e)."""
        calls = []
        unreadable = [dict(pid=7, name='a', side='wsl'), dict(pid=8, name='b', side='wsl')]
        failure = dict(reason_code='process_query_failed', side='windows', error='timed out')

        def processes(**options):
            calls.append(options)
            return SimpleNamespace(out_of_scope=3, unreadable=unreadable, query_failure=failure)

        evidence = StageEvidence(request(budget=dict(target_gb=5)), clock_at([0.0]), processes=processes)
        self.assertEqual(evidence.process_rows(), [])                 # no proof taken, no rows
        proof = evidence.processes()
        self.assertIs(evidence.processes(), proof)
        self.assertEqual(calls, [dict(paths=dict(worker_timeout_s=1800), state_dir='/state', inventory_id='inv',
                                      stage=4, drive=dict(path='/mnt/h'), budget=True)])
        self.assertEqual(evidence.process_rows(), [
            dict(action='report', reason_code='process_out_of_scope', count=3),
            dict(action='report', reason_code='process_visibility_unavailable', count=2, processes=unreadable),
            dict(action='report', **failure)])

    def test_a_fresh_proofs_rows_reach_process_rows_once_each_by_pid_and_name(self):
        """A proof taken at removal (`fresh_processes`, always a new snapshot) reports through the evidence: its query
        failure is a row, and an unreadable process two proofs saw is counted once, keyed by (pid, name)."""
        failure = dict(reason_code='process_query_failed', side='windows', error='timed out')
        first = SimpleNamespace(unreadable=[dict(pid=7, name='a'), dict(pid=8, name='a')])
        fresh = SimpleNamespace(unreadable=[dict(pid=7, name='a', seen='again'), dict(pid=7, name='b')],
                                query_failure=failure)
        proofs = iter([first, fresh])
        evidence = StageEvidence(request(), clock_at([0.0]), processes=lambda **_: next(proofs))
        self.assertIs(evidence.processes(), first)
        self.assertIs(evidence.fresh_processes().proof, fresh)
        self.assertIs(evidence.processes(), first)          # the first pass's proof stands
        self.assertEqual(evidence.process_rows(), [
            dict(action='report', reason_code='process_visibility_unavailable', count=3,
                 processes=[dict(pid=7, name='a', seen='again'), dict(pid=8, name='a'), dict(pid=7, name='b')]),
            dict(action='report', **failure)])

    def test_a_proof_with_nothing_to_say_adds_no_rows(self):
        evidence = StageEvidence(request(), clock_at([0.0]), processes=lambda **_: SimpleNamespace(busy=lambda p: False))
        evidence.processes()
        self.assertEqual(evidence.process_rows(), [])

    def test_fresh_processes_is_always_a_new_snapshot(self):
        proofs = [SimpleNamespace(name=n) for n in ('first', 'second', 'third')]
        asked = iter(proofs)
        evidence = StageEvidence(request(), clock_at([0.0]), processes=lambda **_: next(asked))
        self.assertIs(evidence.processes(), proofs[0])
        fresh = [evidence.fresh_processes(), evidence.fresh_processes()]
        self.assertEqual([type(proof) for proof in fresh], [FreshProcesses, FreshProcesses])
        self.assertEqual([proof.proof for proof in fresh], proofs[1:])
        self.assertIs(evidence.processes(), proofs[0])       # the first pass's proof is no fresh one


class FreshProofs(unittest.TestCase):
    """Section 7.1: a removal's final check takes proof only `fresh_*` can make, read now."""

    def test_constructors_refuse_without_the_token(self):
        with self.assertRaises(TypeError):
            FreshRegistrations((), False)
        with self.assertRaises(TypeError):
            FreshRegistrations((), False, object())
        with self.assertRaises(TypeError):
            FreshProcesses(SimpleNamespace(busy=lambda path: False))
        self.assertFalse(hasattr(StageEvidence, 'listings'))      # the mutable Listings never leave the evidence

    def proving(self, proof, times=None):
        """Evidence whose `prove` answers `proof`, and the calls it was asked."""
        asked = []

        def prove(listings, repo, worktree, *, pointer, timeout_s, side=None):
            asked.append(((repo, worktree), dict(pointer=pointer, timeout_s=timeout_s)))
            return proof
        return StageEvidence(request(), clock_at(times or [0.0]), processes=None, prove=prove), asked

    def test_overlaps_comes_from_proof_reach_as_overlaps_worktrees_did(self):
        """The cases of `overlaps_worktrees` at 6a2d0b1 (test_disk_removal, test_disk_fix8), measured against `Proof.reach`:
        the target nests in, or holds, another place; its own row never counts."""
        rows = ('/w/outer', '/w/outer/generated/inner', '/w/solo')
        reach = tuple(placement.cleanup_parts(path) for path in rows)
        for target, own, expected in (('/w/outer/generated', '/w/outer', True), ('/w/outer', '/w/outer', True),
                                      ('/w/solo/bin', '/w/solo', False), ('/w/free/bin', '/w/free', False),
                                      ('/w', '/w/solo', True), ('/w/outer/generated/inner/x', '/w/outer', True)):
            with self.subTest(target=target):
                evidence, asked = self.proving(Proof((), reach))
                fresh = evidence.fresh_registrations('/r/one', target, own, pointer=True)
                self.assertEqual((type(fresh), fresh.refused, fresh.overlaps), (FreshRegistrations, (), expected))
                self.assertEqual(asked[0][0], ('/r/one', own))       # the proof is of the worktree, nesting of the target
        nested, tree = placement.cleanup_parts('/mnt/h/tree/nested'), placement.cleanup_parts('/mnt/h/tree')
        for target, own, reach, expected in ((r'H:\TREE', '/mnt/h/tree', (nested,), True),
                                             (r'H:\tree/out', r'\\?\H:\TREE', (tree,), False)):
            with self.subTest(spelling=target):
                evidence, _ = self.proving(Proof((), reach))
                self.assertIs(evidence.fresh_registrations('/r/one', target, own, pointer=True).overlaps, expected)
        refusal = Refusal('/r/one', '/w/solo', 'changed', '.git')
        evidence, asked = self.proving(Proof((refusal,), ()))
        self.assertEqual(evidence.fresh_registrations('/r/one', '/w/solo', '/w/solo', pointer=False).refused, (refusal,))
        self.assertIs(asked[0][1]['pointer'], False)

    def test_proof_timeout_is_patience(self):
        """The proof's timeout is the clock's patience on PROOF_TIMEOUT_S: its cap early, the 10 s floor at the 80% line."""
        times = [0.0]
        evidence, asked = self.proving(Proof((), ()), times)
        evidence.fresh_registrations('/r/one', '/w/a', '/w/a', pointer=True)
        times.append(1440.0)
        evidence.fresh_registrations('/r/one', '/w/a', '/w/a', pointer=True)
        self.assertEqual([options['timeout_s'] for _, options in asked], [git_worktrees.PROOF_TIMEOUT_S, 10.0])

    def test_a_repo_whose_listing_failed_raises_in_any_repo(self):
        def listing(repo, **_):
            if repo == '/r/two':
                raise RegistrationsUnknown('fatal: not a git repository')
            return Listing(repo, (dict(worktree='/w/a'),))

        evidence = StageEvidence(request(), clock_at([0.0]), processes=None)
        with patch.object(git_worktrees, 'listing', listing):
            for repo in ('/r/one', '/r/two'):
                with self.subTest(repo=repo), self.assertRaisesRegex(RegistrationsUnknown, 'not a git repository'):
                    evidence.fresh_registrations(repo, '/w/a', '/w/a', pointer=True)

    def test_fresh_registrations_reads_every_repo_once_and_one_tip_for_a_branch(self):
        """Through a removal over a scene of two repos: one proof, each repo's admin files read once, each repo listed
        once in the stage (beta first at the proof), and one tip `rev-parse` for a branch target, none for a detached one."""
        from tests.disk_trace import Scene
        for detached, tips in ((False, 1), (True, 0)):
            with self.subTest(detached=detached):
                scene = Scene()
                self.addCleanup(scene.doCleanups)
                scene.repo('beta')
                scene.worktree('w', detached=detached)
                start = []
                scene.midstage = lambda _: start.append(len(scene.git.asked))
                with patch.object(git_worktrees, '_read_admin', wraps=git_worktrees._read_admin) as read, \
                        patch.object(StageEvidence, 'fresh_registrations', autospec=True,
                                     side_effect=StageEvidence.fresh_registrations) as fresh:
                    trace = scene.record(apply=True)
                self.assertTrue(trace['deleted'])
                self.assertEqual(fresh.call_count, 1)
                self.assertEqual(sorted(placement.cleanup_parts(call.args[0]) for call in read.call_args_list),
                                 sorted(placement.cleanup_parts(scene.common(name)) for name in ('alpha', 'beta')))
                self.assertEqual(scene.git.count('worktree', 'list'), 2)
                asked = scene.git.asked[start[0]:]
                self.assertEqual(asked.count(['rev-parse', '--verify', 'refs/heads/w']), tips)   # the tip, not main's


class Inventories(unittest.TestCase):
    def test_inventory_is_read_only(self):
        evidence = StageEvidence(request(inventory_id=None), clock_at([0.0]), processes=None)
        self.assertIsInstance(evidence.inventory, Inventory)
        self.assertEqual(evidence.inventory.now, 100.0)
        with self.assertRaises(AttributeError):
            evidence.inventory = Inventory(0)


class PathFacts(unittest.TestCase):
    def test_path_facts_live_in_disk_inventory(self):
        for name in ('repository_config', 'repository_root', 'safe_path', 'worktree_roots'):
            self.assertEqual(getattr(disk_inventory, name).__module__, 'drover.disk_inventory')

    def test_repository_config_answers_true_on_any_git_failure(self):
        """3M1: anything but a clean answer is evidence, so the folder is retained; the question has the fixed NORMAL cap."""
        from pathlib import Path
        asked = []
        unknown = git_client.GitReply(git_client.Answer.UNKNOWN, '', '', None, 'timeout')
        no_git = git_client.GitReply(git_client.Answer.UNKNOWN, '', '', None, 'unavailable')
        for name, reply in (('timeout', unknown), ('no git', no_git), ('os error', OSError('gone')),
                            ('value error', ValueError('bad'))):
            def ask(repo, *args, timeout_s, **_):
                asked.append(timeout_s)
                if isinstance(reply, Exception):
                    raise reply
                return reply
            with self.subTest(name), patch.object(git_client, 'ask', ask):
                self.assertIs(disk_inventory.repository_config(Path('/admin')), True)
        self.assertEqual(set(asked), {git_client.NORMAL})

    def test_repository_config_lets_a_programming_error_through(self):
        """Only a failed question is evidence: a bug in the caller or a fake raises, as before the move."""
        from pathlib import Path
        def ask(repo, *args, timeout_s, **_):
            raise TypeError('a bug, not an answer')
        with patch.object(git_client, 'ask', ask), self.assertRaises(TypeError):
            disk_inventory.repository_config(Path('/admin'))


def git_reply(seen, answers=None, *, code=0, err=''):
    """`git_answers`' reply: each command's (first word, timeout) joins `seen`; `answers` maps an argv's words to
    stdout, '' when it names none."""
    def reply(repo, *args, timeout):
        seen.append((args[0], timeout))
        out = next((text for words, text in (answers or {}).items() if words == args[:len(words)]), '')
        return SimpleNamespace(returncode=code, stdout=out, stderr=err)
    return git_answers(reply)


class Reads(unittest.TestCase):
    """Section 7.1: every read's git call takes the stage clock's patience, and a failed read is unknown."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='disk-evidence-', dir=clean_parent())
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.worktree = self.root / 'alpha.worktrees' / 'w'
        self.worktree.mkdir(parents=True)
        (self.worktree / 'a.txt').write_text('a', encoding='utf-8')
        self.machine = dict(name='alpha', repo=str(self.root / 'repo'), remote='origin', default_branch='main')
        self.wire = dict(stage=4, side=placement.cleanup_side(), apply=False, now=100.0,
                         drive=dict(path=str(self.root)), repos=[self.machine], paths=dict(worker_timeout_s=1800),
                         state_dir=str(self.root / 'state'), tasks={})
        for context in (patch.object(placement, 'physical_disk_path', return_value=str(self.root)),
                        patch.object(ownership, 'read', return_value=None)):
            context.start()
            self.addCleanup(context.stop)

    def evidence(self, times, **changes):
        stage = StageRequest.from_wire(dict(self.wire, **changes))
        return StageEvidence(stage, StageClock(1800, clock=lambda: times[-1]),
                             processes=lambda **_: SimpleNamespace(busy=lambda path: False))

    def test_read_steps_take_patience_not_60_seconds(self):
        """At the 80% line patience(NORMAL) is the 10 s floor, where a fixed read waited 60 s."""
        times, seen = [0.0], []
        evidence = self.evidence(times)
        times.append(1440.0)
        answers = {('rev-parse', '--verify', 'HEAD'): 'abc\n', ('rev-parse', '--absolute-git-dir'): '/admin/w\n'}
        with git_reply(seen, answers):
            disk_authority.inspect(SimpleNamespace(request=StageRequest.from_wire(self.wire), evidence=evidence),
                                   self.machine, dict(worktree=str(self.worktree)), processes=evidence.processes())
        self.assertEqual(seen, [('status', 10.0), ('rev-parse', 10.0), ('rev-parse', 10.0)])
        seen.clear()
        with git_reply(seen, answers):
            evidence.status(self.worktree)
            evidence.head(self.worktree)
            evidence.gitdir(self.worktree)
            evidence.tracked(self.worktree)
            self.assertIs(evidence.merged(self.machine, 'abc'), True)       # merge-base says yes
        self.assertEqual([word for word, _ in seen], ['status', 'rev-parse', 'rev-parse', 'ls-files', 'rev-parse',
                                                       'merge-base'])
        self.assertEqual({timeout for _, timeout in seen}, {10.0})

    def test_a_failed_read_raises_and_the_row_is_kept(self):
        """A failed read is unknown: it raises, and stage 4 keeps the worktree as `unavailable`."""
        seen = []
        evidence = self.evidence([0.0])
        with git_reply(seen, code=128, err='fatal: bad object\n'):
            for read in (evidence.status, evidence.head, evidence.gitdir, evidence.tracked):
                with self.subTest(read.__name__), self.assertRaisesRegex(ValueError, '^fatal: bad object$'):
                    read(self.worktree)
            with listing_answers([dict(worktree=str(self.worktree))]), \
                    patch.object(disk_worktrees, 'registrations', return_value={}), \
                    patch.object(disk_worktrees, 'unlisted', return_value=[]), \
                    patch.object(placement, 'cleanup_processes', return_value=SimpleNamespace(busy=lambda p: False)):
                out = staged(disk_worktrees.settled, self.wire)
        row = next(i for i in out['items'] if i['path'] == str(self.worktree))
        self.assertEqual((row['action'], row['reason_code'], row['error']), ('keep', 'unavailable', 'fatal: bad object'))

    def test_only_report_only_runs_reuse_the_inventory(self):
        """disk_paths.py:23 at 0cf5d7b: a report-only run measures with the stage's inventory; an apply run, or one
        ignoring activity, measures afresh."""
        for apply, ignore, reused in ((False, (), True), (True, (), False), (False, (str(self.worktree),), False)):
            evidence, used = self.evidence([0.0], apply=apply), []

            def measure(inventory, path, *, strict=False):
                used.append((inventory, strict))
                return dict(files=1, bytes=2, age_s=3, incomplete=False)
            with self.subTest(apply=apply, ignore=ignore), patch.object(Inventory, 'measure', autospec=True,
                                                                        side_effect=measure):
                self.assertEqual(evidence.measure(self.worktree, ignore_activity=ignore), dict(files=1, bytes=2, age_s=3))
                self.assertEqual([(inventory is evidence.inventory, strict) for inventory, strict in used],
                                 [(reused, True)])

    def test_fresh_snapshot_is_none_past_900_seconds(self):
        now = 100_000.0
        for at, fresh in ((now, True), (now - 900, True), (now - 901, False), (now + 1, False), ('99999', False),
                          (None, False)):
            snapshot = dict(at=at, tasks=[]) if at is not None else {}
            with self.subTest(at=at):
                found = self.evidence([0.0], now=now, tasks=snapshot).fresh_snapshot()
                self.assertEqual(found, snapshot if fresh else None)

    def test_needs_idle_gate_route_reads_fresh_snapshot(self):
        """The closed-task gate asks the evidence for its snapshot: a closed, unlanded task gates, a stale one never."""
        evidence, tag = self.evidence([0.0]), dict(owner='drover:7')
        run = SimpleNamespace(evidence=evidence)
        closed = dict(at=100.0, tasks=[dict(id=7, state='yanked', terminal=True, live=False)])
        with patch.object(evidence, 'fresh_snapshot', return_value=closed) as fresh:
            self.assertIs(disk_authority.needs_idle_gate(run, tag), True)
        fresh.assert_called_once_with()
        with patch.object(evidence, 'fresh_snapshot', return_value=None):
            self.assertIs(disk_authority.needs_idle_gate(run, tag), False)
        landed = dict(closed, tasks=[dict(id=7, state='landed')])
        with patch.object(evidence, 'fresh_snapshot', return_value=landed):
            self.assertIs(disk_authority.needs_idle_gate(run, tag), False)

    def test_moved_functions_live_in_disk_evidence(self):
        for name in ('identity', 'registration_identity', 'outputs_at', 'snapshot'):
            self.assertEqual(getattr(disk_evidence, name).__module__, 'drover.disk_evidence')

    def test_snapshot_takes_fresh_processes_never_processes(self):
        """Section 7.1: a stray's snapshot always takes a new process proof, whatever the first pass saw."""
        evidence = StageEvidence(StageRequest.from_wire(self.wire), StageClock(1800),
                                 processes=lambda **_: SimpleNamespace(busy=lambda path: True))
        with patch.object(evidence, 'processes', side_effect=AssertionError('the first pass proof')), \
                patch.object(evidence, 'fresh_processes', wraps=evidence.fresh_processes) as fresh, \
                self.assertRaisesRegex(ValueError, '^snapshot busy$'):
            disk_evidence.snapshot(self.machine, self.worktree, evidence=evidence)
        fresh.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
