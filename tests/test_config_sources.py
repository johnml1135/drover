"""Configuration values and their origins share one public cache boundary."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from drover import config


def derived(dependencies):
    return 'derived:' + json.dumps(dependencies, sort_keys=True, separators=(',', ':'))


class Sources(unittest.TestCase):
    def setUp(self):
        self.addCleanup(config.reset)

    def call(self, name, *args, **kwargs):
        function = getattr(config, name, None)
        self.assertTrue(callable(function), f'config.{name} must expose configuration origins')
        try:
            return function(*args, **kwargs)
        except (TypeError, AttributeError) as exc:
            self.fail(f'config.{name} must support the provenance contract: {exc}')

    def test_layers_and_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / 'config.toml'
            path.write_text('[capacity]\nparallel_cap=4\n'
                            '[decider]\nurl="http://same.invalid"\n'
                            '[hub]\nallowed_logins=[]\n'
                            '[[trackers]]\nname="ram"\nhold_gb=4.0\n'
                            '[[seats]]\nname="only"\nbackend="claude"\ndirector=true\n', encoding='utf-8')
            label = 'machine:' + str(path)
            sources = {'stale': 'obsolete'}
            env = {'DROVER_HOME': str(root), 'DROVER_CAPACITY_PARALLEL_CAP': '4',
                   'DROVER_DECIDER_URL': 'http://generic.invalid', 'DROVER_OLLAMA_URL': 'http://same.invalid'}
            config.use(config.validate({'capacity': {'parallel_cap': 9}}))
            cfg = self.call('load', home_path=root, env=env, sources=sources)
            self.assertNotIn('stale', sources)
            self.assertEqual(cfg['capacity']['parallel_cap'], 4)
            self.assertEqual(sources['capacity.parallel_cap'], 'env:DROVER_CAPACITY_PARALLEL_CAP')
            self.assertEqual(cfg['decider']['url'], 'http://same.invalid')
            self.assertEqual(sources['decider.url'], 'env:DROVER_OLLAMA_URL')
            self.assertEqual(sources['hub.allowed_logins'], label)
            self.assertEqual(sources['hub.ui_port'], 'default')
            self.assertEqual([s['name'] for s in cfg['seats']], ['only'])
            self.assertEqual(sources['seats.0.name'], label)
            self.assertEqual(sources['seats.0.priority'], 'default')
            for key in ('placement', 'binary'):
                self.assertEqual(sources['seats.0.' + key], derived({'seats.0.backend': label}))
            self.assertEqual(sources['seats.0.director_reserve_pct'],
                             derived({'seats.0.backend': label, 'seats.0.director': label}))
            self.assertEqual(sources['trackers.1.hold_gb'], label)
            self.assertEqual(sources['trackers.1.queue_base_gb'], 'default')
            self.assertEqual(sources['trackers.0.queue_per_core'], 'default')
            # Isolated load never installs its values or sources in the cache.
            self.assertEqual(config.get()['capacity']['parallel_cap'], 9)
            self.assertEqual(self.call('source_of', 'capacity.parallel_cap'), 'injected')
            self.call('use', cfg, sources=sources)
            sources['capacity.parallel_cap'] = 'caller mutation'
            self.assertEqual(self.call('source_of', 'capacity.parallel_cap'), 'env:DROVER_CAPACITY_PARALLEL_CAP')
            self.assertNotIn('sources', cfg)
            self.assertNotIn('_sources', config.dumps(cfg))
            preserved = copy.deepcopy(sources)
            path.write_text('[capacity]\nparallel_cap="invalid"\n', encoding='utf-8')
            with self.assertRaises(config.ConfigError):
                self.call('load', home_path=root, env={}, sources=sources)
            self.assertEqual(sources, preserved, 'unsuccessful load must leave the caller map intact')
            path.write_text('[capacity]\nparallel_cap=6\n', encoding='utf-8')
            with mock.patch.dict(os.environ, {'DROVER_HOME': str(root)}, clear=True):
                config.reset()
                self.assertEqual(config.get()['capacity']['parallel_cap'], 6)
                self.assertEqual(self.call('source_of', 'capacity.parallel_cap'), label)
                path.write_text('[capacity]\nparallel_cap=7\n', encoding='utf-8')
                self.assertEqual(config.get()['capacity']['parallel_cap'], 6)
                config.reset()
                self.assertEqual(config.get()['capacity']['parallel_cap'], 7)
                self.assertEqual(self.call('source_of', 'capacity.parallel_cap'), label)
            config.use(config.validate({}))
            self.assertEqual(self.call('source_of', 'capacity.parallel_cap'), 'injected')
            self.assertEqual(self.call('source_of', 'hub.allowed_logins.0') if config.get()['hub']['allowed_logins']
                             else self.call('source_of', 'hub.allowed_logins'), 'injected')

    def test_selectors_and_aggregate_shape(self):
        cfg = {'run': {'env': {'A.B': 'same', r'C\D': 'slash'}, 'empty': {}},
               'items': [{'value': 3}, []], '_machine': {'hidden': 'secret'}}
        sources = {r'run.env.A\.B': 'machine:/tmp/one.toml', r'run.env.C\\D': 'repo:entry',
                   'run.empty': 'default', 'items.0.value': 'injected', 'items.1': 'default'}
        result = self.call('explain_value', cfg, 'run', sources)
        expected = {r'run.env.A\.B': 'machine:/tmp/one.toml', r'run.env.C\\D': 'repo:entry', 'run.empty': 'default'}
        self.assertEqual(result, {'key': 'run', 'value': cfg['run'], 'sources': expected})
        self.assertEqual(config.dig(cfg, r'run.env.A\.B'), 'same')
        self.assertEqual(config.dig(cfg, r'run.env.C\\D'), 'slash')
        self.assertEqual(self.call('explain_value', cfg, 'items.1', sources),
                         {'key': 'items.1', 'value': [], 'sources': {'items.1': 'default'}})
        self.call('use', cfg, sources=sources)
        self.assertEqual(self.call('source_of', 'run'),
                         'aggregate:' + json.dumps(expected, sort_keys=True, separators=(',', ':')))
        self.assertEqual(self.call('source_of', r'run.env.A\.B'), 'machine:/tmp/one.toml')
        for selector in ('', '.', 'run..env', 'run.env.', 'run.env.A\\', r'run.env.A\q',
                         'run.missing', 'items.-1', 'items.x', 'items.9', '_machine.hidden'):
            with self.subTest(selector=selector):
                for fn in ('source_of', 'explain_value'):
                    with self.assertRaises(config.ConfigError) as caught:
                        if fn == 'source_of':
                            self.call(fn, selector)
                        else:
                            self.call(fn, cfg, selector, sources)
                    self.assertIn(selector, str(caught.exception))

    def test_tracker_kind_replacement_drops_obsolete_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            path.write_text('[[trackers]]\nname="ram"\nkind="ram"\nenabled=false\nqueue_base_gb=9.0\n'
                            '[[trackers]]\nname="ram"\nkind="disk"\nqueue_gb=200.0\n', encoding='utf-8')
            sources = {}
            cfg = self.call('load', home_path=directory, env={}, sources=sources)
            self.assertEqual(cfg['trackers'][1]['kind'], 'disk')
            self.assertNotIn('queue_base_gb', cfg['trackers'][1])
            self.assertNotIn('trackers.1.queue_base_gb', sources)
            self.assertEqual(sources['trackers.1.enabled'], 'default')
