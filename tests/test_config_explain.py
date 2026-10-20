"""Explain exposes the same committed configuration and sources as the loaders."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from unittest import mock
from drover import config, repos
from drover.__main__ import main
from tests.test_config_sources import derived
from tests.test_repo_entry import RepoCase, ENTRY, SCRIPT


class Explain(RepoCase):
    def invoke(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = main(['config', *argv])
            except SystemExit as exc:
                self.fail(f'config explain must be accepted by the public CLI: {exc}; {err.getvalue()}')
        return code, out.getvalue(), err.getvalue()

    def explanation(self, argv, key, value, sources):
        code, out, err = self.invoke(['explain', key, *argv])
        self.assertEqual(code, 0, err)
        self.assertEqual(err, '')
        self.assertEqual(json.loads(out), {'key': key, 'value': value, 'sources': sources})

    def test_cli_scopes(self):
        state = self.fix.root / 'state'
        directory = state / 'repos'
        directory.mkdir(parents=True)
        global_path = state / 'config.toml'
        global_path.write_text('[capacity]\nparallel_cap=4\n', encoding='utf-8')
        machine_path = directory / 'AlPhA.toml'
        machine_path.write_text(config.dumps(self.machine), encoding='utf-8')
        # Selecting alpha must not attempt to parse this unrelated machine file.
        (directory / 'invalid.toml').write_text('not TOML [', encoding='utf-8')
        old = self.adopt(brief='older', run={'platforms': ['wsl']})
        new = self.adopt(brief='current', run={'platforms': ['wsl'], 'env': {'A.B': 'repo'}})
        (self.fix.repo / ENTRY).write_text('broken dirty entry [', encoding='utf-8')
        repo_label = 'repo:.drover/config.toml@' + new
        machine_label = 'machine:' + str(machine_path.resolve())
        with mock.patch.dict(os.environ, {'DROVER_HOME': str(state), 'DROVER_CAPACITY_PARALLEL_CAP': '4'}):
            config.reset()
            self.explanation([], 'capacity.parallel_cap', 4, {'capacity.parallel_cap': 'env:DROVER_CAPACITY_PARALLEL_CAP'})
            self.explanation([], 'hub.ui_port', 8765, {'hub.ui_port': 'default'})
            scope = ['--repo', 'ALPHA']
            self.explanation(scope, 'brief', 'current', {'brief': repo_label})
            self.explanation(scope, 'remote', 'origin', {'remote': 'default'})
            self.explanation(scope, 'name', 'alpha', {'name': machine_label})
            self.explanation(scope, 'scripts', {'test': SCRIPT}, {'scripts.test': repo_label})
            self.explanation(scope, 'run.default', 'wsl',
                             {'run.default': derived({'run.platforms.0': repo_label})})
            self.explanation(scope, r'run.env.A\.B', 'repo', {r'run.env.A\.B': repo_label})
            self.explanation([*scope, '--revision', old], 'brief', 'older',
                             {'brief': 'repo:.drover/config.toml@' + old})
            fallback = self.fix.git('rev-parse', old + '^')
            self.explanation([*scope, '--revision', fallback], 'scripts', {}, {'scripts': 'default'})
            for argv, detail in ((['explain', 'absent'], 'absent'),
                                 (['explain', 'brief', '--repo', 'missing'], 'missing'),
                                 (['explain', 'hub.ui_port', '--revision', old], 'revision'),
                                 (['explain', 'brief', *scope, '--revision', 'no-such-revision'], 'no-such-revision')):
                with self.subTest(argv=argv):
                    code, out, err = self.invoke(argv)
                    self.assertEqual(code, 2)
                    self.assertEqual(out, '')
                    self.assertIn(detail, err)
            bad = self.fix.commit(ENTRY, 'schema_version=99\n')
            code, out, err = self.invoke(['explain', 'brief', *scope, '--revision', bad])
            self.assertEqual((code, out), (2, ''))
            self.assertIn('schema_version', err)
            self.assertIn(bad, err)
            machine_path.write_text(config.dumps({**self.machine, 'repo': str(state / 'unreachable'),
                                                  'scripts': {'test': 'scripts/legacy.ps1'}}), encoding='utf-8')
            self.explanation(scope, 'scripts.test', 'scripts/legacy.ps1', {'scripts.test': machine_label})
            global_path.write_text('[capacity]\nparallel_cap="invalid"\n', encoding='utf-8')
            code, out, err = self.invoke(['explain', 'hub.ui_port'])
            self.assertEqual((code, out), (2, ''))
            self.assertIn(str(global_path), err)

    def test_repo_origins_and_reload(self):
        state = self.fix.root / 'state'
        config.use(config.validate({'paths': {'state_dir': str(state)}}))
        machine_path = self.fix.root / 'actual-filename.toml'
        label = 'machine:' + str(machine_path.resolve())
        raw = {**self.machine, 'remote': 'origin', 'run': {'env': {'SAME': 'same', 'A.B': 'same'}}}
        old = self.adopt(run={'platforms': ['wsl'], 'env': {'SAME': 'same', 'A.B': 'same', 'REPO': 'repo'}},
                         review={'backend': 'codex'})
        sources = {'stale': 'old'}
        prog = self.call(repos.load, raw, old, machine_path=machine_path, sources=sources)
        repo_label = 'repo:.drover/config.toml@' + old
        self.assertNotIn('stale', sources)
        self.assertEqual(sources['remote'], label, 'explicit equal machine values still own their source')
        self.assertEqual(sources['run.env.SAME'], label)
        self.assertEqual(sources[r'run.env.A\.B'], label)
        self.assertEqual(sources['run.env.REPO'], repo_label)
        self.assertEqual(sources['scripts.test'], repo_label)
        self.assertEqual(sources['run.default'], derived({'run.platforms.0': repo_label}))
        self.assertEqual(sources['gate.platform'], derived({'run.platforms.0': repo_label}))
        self.assertEqual(sources['gate.unknown'], derived({'scripts.test': repo_label}))
        self.assertEqual(sources['worktrees'], derived({'name': label, 'repo': label,
                                                       'config.paths.worktrees': 'injected'}))
        self.assertEqual(sources['review.model'], derived({
            'config.models.review': 'injected', 'review.backend': repo_label,
            'config.models.ladders.codex.0': 'injected', 'config.models.ladders.codex.1': 'injected'}))
        self.assertEqual(sources['review.high_risk_model'], derived({
            'review.backend': repo_label, 'review.model': sources['review.model']}))
        self.assertTrue(all(not key.startswith('_') for key in sources))
        self.assertEqual(config.get()['models']['review'], 'sonnet-5.5')
        self.assertEqual(self.call(getattr(config, 'source_of', None), 'models.review'), 'injected')
        new = self.adopt(scripts={}, run={'platforms': ['wsl']})
        answered = repos.with_test_script(prog, 'scripts/answer.ps1')
        self.assertEqual(answered['scripts']['test'], SCRIPT, 'committed policy wins immediately')
        for sha in (new, old, new):
            current = {}
            answered = self.call(repos.load, answered, sha, sources=current)
            self.assertEqual(answered['_machine_path'], str(machine_path.resolve()))
            expected = 'scripts/answer.ps1' if sha == new else SCRIPT
            self.assertEqual(answered['scripts']['test'], expected)
            self.assertEqual(current['scripts.test'], 'answer:test_script' if sha == new else repo_label)
            self.assertEqual(current['remote'], label)
        inferred = {}
        self.call(repos.load, raw, old, sources=inferred)
        self.assertEqual(inferred['name'], 'machine:' + str((repos.repos_dir() / 'alpha.toml').resolve()))
        # normalize consumes raw origins and retains explicit equal-value attribution.
        seed = {'name': 'injected', 'repo': 'injected', 'remote': 'machine:explicit',
                'scripts.test': 'injected'}
        normalized = self.call(repos.normalize, {**self.machine, 'remote': 'origin', 'scripts': {'test': SCRIPT}}, sources=seed)
        self.assertEqual(normalized['remote'], 'origin')
        self.assertEqual(seed['remote'], 'machine:explicit')
        self.assertIn('review.model', seed)
