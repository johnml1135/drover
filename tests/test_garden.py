import unittest
from pathlib import Path

from drover import garden
from drover.ledger import db as ledger_db
from drover.ledger import ops
from tests.gitfix import Repo, sh

DAY = 86400


def decider(answer=None, acted=False, calls=None):
    def d(name, state, rule_answer=None, dynamic_criteria=None, recorder=None):
        if calls is not None:
            calls.append((name, state))
        return {'name': name, 'answer': answer if acted else None, 'shadow_answer': answer, 'acted': acted,
                'confidence': 0.9 if acted else 0.3}
    return d


class GardenCase(unittest.TestCase):
    def setUp(self):
        self.r = Repo(ignore='bin/\nobj/\nartifacts/\n')
        self.addCleanup(self.r.cleanup)
        (self.r.root / 'noproc').mkdir()
        self.proc = garden.Proc(self.r.root / 'noproc', windows=False)
        self.state = self.r.root / 'state'
        import os
        self._env = os.environ.get('DROVER_STATE_DIR')
        os.environ['DROVER_STATE_DIR'] = str(self.state)
        self.addCleanup(lambda: os.environ.pop('DROVER_STATE_DIR') if self._env is None
                        else os.environ.__setitem__('DROVER_STATE_DIR', self._env))

    def worktree(self, name, lock=None):
        path = self.r.root / 'wt' / name
        self.r.git('worktree', 'add', '-q', '-b', name, path, 'origin/main')
        if lock:
            self.r.git('worktree', 'lock', '--reason', lock, path)
        return path

    def propose(self, **kw):
        kw.setdefault('decider', decider())
        return garden.propose(self.r.repo, proc=self.proc, min_age_days=0, **kw)

    def names(self, items):
        return sorted(Path(i['path']).name for i in items)

    def land(self, branch):
        self.r.git('push', '-q', 'origin', f'{branch}:main')
        self.r.git('fetch', '-q', 'origin')


class ClassifyTests(GardenCase):
    def test_fresh_worktree_is_never_merged(self):
        self.worktree('fresh')
        out = self.propose()
        self.assertEqual(self.names(out['keep']), ['fresh'])
        self.assertEqual(out['removable'], [])

    def test_merged_by_ancestry_is_removable(self):
        wt = self.worktree('m')
        self.r.commit('m.txt', cwd=wt)
        self.land('m')
        self.assertEqual(self.names(self.propose()['removable']), ['m'])

    def test_rebased_landing_is_patch_equivalent_via_cherry(self):
        wt = self.worktree('rb')
        self.r.commit('rb.txt', 'rb\n', cwd=wt)
        self.r.commit('other.txt', 'o\n')
        self.r.git('push', '-q', 'origin', 'HEAD:main')
        sh('git', '-C', self.r.repo, 'cherry-pick', 'rb', check=False)
        self.r.git('push', '-q', 'origin', 'HEAD:main')
        self.r.git('fetch', '-q', 'origin')
        self.assertEqual(self.r.git('cherry', 'origin/main', 'rb').count('+'), 0)
        self.assertEqual(self.names(self.propose()['removable']), ['rb'])

    def test_squash_merge_detected(self):
        wt = self.worktree('sq')
        self.r.commit('s1.txt', cwd=wt)
        self.r.commit('s2.txt', cwd=wt)
        self.r.git('merge', '--squash', 'sq')
        self.r.git('commit', '-q', '-m', 'squash')
        self.r.git('push', '-q', 'origin', 'HEAD:main')
        self.r.git('fetch', '-q', 'origin')
        self.assertEqual(self.names(self.propose()['removable']), ['sq'])

    def test_dirty_goes_to_owner_batch_even_when_merged(self):
        wt = self.worktree('d')
        self.r.commit('d.txt', cwd=wt)
        self.land('d')
        (wt / 'scratch.txt').write_text('wip')
        out = self.propose()
        self.assertEqual((out['removable'], self.names(out['owner'])), ([], ['d']))
        self.assertEqual(out['owner'][0]['kind'], 'dirty')

    def test_busy_note_locked_and_other_side_are_kept(self):
        busy = self.worktree('busy')
        self.r.commit('b.txt', cwd=busy)
        self.land('busy')
        proc = self.r.root / 'proc'
        (proc / '42').mkdir(parents=True)
        (proc / '42' / 'cmdline').write_bytes(b'sleep\0')
        (proc / '42' / 'cwd').symlink_to(busy)
        self.proc = garden.Proc(proc, windows=False)
        note = self.worktree('note', lock='keep: reviewing')
        self.r.commit('n.txt', cwd=note)
        self.land('note')
        out = self.propose()
        self.assertEqual(out['removable'], [])
        self.assertEqual(self.names(out['keep']), ['busy', 'note'])

    def test_station_kept(self):
        wt = self.worktree('st')
        self.r.commit('st.txt', cwd=wt)
        self.land('st')
        garden.tag_worktree(wt, 'p', 'station')
        self.assertEqual(self.names(self.propose()['keep']), ['st'])


class VerdictTests(GardenCase):
    def unmerged(self, name='u'):
        wt = self.worktree(name)
        self.r.commit(f'{name}.txt', cwd=wt, msg='Add the thing')
        return wt

    def test_state_has_messages_diffstat_and_matching_main_commits(self):
        self.unmerged()
        self.r.commit('main-thing.txt', msg='Add the thing')
        self.r.git('push', '-q', 'origin', 'HEAD:main')
        self.r.git('fetch', '-q', 'origin')
        calls = []
        self.propose(decider=decider(calls=calls))
        name, state = calls[0]
        self.assertEqual(name, 'branch_superseded')
        self.assertIn('Add the thing', state)
        self.assertIn('u.txt', state)
        self.assertIn('same subject', state)

    def test_acted_true_is_superseded_and_goes_to_owner(self):
        self.unmerged()
        out = self.propose(decider=decider(True, acted=True))
        self.assertEqual([i['verdict'] for i in out['owner']], ['superseded'])

    def test_acted_false_is_valuable_not_a_deletion(self):
        self.unmerged()
        out = self.propose(decider=decider(False, acted=True))
        self.assertEqual((out['owner'], self.names(out['valuable'])), ([], ['u']))

    def test_below_tau_marks_needs_haiku_verdict_in_tag(self):
        wt = self.unmerged()
        garden.tag_worktree(wt, 'p', 'slice')
        out = self.propose(decider=decider(True, acted=False))
        self.assertEqual([i['verdict'] for i in out['owner']], ['needs-haiku-verdict'])
        self.assertEqual(garden.read_tag(garden.admin_dir_of(wt))['state'], 'needs-haiku-verdict')

    def test_decider_error_is_needs_haiku_verdict(self):
        self.unmerged()

        def boom(*a, **k):
            raise RuntimeError('x')
        self.assertEqual(self.propose(decider=boom)['owner'][0]['verdict'], 'needs-haiku-verdict')


class BatchTests(GardenCase):
    def setUp(self):
        super().setUp()
        self.conn = ledger_db.connect(self.r.root / 'l.db')
        self.addCleanup(self.conn.close)

    def setup_items(self):
        self.merged = self.worktree('merged')
        self.r.commit('m.txt', cwd=self.merged)
        self.land('merged')
        self.unm = self.worktree('unm')
        self.r.commit('u.txt', cwd=self.unm)
        self.dirty = self.worktree('dirty')
        (self.dirty / 'wip.txt').write_text('precious')
        self.dirty_other = self.worktree('dirty2')
        self.r.commit('d2.txt', cwd=self.dirty_other)
        (self.dirty_other / 'wip.txt').write_text('precious2')
        return self.propose(decider=decider(True, acted=True))

    def test_question_lists_items_and_one_tap_approves_all(self):
        out = self.setup_items()
        self.assertEqual(self.names(out['removable']), ['merged'])
        qid = garden.ask_owner(self.conn, out['owner'], self.r.repo)
        q = ops.get_question(self.conn, qid)
        self.assertEqual([o['label'] for o in q['options']], [garden.APPROVE, garden.DENY])
        for p in (self.unm, self.dirty_other):
            self.assertIn(str(p), q['body'])
        self.assertEqual(self.names(out['owner']), ['dirty2', 'unm'])   # clean unborn 'dirty' worktree is unchanged: kept
        self.assertIsNone(garden.ask_owner(self.conn, [], self.r.repo))

    def test_unanswered_or_denied_removes_nothing(self):
        out = self.setup_items()
        qid = garden.ask_owner(self.conn, out['owner'], self.r.repo)
        with self.assertRaises(ValueError):
            garden.execute_approved(self.conn, qid, proc=self.proc, apply=True)
        ops.answer_question(self.conn, qid, choice=garden.DENY)
        self.assertEqual(garden.execute_approved(self.conn, qid, proc=self.proc, apply=True), [])
        self.assertTrue(self.unm.is_dir() and self.dirty_other.is_dir())

    def test_approved_removes_by_path_keeps_rescue_and_spares_keep_lines(self):
        out = self.setup_items()
        qid = garden.ask_owner(self.conn, out['owner'], self.r.repo)
        ops.answer_question(self.conn, qid, choice=garden.APPROVE, text=f'keep "{self.dirty_other}/"')   # quoted, trailing slash
        dry = garden.execute_approved(self.conn, qid, proc=self.proc)
        self.assertTrue(all(r.get('dry_run') for r in dry) and self.unm.is_dir())
        res = garden.execute_approved(self.conn, qid, proc=self.proc, apply=True)
        self.assertEqual([Path(r['path']).name for r in res], ['unm'])
        self.assertFalse(self.unm.exists())
        self.assertTrue(self.dirty_other.is_dir())
        self.assertTrue((self.dirty / 'wip.txt').exists())          # never listed: untouched
        self.assertTrue(self.r.git('for-each-ref', 'refs/tags/drover-rescue'))
        self.assertNotIn('unm', self.r.git('for-each-ref', '--format=%(refname:short)', 'refs/heads').split())
        self.assertTrue((self.state / 'garden-manifest.jsonl').exists())

    def test_dirty_item_removed_only_when_listed_and_new_dirt_is_preserved(self):
        out = self.setup_items()
        qid = garden.ask_owner(self.conn, out['owner'], self.r.repo)
        ops.answer_question(self.conn, qid, choice=garden.APPROVE)
        (self.unm / 'late.txt').write_text('appeared after approval')   # unm was listed clean
        res = {Path(r['path']).name: r for r in garden.execute_approved(self.conn, qid, proc=self.proc, apply=True)}
        self.assertFalse(res['unm']['ok'])
        self.assertTrue((self.unm / 'late.txt').exists())
        self.assertTrue(res['dirty2']['ok'])
        self.assertFalse(self.dirty_other.exists())

    def test_head_moved_since_proposal_is_kept(self):
        out = self.setup_items()
        qid = garden.ask_owner(self.conn, out['owner'], self.r.repo)
        ops.answer_question(self.conn, qid, choice=garden.APPROVE)
        self.r.commit('more.txt', cwd=self.unm)
        res = {Path(r['path']).name: r for r in garden.execute_approved(self.conn, qid, proc=self.proc, apply=True)}
        self.assertEqual(res['unm']['reason'], 'HEAD moved since the proposal')
        self.assertTrue(self.unm.is_dir())

    def test_remove_merged_script_dry_run_then_apply_and_never_prunes(self):
        self.setup_items()
        gone = self.r.root / 'wt' / 'ghost'
        self.r.git('worktree', 'add', '-q', '-b', 'ghost', gone, 'origin/main')
        self.r.commit('g.txt', cwd=gone)
        self.land('ghost')
        import shutil
        shutil.rmtree(gone)   # a missing directory is another side's or a crash's: prune would forget it
        rows = garden.remove_merged(self.r.repo, proc=self.proc, min_age_days=0)
        self.assertEqual([Path(r['path']).name for r in rows], ['merged'])
        self.assertTrue(self.merged.is_dir())
        rows = garden.remove_merged(self.r.repo, apply=True, proc=self.proc, min_age_days=0)
        self.assertTrue(rows[0]['result']['ok'])
        self.assertFalse(self.merged.exists())
        self.assertIn('ghost', self.r.git('worktree', 'list', '--porcelain'))   # entry still there: no prune

    def test_valuable_branch_becomes_a_proposed_task(self):
        wt = self.worktree('val')
        self.r.commit('v.txt', cwd=wt)
        out = self.propose(decider=decider(False, acted=True))
        made = garden.make_tasks_for_valuable(self.conn, self.r.repo, out['valuable'])
        t = ops.get_task(self.conn, made[0])
        self.assertEqual((t['status'], t['acceptance'], t['branch']), ('proposed', ['landed on main'], 'val'))
        self.assertEqual(garden.make_tasks_for_valuable(self.conn, self.r.repo, out['valuable']), [])


class SafetyTests(GardenCase):
    def setUp(self):
        super().setUp()
        self.conn = ledger_db.connect(self.r.root / 'l.db')
        self.addCleanup(self.conn.close)

    def dirty_item(self):
        wt = self.worktree('dd')
        self.r.commit('d.txt', cwd=wt)
        (wt / 'wip.txt').write_text('one')
        out = self.propose(decider=decider(True, acted=True))
        qid = garden.ask_owner(self.conn, out['owner'], self.r.repo)
        ops.answer_question(self.conn, qid, choice=garden.APPROVE)
        return wt, qid, out['owner'][0]

    def test_snapshot_has_porcelain_and_hashes(self):
        wt, qid, item = self.dirty_item()
        self.assertEqual(item['dirt']['porcelain'], ['?? wip.txt'])
        self.assertEqual(len(item['dirt']['untracked_hash']), 64)

    def test_same_snapshot_removes_dirty_work_as_approved(self):
        wt, qid, _ = self.dirty_item()
        res = garden.execute_approved(self.conn, qid, proc=self.proc, apply=True)
        self.assertTrue(res[0]['ok'])
        self.assertFalse(wt.exists())

    def test_extra_dirt_after_approval_refuses_and_asks_to_repropose(self):
        wt, qid, _ = self.dirty_item()
        (wt / 'second.txt').write_text('new work')
        res = garden.execute_approved(self.conn, qid, proc=self.proc, apply=True)
        self.assertEqual((res[0]['ok'], res[0].get('repropose')), (False, True))
        self.assertTrue((wt / 'second.txt').exists() and (wt / 'wip.txt').exists())

    def test_untracked_file_edited_after_approval_is_kept_and_reproposed(self):
        wt, qid, _ = self.dirty_item()                       # wip.txt untracked, content 'one'
        (wt / 'wip.txt').write_text('two')                   # same name, different size and content
        res = garden.execute_approved(self.conn, qid, proc=self.proc, apply=True)
        self.assertEqual((res[0]['ok'], res[0].get('repropose')), (False, True))
        self.assertEqual((wt / 'wip.txt').read_text(), 'two')

    def test_untracked_edit_with_same_size_is_still_detected(self):
        wt, qid, _ = self.dirty_item()
        (wt / 'wip.txt').write_text('ONE')                   # same length as 'one'
        res = garden.execute_approved(self.conn, qid, proc=self.proc, apply=True)
        self.assertTrue(res[0].get('repropose'))
        self.assertTrue(wt.exists())

    def test_oversized_or_unreadable_untracked_refuses_removal(self):
        wt, qid, _ = self.dirty_item()
        old = garden.MAX_UNTRACKED_BYTES
        garden.MAX_UNTRACKED_BYTES = 2                       # 'one' is 3 bytes
        self.addCleanup(setattr, garden, 'MAX_UNTRACKED_BYTES', old)
        snap = garden.dirt_snapshot(wt)
        self.assertIn('exceed', snap['error'])
        res = garden.execute_approved(self.conn, qid, proc=self.proc, apply=True)
        self.assertTrue(res[0].get('repropose'))
        self.assertTrue((wt / 'wip.txt').exists())

    def test_approved_snapshot_with_error_never_removes(self):
        wt, qid, item = self.dirty_item()
        common, found = garden.entries(self.r.repo)
        bad = dict(item['dirt'], error='cannot read untracked file x: denied')
        res = garden.remove_worktree(common, found[0], 'origin/main', self.proc, approved_dirt=bad)
        self.assertFalse(res['ok'])
        self.assertTrue(wt.exists())

    def test_edited_file_with_same_name_is_a_different_snapshot(self):
        wt = self.worktree('ed')
        self.r.commit('t.txt', 'a\n', cwd=wt)
        (wt / 't.txt').write_text('changed\n')
        out = self.propose(decider=decider(True, acted=True))
        qid = garden.ask_owner(self.conn, out['owner'], self.r.repo)
        ops.answer_question(self.conn, qid, choice=garden.APPROVE)
        (wt / 't.txt').write_text('changed again\n')
        res = garden.execute_approved(self.conn, qid, proc=self.proc, apply=True)
        self.assertTrue(res[0].get('repropose'))
        self.assertTrue(wt.exists())

    def test_keep_line_normalised_and_unmatched_keep_warns(self):
        wt, qid, _ = self.dirty_item()
        repo, items, warnings = garden.approved_items(self.conn, qid)
        self.assertEqual(len(items), 1)
        self.assertEqual(warnings, [])
        self.assertEqual(garden.norm_path(str(wt) + '/'), garden.norm_path(str(wt)))
        self.assertEqual(garden.norm_path('"%s"' % wt), garden.norm_path(wt))

    def test_unmatched_keep_line_is_reported(self):
        wt = self.worktree('k')
        self.r.commit('k.txt', cwd=wt)
        out = self.propose(decider=decider(True, acted=True))
        qid = garden.ask_owner(self.conn, out['owner'], self.r.repo)
        ops.answer_question(self.conn, qid, choice=garden.APPROVE, text='keep /nowhere/near')
        _, items, warnings = garden.approved_items(self.conn, qid)
        self.assertEqual(len(items), 1)
        self.assertEqual(len(warnings), 1)

    def test_windows_busy_via_cim_command_line_and_unknown_fails_safe(self):
        wt = self.worktree('w')
        rows = [(None, 'checker test ' + str(wt) + '/tests'), (None, 'notepad')]
        self.assertTrue(garden.Proc(windows=True, cim=lambda: rows).busy(wt))
        self.assertFalse(garden.Proc(windows=True, cim=lambda: [(None, 'notepad ' + str(wt) + '-pair/x')]).busy(wt))

        def timeout():
            raise garden.subprocess.TimeoutExpired('powershell', 30)
        self.assertTrue(garden.Proc(windows=True, cim=timeout).busy(wt))

    def test_missing_proc_is_unknown_and_busy(self):
        wt = self.worktree('np')
        self.assertTrue(garden.Proc(self.r.root / 'does-not-exist', windows=False).busy(wt))

    def test_sibling_prefix_command_does_not_make_worktree_busy(self):
        wt = self.worktree('x')
        proc = self.r.root / 'proc2'
        (proc / '7').mkdir(parents=True)
        (proc / '7' / 'cmdline').write_bytes(b'checker\0build\0' + str(wt).encode() + b'-pair/project.txt\0')
        (proc / '7' / 'cwd').symlink_to(self.r.root)
        self.assertFalse(garden.Proc(proc, windows=False).busy(wt))

    def test_young_worktree_is_kept(self):
        wt = self.worktree('y')
        self.r.commit('y.txt', cwd=wt)
        self.land('y')
        out = garden.propose(self.r.repo, proc=self.proc, decider=decider(), min_age_days=1)
        self.assertEqual((out['removable'], self.names(out['keep'])), ([], ['y']))

    def test_other_side_worktree_is_never_touched(self):
        wt = self.worktree('os')
        self.r.commit('o.txt', cwd=wt)
        self.land('os')
        meta = garden.admin_dir_of(wt)
        (meta / 'gitdir').write_text('C:\\somewhere\\os\\.git\n')
        out = self.propose()
        self.assertEqual((out['removable'], out['owner']), ([], []))
        self.assertTrue(wt.is_dir())

    def test_plain_lock_without_reason_is_not_a_note(self):
        wt = self.worktree('pl')
        self.r.commit('p.txt', cwd=wt)
        self.land('pl')
        self.r.git('worktree', 'lock', wt)
        self.assertEqual(self.names(self.propose()['removable']), ['pl'])

    def test_lock_note_added_after_proposal_is_honoured(self):
        wt = self.worktree('ln')
        self.r.commit('l.txt', cwd=wt)
        self.land('ln')
        out = self.propose()
        self.assertEqual(self.names(out['removable']), ['ln'])
        self.r.git('worktree', 'lock', '--reason', 'keep: reviewing', wt)
        common, found = garden.entries(self.r.repo)
        res = garden.remove_worktree(common, found[0], 'origin/main', self.proc)
        self.assertFalse(res['ok'])
        self.assertTrue(wt.is_dir())

    def test_detached_unreachable_head_gets_rescue_tag(self):
        wt = self.r.root / 'wt' / 'det'
        self.r.git('worktree', 'add', '-q', '--detach', wt, 'origin/main')
        sha = self.r.commit('det.txt', cwd=wt)
        common, found = garden.entries(self.r.repo)
        res = garden.remove_worktree(common, found[0], 'origin/main', self.proc, approved_dirt=garden.dirt_snapshot(wt))
        self.assertTrue(res['ok'])
        self.assertIn(sha, self.r.git('for-each-ref', '--format=%(objectname)', 'refs/tags/drover-rescue'))

    def test_branch_checked_out_elsewhere_is_not_deleted(self):
        wt = self.worktree('shared')
        self.r.commit('s.txt', cwd=wt)
        self.land('shared')
        other = self.r.root / 'wt' / 'other'
        self.r.git('worktree', 'add', '-q', '--force', other, 'shared')
        common, found = garden.entries(self.r.repo)
        mine = [e for e in found if e.path == wt][0]
        res = garden.remove_worktree(common, mine, 'origin/main', self.proc, approved_dirt=garden.dirt_snapshot(wt), delete_branch=True)
        self.assertTrue(res['ok'])
        self.assertIn('shared', self.r.git('for-each-ref', '--format=%(refname:short)', 'refs/heads').split())

    def test_propose_writes_nothing_without_a_verdict_to_mark(self):
        wt = self.worktree('ro')
        self.r.commit('r.txt', cwd=wt)
        self.land('ro')
        before = (self.r.git('worktree', 'list', '--porcelain'), sorted(p.name for p in garden.admin_dir_of(wt).iterdir()))
        garden.propose(self.r.repo, proc=self.proc, decider=decider(), min_age_days=0)
        after = (self.r.git('worktree', 'list', '--porcelain'), sorted(p.name for p in garden.admin_dir_of(wt).iterdir()))
        self.assertEqual(before, after)
        self.assertTrue(wt.is_dir())


class TagAndPurgeTests(GardenCase):
    def test_tag_roundtrip_keeps_fork_point(self):
        wt = self.worktree('t')
        tag = garden.tag_worktree(wt, 'ui', 'slice', owner='lead-1')
        self.assertEqual(garden.read_tag(garden.admin_dir_of(wt))['repo'], 'ui')
        self.assertEqual(tag['base_sha'], self.r.git('rev-parse', 'origin/main'))
        again = garden.tag_worktree(wt, 'ui', 'station')
        self.assertEqual((again['kind'], again['base_sha']), ('station', tag['base_sha']))

    def test_purge_only_ignored_untracked_build_output(self):
        wt = self.worktree('p')
        for rel in ('obj/x.bin', 'bin/Debug/a.dll', 'bin/.cache/keep.txt', 'artifacts/z', 'src/obj/y'):
            (wt / rel).parent.mkdir(parents=True, exist_ok=True)
            (wt / rel).write_text('x')
        self.r.commit('tracked.txt', cwd=wt)
        freed, dirs, kept = garden.purge(wt)
        self.assertTrue((wt / 'obj').exists())                     # dry run
        garden.purge(wt, apply=True)
        for gone in ('obj', 'bin/Debug', 'artifacts', 'src/obj'):
            self.assertFalse((wt / gone).exists(), gone)
        self.assertTrue((wt / 'bin/.cache/keep.txt').exists())
        self.assertTrue((wt / 'tracked.txt').exists())
        self.assertEqual(kept, ['bin/.cache'])


if __name__ == '__main__':
    unittest.main()
