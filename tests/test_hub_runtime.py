"""Immutable exports and the separate candidate/recovery trust boundaries."""
import hashlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import stat
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from functools import lru_cache
from hub.runtime import ReleaseError, prepare_release, check_eligibility, check_recovery, child_command
from hub.runtime import _blob_digests
from tests.gitfix import sh, _remove_tree

INSTANCE = '10000000-0000-4000-8000-000000000001'

def digest(data):
    return hashlib.sha256(data).hexdigest()


def git(source, *args):
    return sh('git', '-C', str(source), *args, text=False).stdout


def setUpModule():
    global _source_template, _source_sha
    root = Path(tempfile.mkdtemp(prefix='Runtime template '))
    unittest.addModuleCleanup(_remove_tree, root)
    _source_template = root / 'source checkout'
    _source_template.mkdir()
    git(_source_template, 'init', '--object-format=sha1', '-b', 'main')
    git(_source_template, 'config', 'user.email', 'test@example.invalid')
    git(_source_template, 'config', 'user.name', 'Runtime Test')
    git(_source_template, 'config', 'commit.gpgsign', 'false')
    for name, content in {
        'hub/__init__.py': '', 'hub/__main__.py': 'raise RuntimeError("no import")\n',
        'hub/pyproject.toml': '[project]\nname="fixture"\n', 'hub/uv.lock': 'version=1\n',
        'drover/ledger/schema.py': 'raise RuntimeError("no schema import")\n',
        'hub/runtime.py': '# frozen runtime\n',
        'hub/reload_protocol.py': '# frozen protocol\n',
        'hub/scripts/Install-Drover.ps1': '# install\n',
        'hub/scripts/Install-DroverHub.ps1': '# install hub\n',
        '.gitattributes': '* text=auto eol=lf\n',
    }.items():
        path = _source_template / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8', newline='\n')
    git(_source_template, 'add', '.')
    git(_source_template, 'commit', '-m', 'fixture')
    _source_sha = git(_source_template, 'rev-parse', 'HEAD').decode().strip()


class _RuntimeBase(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('hub.runtime._blob_digests', lru_cache()(_blob_digests)))
        self.temp = tempfile.TemporaryDirectory(prefix='Runtime spaces ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source checkout'
        # Each test mutates a private copy; the committed template stays unchanged.
        shutil.copytree(_source_template, self.source)
        self.a = _source_sha
        self.python = self.root / 'existing venv' / 'Scripts' / 'python.exe'
        self.python.parent.mkdir(parents=True)
        self.python.write_bytes(b'existing interpreter')
        # Bootstrap digest is a canonical map including absent future peers.
        bootstrap = {name: (digest((self.source / name).read_bytes())
                           if (self.source / name).exists() else None) for name in (
            'hub/supervisor.py', 'hub/runtime.py', 'hub/reload_protocol.py',
            'hub/reload_machine.py', 'hub/scripts/Start-DroverHub.ps1',
            'hub/scripts/Install-Drover.ps1', 'hub/scripts/Install-DroverHub.ps1')}
        self.enrolled = dict(
            protocol=1, run_format=1, source_root=str(self.source),
            common_dir=str(self.source / '.git'), default_branch='main',
            interpreter={'path': str(self.python), 'identity': digest(self.python.read_bytes())},
            paths={k: str(self.root / 'owner state' / k) for k in
                   ('ledger', 'runs', 'repo_config', 'config_home', 'token', 'overlay')},
            state_root=str(self.root / 'owner state' / 'hub-runtime'),
            ledger_schema=4, required_schema=4,
            bootstrap_digest=digest(json.dumps(bootstrap, sort_keys=True,
                                              separators=(',', ':')).encode()))
        self.release_root = self.root / 'owner state' / 'hub-runtime' / 'releases'


    def write(self, name, content):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8', newline='\n')


    def commit(self):
        git(self.source, 'add', '.')
        git(self.source, 'commit', '-m', 'fixture')
        return git(self.source, 'rev-parse', 'HEAD').decode().strip()


    def baseline(self):
        release = prepare_release(
            self.source, self.a, self.release_root, self.enrolled,
            first_enrollment=True,
            baseline_probe=lambda path, enrolled: {'ledger_schema': 4, 'required_schema': 4})
        self.enrolled['baseline'] = {k: release[k] for k in
                                     ('sha', 'release_path', 'manifest_digest')}
        return release


    def receipt(self, release):
        return dict(sha=release['sha'], manifest_digest=release['manifest_digest'],
                    instance=INSTANCE, healthy_at='2026-10-17T17:00:00Z')


    def reseal(self, release):
        release['manifest_digest'] = digest(json.dumps(
            {k: v for k, v in release.items() if k != 'manifest_digest'},
            sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode())
        (Path(release['release_path']) / 'manifest.json').write_text(json.dumps(release))


class RuntimeTests(_RuntimeBase):
    def test_exact_sha_export_preserves_source_and_reuses_only_identical_release(self):
        release = self.baseline()
        self.write('hub/app.py', 'candidate\n')
        b = self.commit()
        self.write('hub/app.py', 'dirty owner content\n')
        (self.source / 'untracked').write_text('private')
        status = git(self.source, 'status', '--porcelain')
        reused = prepare_release(self.source, self.a, self.release_root, self.enrolled)
        self.assertEqual(release, reused)
        path = Path(release['release_path'])
        self.assertFalse((path / 'hub/app.py').exists())
        self.assertFalse((path / 'untracked').exists())
        self.assertFalse((path / '.git').exists())
        self.assertEqual((path / 'hub/__main__.py').read_bytes(),
                         git(self.source, 'show', self.a + ':hub/__main__.py'))
        self.assertEqual(git(self.source, 'rev-parse', 'HEAD').decode().strip(), b)
        self.assertEqual(git(self.source, 'status', '--porcelain'), status)
        self.assertEqual((release['ledger_schema'], release['required_schema']), (4, 4))


    def test_schema_change_is_pending(self):
        self.baseline()
        self.write('drover/ledger/schema.py', '# even a comment is a change\n')
        b = self.commit()
        result = check_eligibility(self.enrolled, self.a, b, ledger_schema=4)
        self.assertEqual(result, {'eligible': False, 'reasons': ['schema-change']})


    def test_candidate_contract_changes_are_pending(self):
        self.baseline()
        for name, reason in (
            ('hub/uv.lock', 'dependency-change'),
            ('hub/pyproject.toml', 'dependency-change'),
            ('hub/runtime.py', 'bootstrap-change'),
            ('hub/reload_protocol.py', 'bootstrap-change'),
            ('hub/supervisor.py', 'bootstrap-change'),
            ('hub/reload_machine.py', 'bootstrap-change'),
            ('hub/scripts/Start-DroverHub.ps1', 'bootstrap-change'),
            ('hub/scripts/Install-Drover.ps1', 'bootstrap-change'),
            ('hub/scripts/Install-DroverHub.ps1', 'bootstrap-change'),
        ):
            with self.subTest(name=name):
                git(self.source, 'reset', '--hard', self.a)  # Our disposable fixture only.
                self.write(name, '# changed contract\n')
                b = self.commit()
                self.assertEqual(check_eligibility(self.enrolled, self.a, b, 4)['reasons'], [reason])
                with self.assertRaisesRegex(ReleaseError, reason):
                    prepare_release(self.source, b, self.release_root, self.enrolled)


    def test_actual_schema_required(self):
        self.baseline()
        for value in (None, True, False, 3, 5, '4', -1):
            with self.subTest(value=value):
                self.assertEqual(check_eligibility(self.enrolled, self.a, self.a, value)['reasons'],
                                 ['actual-schema-mismatch'])
        self.assertEqual(check_eligibility(self.enrolled, self.a, self.a)['reasons'],
                         ['actual-schema-mismatch'])
        self.assertTrue(check_eligibility(self.enrolled, self.a, self.a, 4)['eligible'])


    def test_enrollment_required_schema_cannot_be_guessed(self):
        release = self.baseline()
        for value in (None, True, 3, '4', 'missing'):
            with self.subTest(value=value):
                enrolled = copy.deepcopy(self.enrolled)
                if value == 'missing':
                    enrolled.pop('required_schema')
                else:
                    enrolled['required_schema'] = value
                self.assertEqual(check_eligibility(enrolled, self.a, self.a, 4)['reasons'],
                                 ['enrollment-schema-mismatch'])
                self.assertEqual(check_recovery(enrolled, release, self.receipt(release))['reasons'],
                                 ['enrollment-schema-mismatch'])
                with self.assertRaisesRegex(ReleaseError, 'enrollment-schema-mismatch'):
                    prepare_release(self.source, self.a, self.release_root, enrolled)
        forged = copy.deepcopy(self.enrolled)
        forged.update(ledger_schema=3, required_schema=4)
        self.assertFalse(check_eligibility(forged, self.a, self.a, 3)['eligible'])
        self.assertFalse(check_recovery(forged, release, self.receipt(release))['eligible'])


    def test_first_export_needs_independent_matching_probe(self):
        with self.assertRaisesRegex(ReleaseError, 'missing-schema-anchor'):
            prepare_release(self.source, self.a, self.release_root, self.enrolled)
        with self.assertRaisesRegex(ReleaseError, 'baseline-probe-required'):
            prepare_release(self.source, self.a, self.release_root, self.enrolled, first_enrollment=True)
        for proof in ({'ledger_schema': 3, 'required_schema': 4},
                      {'ledger_schema': 4}, {'ledger_schema': 4, 'required_schema': True}):
            with self.subTest(proof=proof), self.assertRaisesRegex(ReleaseError, 'baseline-schema-mismatch'):
                prepare_release(self.source, self.a, self.release_root, self.enrolled,
                                first_enrollment=True, baseline_probe=lambda *_: proof)
            self.assertFalse((self.release_root / self.a).exists())


    def test_probe_cannot_publish_modified_or_added_release_content(self):
        for action in ('modify', 'add'):
            with self.subTest(action=action):
                def probe(path, enrolled):
                    target = path / ('hub/__main__.py' if action == 'modify' else 'unexpected.py')
                    target.write_text('changed by probe')
                    return {'ledger_schema': 4, 'required_schema': 4}
                with self.assertRaisesRegex(ReleaseError, 'retained-content-mismatch'):
                    prepare_release(self.source, self.a, self.release_root, self.enrolled,
                                    first_enrollment=True, baseline_probe=probe)
                self.assertFalse((self.release_root / self.a).exists())


    def test_first_enrollment_requires_clean_exact_source(self):
        self.write('hub/app.py', 'dirty')
        with self.assertRaisesRegex(ReleaseError, 'source-dirty'):
            self.baseline()


    def test_retained_a_recovers_with_source_b_c_or_dirty(self):
        release = self.baseline()
        for content in ('B', 'C', 'dirty'):
            self.write('hub/app.py', content)
            target = self.commit() if content != 'dirty' else target
            # Any source Git probe on recovery is a failure, even an innocuous one.
            with patch('hub.runtime._git', side_effect=AssertionError('recovery used Git')), \
                    patch('hub.runtime.subprocess.run', side_effect=AssertionError('recovery spawned')):
                result = check_recovery(self.enrolled, release, self.receipt(release))
            self.assertEqual(result, {'eligible': True, 'reasons': []})
            self.assertEqual(check_eligibility(self.enrolled, self.a, self.a, 4)['reasons'],
                             ['source-dirty' if content == 'dirty' else 'source-head-mismatch'])
            if content != 'dirty':
                self.assertTrue(check_eligibility(self.enrolled, self.a, target, 4)['eligible'])
        self.assertEqual(list(self.release_root.iterdir()), [Path(release['release_path'])])


    def test_non_descendant_and_wrong_checkout_or_branch_refused(self):
        self.baseline()
        git(self.source, 'checkout', '--orphan', 'other-history')
        self.write('hub/app.py', 'unrelated')
        unrelated = self.commit()
        git(self.source, 'branch', '-f', 'main', unrelated)
        git(self.source, 'checkout', 'main')
        self.assertEqual(check_eligibility(self.enrolled, self.a, unrelated, 4)['reasons'], ['not-descendant'])
        wrong = copy.deepcopy(self.enrolled)
        wrong['common_dir'] = str(self.root / 'other.git')
        self.assertEqual(check_eligibility(wrong, self.a, unrelated, 4)['reasons'], ['wrong-source-checkout'])
        wrong = copy.deepcopy(self.enrolled)
        wrong['source_root'] = str(self.source / 'hub')
        self.assertEqual(check_eligibility(wrong, self.a, unrelated, 4)['reasons'], ['wrong-source-checkout'])
        git(self.source, 'checkout', 'other-history')
        self.assertEqual(check_eligibility(self.enrolled, self.a, unrelated, 4)['reasons'], ['wrong-default-branch'])


    def test_archive_hazards_and_insufficient_disk_refused(self):
        self.baseline()
        self.write('hub/app.py', 'B')
        b = self.commit()
        original = git(self.source, 'archive', '--format=zip', b)

        def archive_with(name, mode=stat.S_IFREG | 0o644):
            buffer = io.BytesIO()
            with zipfile.ZipFile(io.BytesIO(original)) as old, zipfile.ZipFile(buffer, 'w') as new:
                for entry in old.infolist():
                    if entry.filename != name:
                        new.writestr(entry, old.read(entry))
                entry = zipfile.ZipInfo(name)
                # Preserve malformed wire names even on Windows, where the
                # constructor normalizes backslashes before writing the ZIP.
                entry.filename = name
                entry.create_system = 3
                entry.external_attr = mode << 16
                new.writestr(entry, b'evil')
            return buffer.getvalue()

        for name, payload, reason in (
            ('traversal', archive_with('../escape'), 'archive-path'),
            ('absolute', archive_with('/escape'), 'archive-path'),
            ('drive', archive_with('C:/escape'), 'archive-path'),
            ('backslash', archive_with('hub\\escape'), 'archive-path'),
            ('reserved', archive_with('hub/CON.txt'), 'archive-path'),
            ('link', archive_with('hub/link', stat.S_IFLNK | 0o777), 'archive-type'),
            ('fifo', archive_with('hub/fifo', stat.S_IFIFO | 0o600), 'archive-type'),
            ('directory-body', archive_with('hub/', stat.S_IFDIR | 0o755), 'archive-type'),
            ('corruption', b'not a zip', 'archive-corrupt'),
        ):
            with self.subTest(name=name):
                def adapter(source, *args):
                    return payload if args[0] == 'archive' else git(source, *args)
                with self.assertRaisesRegex(ReleaseError, reason):
                    prepare_release(self.source, b, self.release_root, self.enrolled, git=adapter)
                self.assertFalse((self.release_root / b).exists())
        expanded = sum(x.file_size for x in zipfile.ZipFile(io.BytesIO(original)).infolist())
        required = 2 * expanded + 256 * 1024 * 1024
        with self.assertRaisesRegex(ReleaseError, 'insufficient-disk'):
            prepare_release(self.source, b, self.release_root, self.enrolled, disk=lambda _: required - 1)
        exported = prepare_release(self.source, b, self.release_root, self.enrolled, disk=lambda _: required)
        self.assertTrue(Path(exported['release_path']).is_dir())


    def test_archive_raw_paths_refused_before_zip_normalization(self):
        self.baseline()
        self.write('hub/app.py', 'B')
        b = self.commit()
        original = git(self.source, 'archive', '--format=zip', b)
        for raw_name in ('hub\\__main__.py', 'hub/__main__.py\x00ignored'):
            with self.subTest(raw_name=raw_name):
                buffer = io.BytesIO()
                with zipfile.ZipFile(io.BytesIO(original)) as old, zipfile.ZipFile(buffer, 'w') as new:
                    for entry in old.infolist():
                        data = old.read(entry)
                        if entry.filename == 'hub/__main__.py':
                            entry.filename = raw_name
                        new.writestr(entry, data)
                payload = buffer.getvalue()
                def adapter(source, *args):
                    return payload if args[0] == 'archive' else git(source, *args)
                # Exercise Windows reader normalization on every platform.
                with patch('zipfile.os.sep', '\\'):
                    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                        entry = next(x for x in archive.infolist() if x.orig_filename == raw_name)
                        self.assertEqual(entry.filename, 'hub/__main__.py')
                    with self.assertRaisesRegex(ReleaseError, 'archive-path'):
                        prepare_release(self.source, b, self.release_root, self.enrolled, git=adapter)
                self.assertFalse((self.release_root / b).exists())


    def test_archive_is_exact_despite_git_export_attributes(self):
        self.baseline()
        for attribute, reason in (('export-ignore', 'archive-tree-mismatch'),
                                  ('export-subst', 'archive-blob-mismatch')):
            with self.subTest(attribute=attribute):
                self.write('.gitattributes', '* text=auto eol=lf\nhub/app.py ' + attribute + '\n')
                self.write('hub/app.py', '$Format:%H$\n')
                b = self.commit()
                with self.assertRaisesRegex(ReleaseError, reason):
                    prepare_release(self.source, b, self.release_root, self.enrolled)
