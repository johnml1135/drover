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

from hub.runtime import ReleaseError, prepare_release, check_eligibility, check_recovery, child_command


INSTANCE = '10000000-0000-4000-8000-000000000001'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def git(source, *args):
    return subprocess.run(['git', '-C', str(source), *args], check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='Runtime spaces ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source checkout'
        self.source.mkdir()
        git(self.source, 'init', '--object-format=sha1', '-b', 'main')
        git(self.source, 'config', 'user.email', 'test@example.invalid')
        git(self.source, 'config', 'user.name', 'Runtime Test')
        git(self.source, 'config', 'commit.gpgsign', 'false')
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
            self.write(name, content)
        self.a = self.commit()
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

    def test_retained_required_schema_rejected_even_with_new_digest(self):
        release = self.baseline()
        original = (Path(release['release_path']) / 'manifest.json').read_bytes()
        for value in (None, True, 3, 'missing'):
            with self.subTest(value=value):
                forged = copy.deepcopy(release)
                if value == 'missing':
                    forged.pop('required_schema')
                else:
                    forged['required_schema'] = value
                self.reseal(forged)
                result = check_recovery(self.enrolled, forged, self.receipt(forged))
                self.assertEqual(result['reasons'], ['release-schema-mismatch'])
                (Path(release['release_path']) / 'manifest.json').write_bytes(original)

    def reseal(self, release):
        release['manifest_digest'] = digest(json.dumps(
            {k: v for k, v in release.items() if k != 'manifest_digest'},
            sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode())
        (Path(release['release_path']) / 'manifest.json').write_text(json.dumps(release))

    def test_retained_content_and_manifest_corruption_refused_without_repair(self):
        release = self.baseline()
        path = Path(release['release_path'])
        file = path / 'hub/__main__.py'
        original = file.read_bytes()
        file.write_bytes(b'corrupt')
        self.assertEqual(check_recovery(self.enrolled, release, self.receipt(release))['reasons'],
                         ['retained-content-mismatch'])
        with self.assertRaisesRegex(ReleaseError, 'retained-content-mismatch'):
            prepare_release(self.source, self.a, self.release_root, self.enrolled)
        self.assertEqual(file.read_bytes(), b'corrupt')
        file.write_bytes(original)
        manifest = path / 'manifest.json'
        manifest.write_text('{"broken": true}')
        self.assertEqual(check_recovery(self.enrolled, release, self.receipt(release))['reasons'], ['manifest-corrupt'])
        self.assertTrue(path.exists())

    def test_non_object_baseline_manifest_returns_corruption_reason(self):
        release = self.baseline()
        path = Path(release['release_path']) / 'manifest.json'
        for value in ('[]', 'null', '"text"', '4'):
            with self.subTest(value=value):
                path.write_text(value)
                self.assertEqual(check_eligibility(self.enrolled, self.a, self.a, 4)['reasons'],
                                 ['manifest-corrupt'])
                self.assertEqual(check_recovery(self.enrolled, release, self.receipt(release))['reasons'],
                                 ['manifest-corrupt'])

    def test_broken_compressed_stream_is_an_explicit_archive_refusal(self):
        self.baseline()
        self.write('hub/app.py', 'B')
        b = self.commit()
        buffer = io.BytesIO()
        original = git(self.source, 'archive', '--format=zip', b)
        with zipfile.ZipFile(io.BytesIO(original)) as old, zipfile.ZipFile(buffer, 'w') as new:
            for entry in old.infolist():
                new.writestr(entry, old.read(entry), compress_type=zipfile.ZIP_DEFLATED)
        payload = bytearray(buffer.getvalue())
        with zipfile.ZipFile(buffer) as archive:
            offset = archive.getinfo('hub/__main__.py').header_offset
        name_len, extra_len = struct.unpack_from('<HH', payload, offset + 26)
        payload[offset + 30 + name_len + extra_len] = 7  # Reserved DEFLATE block type.
        def adapter(source, *args):
            return bytes(payload) if args[0] == 'archive' else git(source, *args)
        with self.assertRaisesRegex(ReleaseError, 'archive-corrupt'):
            prepare_release(self.source, b, self.release_root, self.enrolled, git=adapter)
        self.assertFalse((self.release_root / b).exists())

    def test_retained_directory_reparse_points_are_refused(self):
        release = self.baseline()
        retained_hub = Path(release['release_path']) / 'hub'
        original_lstat = Path.lstat
        def metadata(path, *args, **kwargs):
            value = original_lstat(path, *args, **kwargs)
            if path != retained_hub:
                return value
            class ReparsePoint:
                st_file_attributes = 0x400
                def __getattr__(self, name):
                    return getattr(value, name)
            return ReparsePoint()
        # Simulate Windows junction metadata without requiring junction rights.
        with patch.object(Path, 'lstat', metadata):
            self.assertEqual(check_recovery(self.enrolled, release, self.receipt(release))['reasons'],
                             ['retained-content-type'])

    def test_retained_contract_mismatch_refused(self):
        release = self.baseline()
        original = (Path(release['release_path']) / 'manifest.json').read_bytes()
        for field, value, reason in (
            ('protocol', 2, 'protocol-mismatch'), ('run_format', 2, 'run-format-mismatch'),
            ('ledger_schema', 3, 'release-schema-mismatch'),
            ('schema_digest', 'd' * 64, 'retained-contract-mismatch'),
            ('dependency_digests', {'hub/uv.lock': 'd' * 64, 'hub/pyproject.toml': 'e' * 64}, 'retained-contract-mismatch'),
            ('bootstrap_digest', 'd' * 64, 'retained-contract-mismatch'),
            ('paths', {**release['paths'], 'token': str(self.root / 'other-token')}, 'pinned-path-mismatch'),
            ('interpreter', {**release['interpreter'], 'identity': 'different'}, 'pinned-path-mismatch'),
        ):
            with self.subTest(field=field):
                forged = copy.deepcopy(release)
                forged[field] = value
                self.reseal(forged)
                self.assertEqual(check_recovery(self.enrolled, forged, self.receipt(forged))['reasons'], [reason])
                (Path(release['release_path']) / 'manifest.json').write_bytes(original)
        receipt = self.receipt(release)
        receipt['sha'] = 'b' * 40
        self.assertEqual(check_recovery(self.enrolled, release, receipt)['reasons'], ['unapproved-release'])
        changed = {**release, 'tree_digest': 'd' * 64}
        self.assertEqual(check_recovery(self.enrolled, changed, self.receipt(release))['reasons'], ['manifest-digest-mismatch'])
        self.assertTrue(check_recovery(self.enrolled, release, self.enrolled['baseline'])['eligible'])

    def test_retained_consistent_contract_still_must_match_enrollment(self):
        release = self.baseline()
        enrolled = copy.deepcopy(self.enrolled)
        enrolled.pop('baseline')
        enrolled.update(schema_digest=release['schema_digest'],
                        dependency_digests=release['dependency_digests'])
        for name, contract, reason in (
            ('drover/ledger/schema.py', 'schema_digest', 'schema-change'),
            ('hub/uv.lock', 'dependency_digests', 'dependency-change'),
        ):
            with self.subTest(name=name):
                path = Path(release['release_path']) / name
                original = path.read_bytes()
                path.write_bytes(b'changed retained contract')
                forged = copy.deepcopy(release)
                forged['files'][name] = digest(path.read_bytes())
                forged['content_digest'] = digest(json.dumps(forged['files'], sort_keys=True,
                                                              separators=(',', ':')).encode())
                if contract == 'schema_digest':
                    forged[contract] = forged['files'][name]
                else:
                    forged[contract][name] = forged['files'][name]
                self.reseal(forged)
                self.assertEqual(check_recovery(enrolled, forged, self.receipt(forged))['reasons'], [reason])
                path.write_bytes(original)
                self.reseal(release)

    def test_candidate_and_recovery_support_explicit_verified_contract_anchors(self):
        release = self.baseline()
        enrolled = copy.deepcopy(self.enrolled)
        enrolled.pop('baseline')
        self.assertEqual(check_eligibility(enrolled, self.a, self.a, 4)['reasons'], ['missing-schema-anchor'])
        enrolled.update(schema_digest=release['schema_digest'],
                        dependency_digests=release['dependency_digests'])
        self.assertTrue(check_eligibility(enrolled, self.a, self.a, 4)['eligible'])
        self.assertTrue(check_recovery(enrolled, release, self.receipt(release))['eligible'])
        self.assertEqual(prepare_release(self.source, self.a, self.release_root, enrolled), release)

    def test_interpreter_and_wsl_reachability_required(self):
        release = self.baseline()
        self.python.write_bytes(b'changed interpreter')
        self.assertEqual(check_eligibility(self.enrolled, self.a, self.a, 4)['reasons'], ['interpreter-change'])
        self.assertEqual(check_recovery(self.enrolled, release, self.receipt(release))['reasons'], ['interpreter-change'])
        self.python.write_bytes(b'existing interpreter')
        for probe in (None, lambda _: False, lambda _: True):
            expected = probe is not None and probe(Path(release['release_path']))
            self.assertEqual(check_recovery(self.enrolled, release, self.receipt(release),
                                            wsl_probe=probe, wsl_live_run_ids=[612])['eligible'], expected)
            self.assertEqual(check_eligibility(self.enrolled, self.a, self.a, 4,
                                               wsl_probe=probe, wsl_live_run_ids=[612])['eligible'], expected)

    def test_new_candidate_checks_existing_parent_then_exported_wsl_path(self):
        self.baseline()
        self.write('hub/app.py', 'candidate\n')
        b = self.commit()
        destination = self.release_root / b
        probes = []

        def reachable(path):
            probes.append(path)
            return path.is_dir()

        result = check_eligibility(self.enrolled, self.a, b, 4,
                                   wsl_probe=reachable, wsl_live_run_ids=[612])
        self.assertEqual(result, {'eligible': True, 'reasons': []})
        self.assertEqual(probes, [self.release_root])
        self.assertFalse(destination.exists())
        probes.clear()
        release = prepare_release(self.source, b, self.release_root, self.enrolled,
                                  wsl_probe=reachable, wsl_live_run_ids=[612])
        self.assertEqual(probes, [self.release_root, destination])
        self.assertTrue((destination / 'manifest.json').is_file())
        probes.clear()
        self.assertTrue(check_eligibility(self.enrolled, self.a, b, 4,
                                         wsl_probe=reachable, wsl_live_run_ids=[612])['eligible'])
        self.assertEqual(probes, [destination])
        probes.clear()
        self.assertEqual(prepare_release(self.source, b, self.release_root, self.enrolled,
                                         wsl_probe=reachable, wsl_live_run_ids=[612]), release)
        self.assertEqual(probes, [destination, destination])

    def test_first_enrollment_checks_existing_ancestor_then_exported_wsl_path(self):
        probes = []

        def reachable(path):
            probes.append(path)
            return path.is_dir()

        self.assertFalse(self.release_root.exists())
        release = prepare_release(
            self.source, self.a, self.release_root, self.enrolled,
            first_enrollment=True,
            baseline_probe=lambda *_: {'ledger_schema': 4, 'required_schema': 4},
            wsl_probe=reachable, wsl_live_run_ids=[612])
        destination = Path(release['release_path'])
        self.assertEqual(probes, [self.root, destination])
        self.assertTrue((destination / 'manifest.json').is_file())

    def test_unreachable_wsl_parent_refuses_before_export(self):
        self.baseline()
        self.write('hub/app.py', 'candidate\n')
        b = self.commit()
        probes = []

        def unreachable(path):
            probes.append(path)
            return False

        self.assertEqual(check_eligibility(self.enrolled, self.a, b, 4,
                                          wsl_probe=unreachable, wsl_live_run_ids=[612])['reasons'],
                         ['wsl-release-unreachable'])
        with self.assertRaisesRegex(ReleaseError, 'wsl-release-unreachable'):
            prepare_release(self.source, b, self.release_root, self.enrolled,
                            git=lambda *_: self.fail('unreachable parent must refuse before Git export'),
                            wsl_probe=unreachable, wsl_live_run_ids=[612])
        self.assertEqual(probes, [self.release_root, self.release_root])
        self.assertEqual([path.name for path in self.release_root.iterdir()], [self.a])

    def test_reachable_parent_cannot_authorize_unreachable_exported_wsl_path(self):
        self.baseline()
        self.write('hub/app.py', 'candidate\n')
        b = self.commit()
        destination = self.release_root / b
        probes = []

        def parent_only(path):
            self.assertTrue(path.is_dir())
            probes.append(path)
            return path == self.release_root

        for attempt in ('new export', 'retained reuse'):
            with self.subTest(attempt=attempt):
                probes.clear()
                with self.assertRaisesRegex(ReleaseError, 'wsl-release-unreachable'):
                    prepare_release(self.source, b, self.release_root, self.enrolled,
                                    wsl_probe=parent_only, wsl_live_run_ids=[612])
                self.assertEqual(probes, [self.release_root, destination] if attempt == 'new export'
                                 else [destination])
                self.assertTrue((destination / 'manifest.json').is_file())
        self.assertEqual(check_eligibility(self.enrolled, self.a, b, 4,
                                          wsl_probe=parent_only, wsl_live_run_ids=[612])['reasons'],
                         ['wsl-release-unreachable'])

    def test_recovery_compares_both_paths_with_native_case_rules(self):
        release = self.baseline()
        # Exercise Windows normcase behavior on every platform.
        with patch('hub.runtime.os.path.normcase', side_effect=str.casefold):
            self.assertTrue(check_recovery(self.enrolled, release, self.receipt(release))['eligible'])

    def test_child_command_pins_imports_and_paths_with_spaces(self):
        release = self.baseline()
        argv, env, cwd = child_command(self.enrolled, release, 12, INSTANCE, env={
            'PYTHONPATH': str(self.source), 'PythonPath': 'other source',
            'PYTHONHOME': 'foreign python', 'DROVER_RELOAD_LEDGER': 'foreign ledger',
            'drover_overlay': 'foreign overlay', 'drover_home': 'foreign home',
            'drover_repo_configs_dir': 'foreign configs', 'pythonsafepath': '0',
            'OWNER_SETTING': 'preserved'})
        self.assertEqual(argv, [str(self.python), '-P', '-m', 'hub'])
        self.assertEqual(cwd, release['release_path'])
        self.assertEqual(env['PYTHONPATH'], cwd)
        self.assertNotIn('PythonPath', env)
        self.assertNotIn('PYTHONHOME', env)
        for key in ('drover_overlay', 'drover_home', 'drover_repo_configs_dir', 'pythonsafepath'):
            self.assertNotIn(key, env)
        self.assertEqual(env['PYTHONSAFEPATH'], '1')
        self.assertEqual(env['PYTHONDONTWRITEBYTECODE'], '1')
        self.assertEqual(env['OWNER_SETTING'], 'preserved')
        self.assertEqual(env['DROVER_OVERLAY'], self.enrolled['paths']['overlay'])
        self.assertEqual(env['DROVER_HOME'], self.enrolled['paths']['config_home'])
        self.assertEqual(env['DROVER_REPO_CONFIGS_DIR'], self.enrolled['paths']['repo_config'])
        for name, value in self.enrolled['paths'].items():
            self.assertEqual(env['DROVER_RELOAD_' + name.upper()], value)
        self.assertEqual(env['DROVER_RELOAD_REQUIRED_SCHEMA'], '4')

    def test_expanded_size_limit_refused_before_extraction(self):
        self.baseline()
        self.write('hub/app.py', 'B')
        b = self.commit()
        payload = bytearray(git(self.source, 'archive', '--format=zip', b))
        central = payload.index(b'PK\x01\x02')
        # Advertise a large expansion without allocating or extracting it.
        struct.pack_into('<I', payload, central + 24, 128 * 1024 * 1024 + 1)
        def adapter(source, *args):
            return bytes(payload) if args[0] == 'archive' else git(source, *args)
        with self.assertRaisesRegex(ReleaseError, 'archive-too-large'):
            prepare_release(self.source, b, self.release_root, self.enrolled, git=adapter)
        self.assertFalse((self.release_root / b).exists())

    def test_tracked_link_and_windows_case_collision_refused(self):
        self.baseline()
        # Inject Git tree records so this runs on Windows without symlink rights
        # or a case-sensitive volume, as well as on POSIX.
        original = git(self.source, 'ls-tree', '-r', '-z', '--full-tree', self.a)
        blob = git(self.source, 'rev-parse', self.a + ':hub/__main__.py').strip()
        for extra, reason in (
            (b'120000 blob ' + blob + b'\thub/link\0', 'archive-type'),
            (b'160000 commit ' + self.a.encode() + b'\tsubmodule\0', 'archive-type'),
            (b'100644 blob ' + blob + b'\tHub/other.py\0', 'archive-path-collision'),
        ):
            def adapter(source, *args):
                return original + extra if args[0] == 'ls-tree' else git(source, *args)
            with self.subTest(extra=extra), self.assertRaisesRegex(ReleaseError, reason):
                prepare_release(self.source, self.a, self.release_root, self.enrolled, git=adapter)

    def test_child_package_and_runner_import_roots_are_retained(self):
        self.write('drover/__init__.py', '')
        self.write('drover/launch.py', 'from pathlib import Path\nROOT=Path(__file__).resolve().parent.parent\n')
        self.write('hub/__main__.py',
                   'import json, os\nfrom pathlib import Path\nimport drover.launch\n'
                   'print(json.dumps({"hub":str(Path(__file__).resolve()),'
                   '"runner_root":str(drover.launch.ROOT),"overlay":os.environ["DROVER_OVERLAY"]}))\n')
        self.a = self.commit()
        release = self.baseline()
        self.write('hub/__main__.py', 'raise RuntimeError("mutable source imported")\n')
        self.write('drover/launch.py', 'raise RuntimeError("mutable runner imported")\n')
        argv, env, cwd = child_command(self.enrolled, release, 12, INSTANCE,
                                       env={**os.environ, 'PYTHONPATH': str(self.source)})
        # The installed-interpreter argv is asserted above. Use the preinstalled
        # test Python to execute the same import flags against this fixture.
        process = subprocess.run([sys.executable, *argv[1:]], env=env, cwd=cwd,
                                 capture_output=True, text=True, check=True)
        report = json.loads(process.stdout)
        self.assertEqual(report['hub'], str(Path(cwd) / 'hub/__main__.py'))
        self.assertEqual(report['runner_root'], cwd)
        self.assertEqual(report['overlay'], self.enrolled['paths']['overlay'])

    def test_frozen_module_supports_package_and_sibling_imports_without_application_or_sqlite(self):
        bootstrap = self.root / 'frozen bootstrap'
        bootstrap.mkdir()
        repo = Path(__file__).resolve().parent.parent
        for name in ('runtime.py', 'reload_protocol.py'):
            shutil.copyfile(repo / 'hub' / name, bootstrap / name)
        guard = '''
import sys
class Guard:
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in ('drover', 'sqlite3', 'fastapi', 'uvicorn'):
            raise AssertionError('forbidden import: ' + fullname)
sys.meta_path.insert(0, Guard())
sys.path.insert(0, sys.argv[1])
'''
        for directory, module in ((repo, 'hub.runtime'), (bootstrap, 'runtime')):
            script = guard + '\nimport importlib\nm = importlib.import_module(sys.argv[2])\nassert callable(m.check_recovery)\n'
            result = subprocess.run([sys.executable, '-I', '-B', '-c', script, str(directory), module],
                                    cwd=bootstrap, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
