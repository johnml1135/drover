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


from tests.test_hub_runtime import INSTANCE, digest, git, setUpModule, _RuntimeBase


class RuntimeIntegrityTests(_RuntimeBase):
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


    def test_healthy_b_manifest_numeric_type_corruption_refused_with_baseline_a_intact(self):
        baseline = self.baseline()
        baseline_path = Path(baseline['release_path']) / 'manifest.json'
        baseline_bytes = baseline_path.read_bytes()
        self.write('hub/app.py', 'B')
        b = self.commit()
        release = prepare_release(self.source, b, self.release_root, self.enrolled)
        # Extension fields are not type-checked by the protocol validator;
        # their integrity must also be covered by the recomputed digest.
        release['metadata'] = {'sequence': 1}
        self.reseal(release)
        manifest = Path(release['release_path']) / 'manifest.json'
        receipt = self.receipt(release)
        self.assertTrue(check_recovery(self.enrolled, release, receipt)['eligible'])
        for changes in ({'required_schema': 4.0}, {'ledger_schema': 4.0},
                        {'required_schema': 4.0, 'ledger_schema': 4.0},
                        {'protocol': True}, {'run_format': True},
                        {'metadata': {'sequence': True}}, {'metadata': {'sequence': 1.0}}):
            with self.subTest(changes=changes):
                changed = {**release, **changes}
                # Python dict equality masks all these on-disk corruptions.
                self.assertEqual(changed, release)
                manifest.write_text(json.dumps(changed), encoding='utf-8')
                self.assertEqual(check_recovery(self.enrolled, release, receipt),
                                 {'eligible': False, 'reasons': ['manifest-corrupt']})
                with self.assertRaisesRegex(ReleaseError, 'manifest-corrupt'):
                    child_command(self.enrolled, release, 12, INSTANCE)
                self.assertEqual(baseline_path.read_bytes(), baseline_bytes)


    def test_manifest_special_files_refused_before_open_including_baseline_reads(self):
        baseline = self.baseline()
        self.write('hub/app.py', 'B')
        b = self.commit()
        release = prepare_release(self.source, b, self.release_root, self.enrolled)
        original_lstat, original_read = Path.lstat, Path.read_text
        for selected in (baseline, release):
            manifest = Path(selected['release_path']) / 'manifest.json'
            for kind, attributes in ((stat.S_IFIFO, 0), (stat.S_IFDIR, 0),
                                     (stat.S_IFLNK, 0), (stat.S_IFSOCK, 0),
                                     (stat.S_IFREG, 0x400)):
                with self.subTest(sha=selected['sha'], kind=kind, attributes=attributes):
                    def metadata(path, *args, **kwargs):
                        value = original_lstat(path, *args, **kwargs)
                        if path != manifest:
                            return value
                        class SpecialFile:
                            st_mode = stat.S_IMODE(value.st_mode) | kind
                            st_file_attributes = attributes
                            def __getattr__(self, name):
                                return getattr(value, name)
                        return SpecialFile()

                    def read(path, *args, **kwargs):
                        if path == manifest:
                            self.fail('opened a manifest with unsafe file metadata')
                        return original_read(path, *args, **kwargs)

                    # Inject metadata so FIFO/reparse checks execute on Windows
                    # too, and a regression cannot hang the test opening a FIFO.
                    with patch.object(Path, 'lstat', metadata), patch.object(Path, 'read_text', read):
                        self.assertEqual(check_recovery(self.enrolled, release, self.receipt(release)),
                                         {'eligible': False, 'reasons': ['manifest-corrupt']})
                        if selected is baseline:
                            self.assertEqual(check_eligibility(self.enrolled, self.a, b, 4),
                                             {'eligible': False, 'reasons': ['manifest-corrupt']})


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
