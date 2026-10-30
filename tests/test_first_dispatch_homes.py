"""First dispatch seat-home resolution, validation and hub startup regressions."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from drover import config, directors, dispatcher, launch
from tests.test_dispatcher import Base


class SeatHomes(unittest.TestCase):
    def test_config_load_preserves_explicit_tilde_without_probing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'config.toml').write_text('[[seats]]\nname="owner"\nbackend="claude"\ndirector=true\n'
                                            'config_dir="~/.custom"\n')
            with patch('drover.seat_paths.resolve', side_effect=AssertionError('must resolve at use time')):
                cfg = config.load(root, {})
            self.assertEqual(cfg['seats'][0]['config_dir'], '~/.custom')

    def test_validation_uses_effective_distro_for_expansion_and_existence(self):
        from drover import placement, seat_paths
        cfg = config.validate({'paths': {'wsl_distro': 'Ubuntu'}, 'seats': [
            {'name': 'owner', 'backend': 'claude', 'director': True, 'placement': 'wsl', 'config_dir': '~/.codex'}]})
        previous = placement.bind_settings(lambda: {'wsl_distro': 'Debian'})
        self.addCleanup(placement.bind_settings, previous)
        calls = []
        def run(argv, **kw):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout='/home/debian/.codex\n' if argv[-1].startswith('~') else '1\n')
        self.assertEqual(seat_paths.validate(cfg, windows=True, run=run), {})
        self.assertEqual([c[:3] for c in calls], [['wsl.exe', '-d', 'Debian']] * 2)
        self.assertEqual(calls[-1][-1], '/home/debian/.codex')
        self.assertEqual(cfg['seats'][0]['config_dir'], '~/.codex')

    def test_wsl_home_comes_from_wsl_not_windows(self):
        from drover import seat_paths
        calls = []
        def run(argv, **kw):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout='/srv/wsl-account/.codex\n')
        seat = {'placement': 'wsl'}
        with patch('os.path.expanduser', side_effect=AssertionError('Windows home must not be used')):
            home = seat_paths.resolve('~/.codex', seat, {'wsl_distro': 'Ubuntu'}, windows=True, run=run)
        self.assertEqual(home, '/srv/wsl-account/.codex')
        self.assertIn('Ubuntu', calls[0])
        self.assertEqual(calls[0][-1], '~/.codex')

    def test_missing_home_health_identifies_seat_and_platform(self):
        from drover import seat_paths
        cfg = config.validate({'seats': [{'name': 'owner', 'backend': 'claude', 'director': True,
                                          'placement': 'wsl', 'config_dir': '/srv/missing'}]})
        calls = []
        def run(argv, **kw):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout='0\n')
        errors = seat_paths.validate(cfg, windows=True, run=run)
        self.assertIn('owner', errors)
        self.assertIn('wsl', errors['owner'])
        self.assertIn('/srv/missing', errors['owner'])
        self.assertEqual(calls[0][-1], '/srv/missing')

    def test_local_home_validation_and_failed_remote_probe(self):
        from drover import seat_paths
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config.validate({'seats': [{'name': 'owner', 'backend': 'claude', 'director': True,
                                              'placement': 'wsl', 'config_dir': tmp}]})
            self.assertEqual(seat_paths.validate(cfg, windows=False), {})
            errors = seat_paths.validate(cfg, windows=True, run=lambda *a, **k: SimpleNamespace(returncode=1, stdout=''))
            self.assertIn('owner', errors)


class SeatHomeConsumers(Base):
    def setUp(self):
        super().setUp()
        from drover import placement
        root = Path(self.tmp.name)
        (root / 'config.toml').write_text(
            '[paths]\nwsl_distro="Ubuntu"\n'
            '[[seats]]\nname="owner"\nbackend="claude"\ndirector=true\npriority=1\n'
            'placement="wsl"\nconfig_dir="~/.claude-owner"\n'
            '[[seats]]\nname="coder"\nbackend="codex"\npriority=2\nplacement="wsl"\ncodex_home="~/.codex-coder"\n')
        self.cfg = config.load(root, {})
        config.use(self.cfg)
        self.addCleanup(config.reset)
        self.port.set_setting('wsl_distro', 'Debian')
        previous = placement.bind_settings(lambda: {'wsl_distro': self.port.get_setting('wsl_distro')})
        self.addCleanup(placement.bind_settings, previous)

    def capture_launch(self, seat, backend, role, launch_through):
        calls, specs = [], []
        def run(argv, **kw):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout='/home/debian/home\n')
        launcher = launch.RunnerLauncher(runs_dir=self.tmp.name, distro='Ubuntu', windows=True, run=run)
        def command(spec, *args):
            specs.append(spec)
            raise RuntimeError('captured before detach')
        spec = {'run_id': 99, 'backend': backend, 'model': 'm', 'seat': seat['name'], 'placement': 'wsl',
                'role': role, 'write': False, 'env': config.seat_env(seat), 'cwd': '/repo', 'user_prompt': 'test',
                'system_append': 'test'}
        with patch.object(launcher, 'command', side_effect=command), self.assertRaisesRegex(RuntimeError, 'captured'):
            launch_through(launcher, spec)
        return specs[0]['env'], calls

    def test_launcher_resolves_home_with_ledger_distro(self):
        env, calls = self.capture_launch(self.cfg['seats'][1], 'codex', 'worker', lambda l, s: l.launch(s))
        self.assertEqual(env, {'CODEX_HOME': '/home/debian/home'})
        self.assertEqual(calls[0][:3], ['wsl.exe', '-d', 'Debian'])
        self.assertEqual(calls[0][-1], '~/.codex-coder')

    def test_director_launch_on_wsl_seat_gets_resolved_home(self):
        class Executor:
            capabilities = {}
            def launch(self, launcher, spec, credential, guard):
                return launcher.launch(spec)
        adapter_for = lambda l: directors.RunnerDirectorAdapter(l, Executor())
        env, calls = self.capture_launch(self.cfg['seats'][0], 'claude', 'director',
                                         lambda l, s: adapter_for(l).launch(s, 'credential', lambda: None))
        self.assertEqual(env, {'CLAUDE_CONFIG_DIR': '/home/debian/home'})
        self.assertEqual(calls[0][:3], ['wsl.exe', '-d', 'Debian'])
        self.assertEqual(calls[0][-1], '~/.claude-owner')

    def test_usage_resolves_each_wsl_seat_inside_effective_distro(self):
        from drover import usage
        now = self.d.now()
        calls = []
        raw = json.dumps({'primary': {'used_percent': 12, 'window_minutes': 10080, 'resets_at': now + 86400},
                          'secondary': None})
        def run(argv, **kw):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout=json.dumps(raw))
        poller = usage.Poller(self.conn, run=run, now=lambda: now, windows=True)
        seats = [self.cfg['seats'][1], {**self.cfg['seats'][1], 'name': 'spare', 'codex_home': '~/.codex-spare'}]
        for seat in seats:
            self.assertEqual(poller.refresh(seat)['remaining_pct'], 88)
        self.assertEqual([c[:3] for c in calls], [['wsl.exe', '-d', 'Debian']] * 2)
        self.assertEqual([c[-1] for c in calls], ['~/.codex-coder', '~/.codex-spare'])
        self.assertTrue(all('os.path.expanduser(sys.argv[2])' in c[c.index('-c') + 1] for c in calls))


class Health(unittest.TestCase):
    def test_health_and_settings_surface_admission_and_missing_homes(self):
        from hub.store import Store, UiView
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'ledger.db')
            try:
                report = {'held': True, 'backends': {'codex': [{'seat': 'codex', 'reason': 'below reserve'}]}}
                from drover.ledger import ops
                ops.set_setting(store.conn, 'admission_hold', json.dumps(report))
                ops.set_setting(store.conn, 'seat_health', json.dumps({'codex': 'missing home'}))
                health = UiView(store).health()
                self.assertEqual(health['admission_hold'], report)
                self.assertEqual(health['seat_health'], {'codex': 'missing home'})
                self.assertIn('admission_hold', store.get_settings())
                from hub.web import templates
                html = templates.get_template('health.html').render(h=health, partial=True,
                        dispatch={'label': 'paused', 'enabled': False})
                self.assertIn('Admission held', html)
                self.assertIn('below reserve', html)
                self.assertIn('missing home', html)
            finally:
                store.close()


class Startup(Base):
    def test_missing_seat_home_pauses_at_hub_startup(self):
        from hub.dispatch import DispatcherService
        settings = SimpleNamespace(dispatcher_lock=Path(self.tmp.name) / 'dispatcher.lock', runs_dir=self.tmp.name,
                                   distro='Ubuntu')
        service = DispatcherService(SimpleNamespace(get_settings=lambda: {}), settings, landing_service=object())
        with patch('drover.seat_paths.validate', return_value={'codex': 'home does not exist in WSL'}), \
                patch('hub.dispatch.Dispatcher'):
            service._default_dispatcher(self.port)
        self.assertEqual(self.port.get_setting('dispatch_enabled'), 'false')
        self.assertIn('codex', json.loads(self.port.get_setting('seat_health')))
        self.assertEqual(len(self.events('seat-home-error')), 1)

    def test_distro_changes_revalidate_during_loop_and_clearing_keeps_owner_pause(self):
        from hub.dispatch import DispatcherService
        choices = {'wsl_distro': 'Ubuntu'}
        store = SimpleNamespace(get_settings=lambda: choices)
        settings = SimpleNamespace(dispatcher_lock=Path(self.tmp.name) / 'dispatcher.lock', distro='Ubuntu',
                                   dispatch_interval_s=0)
        service = DispatcherService(store, settings, make_dispatcher=lambda port: self.d)
        checks = []
        def validate(cfg, *, distro):
            checks.append(distro)
            return {'codex': 'home does not exist in Debian'} if distro == 'Debian' else {}
        ticks = []
        def tick():
            ticks.append(json.loads(self.port.get_setting('seat_health')))
            if len(ticks) == 1:
                choices['wsl_distro'] = 'Debian'
            elif len(ticks) == 2:
                self.assertEqual(self.port.get_setting('dispatch_enabled'), 'false')
            elif len(ticks) == 3:
                choices['wsl_distro'] = 'Ubuntu'
                self.port.set_setting('dispatch_enabled', 'false')  # owner keeps dispatch paused
            else:
                service._stop.set()
        with patch('hub.dispatch.HubPort', return_value=self.port), \
                patch('drover.seat_paths.validate', side_effect=validate), patch.object(self.d, 'tick', side_effect=tick):
            service._loop()
        self.assertIsNone(service.error)
        self.assertEqual(checks, ['Ubuntu', 'Debian', 'Ubuntu'])
        self.assertEqual(ticks, [{}, {'codex': 'home does not exist in Debian'},
                                 {'codex': 'home does not exist in Debian'}, {}])
        self.assertEqual(len(self.events('seat-home-error')), 1)
        self.assertEqual(len(self.events('seat-home-cleared')), 1)
        self.assertEqual(self.port.get_setting('dispatch_enabled'), 'false')
