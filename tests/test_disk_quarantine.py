"""Applied observations, not scan age, establish orphan grace."""
import shutil
from pathlib import Path

from tests.test_disk_worktrees import WorktreeCase

DAY = 86400


class Quarantine(WorktreeCase):
    def test_untagged_and_lapsed_three_day_grace(self):
        from drover import ownership
        paths = []
        for name, expired, ignored in (
            ('untagged', False, None), ('expired', True, None),
            ('orphan-data', False, 'data/keep'), ('orphan-env', False, '.venv/keep'),
        ):
            wt = self.worktree(name, lease=False)
            self.r.commit(name + '.txt', cwd=wt)
            self.merge(wt)
            if expired:
                ownership.tag(str(wt), 'alpha', 'old-session', self.now - 1,
                              now=self.now - 2 * DAY)
            if ignored:
                p = wt / ignored
                p.parent.mkdir(parents=True)
                p.write_text('must survive')
            paths.append((wt, ignored))
        self.cleanup()
        # Dry scans cannot start grace or modify quarantine.
        self.now += 4 * DAY
        self.cleanup(apply=True)
        self.assertTrue(all(p.exists() for p, _ in paths))
        self.now += 3 * DAY - 1
        self.cleanup(apply=True)
        self.assertTrue(all(p.exists() for p, _ in paths))
        self.now += 1
        result = self.cleanup(apply=True)
        for wt, ignored in paths:
            self.assertEqual(wt.exists(), ignored is not None)
            if ignored:
                self.assertEqual((wt / ignored).read_text(), 'must survive')
                self.reported(result, wt / ignored)

    def test_renewal_dirt_and_recreation_reset_grace(self):
        from drover import ownership
        paths = {name: self.worktree(name, lease=False)
                 for name in ('renewed', 'dirty', 'recreated', 'moved-head')}
        for name, path in paths.items():
            self.r.commit('unmerged-' + name, cwd=path)
        self.cleanup()
        self.cleanup(apply=True)
        self.now += 2 * DAY
        ownership.tag(str(paths['renewed']), 'alpha', 'new-session',
                      self.now + 10 * DAY, now=self.now)
        (paths['dirty'] / 'scratch').write_text('busy')
        self.r.commit('moved.txt', cwd=paths['moved-head'])
        recreated = paths['recreated']
        self.r.git('worktree', 'unlock', recreated)
        self.r.git('worktree', 'remove', recreated)
        self.r.git('worktree', 'add', '-q', '-b', 'replacement', recreated, 'origin/main')
        self.r.git('worktree', 'lock', '--reason', self.side, recreated)
        self.cleanup(apply=True)
        (paths['dirty'] / 'scratch').unlink()
        self.now += DAY
        self.cleanup(apply=True)
        self.assertTrue(all(p.exists() for p in paths.values()), 'no inherited grace')
        self.now += 3 * DAY
        self.cleanup(apply=True)
        self.assertTrue(paths['renewed'].exists(), 'current lease protects unmerged work')
        for name in ('dirty', 'recreated', 'moved-head'):
            self.assertFalse(paths[name].exists(), name)

    def test_unlisted_clean_folder_and_unknown_folder_report_only(self):
        from drover import disk_cleanup
        # A positively identified snapshot loses its registration but retains a valid
        # pointer to this repo's objects. A random directory is never "clean".
        clean = self.worktree('unlisted', lease=False)
        head = self.r.git('rev-parse', 'HEAD', cwd=clean)
        clean_git = clean / '.git'
        admin = Path(clean_git.read_text().split(': ', 1)[1].strip())
        if not admin.is_absolute():
            admin = clean / admin
        shutil.rmtree(admin)
        snapshot = self.trees / 'proven snapshot'
        shutil.copytree(self.r.repo, snapshot, ignore=shutil.ignore_patterns('.git'))
        snapshot_head = self.r.git('rev-parse', 'HEAD')
        unknown = self.trees / 'unknown'
        unknown.mkdir()
        (unknown / 'precious').write_text('not git')
        foreign = self.trees / 'independent'
        from tests.gitfix import sh
        sh('git', 'init', '-q', foreign)
        (foreign / 'precious').write_text('separate repo')
        self.cleanup()
        result = self.cleanup(apply=True)
        self.assertTrue(snapshot.exists(), 'positive snapshot proof still requires grace')
        self.assertTrue(clean.exists(), 'broken pointer is report-only')
        self.reported(result, clean)
        self.reported(result, unknown)
        self.now += 3 * DAY
        result = self.cleanup(apply=True)
        # Broken pointers provide no positive snapshot proof, even after grace.
        self.assertFalse(snapshot.exists(), 'known clean snapshot can expire after full grace')
        records = [r for r in self.journal() if r['path'] == str(snapshot)]
        self.assertEqual(records[-1]['head'], snapshot_head)
        self.assertIn('--detach', records[-1]['restore_argv'])
        self.assertTrue(clean.exists())
        self.assertTrue(unknown.exists())
        self.assertTrue((foreign / '.git').is_dir())
        self.assertEqual((unknown / 'precious').read_text(), 'not git')
        self.assertEqual(self.r.git('rev-parse', head), head)

    def test_unlisted_activity_cancels_elapsed_grace(self):
        snapshot = self.trees / 'busy snapshot'
        self.trees.mkdir(exist_ok=True)
        shutil.copytree(self.r.repo, snapshot, ignore=shutil.ignore_patterns('.git'))
        self.cleanup()
        self.cleanup(apply=True)
        self.now += 2 * DAY
        self.busy_paths.add(str(snapshot))
        self.cleanup(apply=True)
        self.busy_paths.clear()
        self.now += DAY
        self.cleanup(apply=True)
        self.assertTrue(snapshot.exists(), 'busy observation must cancel original grace')
        self.now += 3 * DAY
        self.cleanup(apply=True)
        self.assertFalse(snapshot.exists())
