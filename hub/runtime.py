"""Stdlib-only immutable releases for the frozen hub supervisor.

Git adapters are callables ``git(source: Path, *args: str) -> bytes`` and must
raise on failure. Disk adapters return free bytes for a Path; interpreter
probes return an identity string for the configured executable. The default
identity is its SHA256. WSL probes return whether a retained Path is reachable.

``root`` is the releases container, normally state_root/releases. Enrollment
may carry schema_digest/dependency_digests, or a baseline {sha, release_path,
manifest_digest}. A trusted installer may instead pass its baseline manifest
as a keyword. Only first_enrollment may omit those anchors; it must supply a
baseline_probe(staging_path, enrollment) doing the independent, read-only child
schema preflight before publication. This module never imports release code.

manifest_digest hashes canonical JSON excluding that field (no newline).
Recovery accepts a durable active/previous receipt or enrollment['baseline'];
its success authorizes only CLOSED startup with schema preflight, migration
disabled and fresh readiness. It supplies no assertion about the live ledger.
"""
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import stat
import subprocess
import tempfile
from uuid import UUID
import zipfile
import zlib

if __package__:
    from .reload_protocol import PROTOCOL, RUN_FORMAT, ProtocolError, atomic_json, validate_record
else:
    from reload_protocol import PROTOCOL, RUN_FORMAT, ProtocolError, atomic_json, validate_record


MAX_EXPANDED = 128 * 1024 * 1024
DISK_RESERVE = 256 * 1024 * 1024
SCHEMA = 'drover/ledger/schema.py'
DEPENDENCIES = ('hub/pyproject.toml', 'hub/uv.lock')
BOOTSTRAP = ('hub/supervisor.py', 'hub/runtime.py', 'hub/reload_protocol.py',
             'hub/reload_machine.py', 'hub/scripts/Start-DroverHub.ps1',
             'hub/scripts/Install-Drover.ps1', 'hub/scripts/Install-DroverHub.ps1')
MANIFEST = 'manifest.json'


class ReleaseError(ValueError):
    """A bounded refusal reason, safe to expose as a pending blocker."""


def _refuse(reason):
    raise ReleaseError(reason)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode('utf-8')


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _manifest_digest(value):
    return _hash(_json({k: v for k, v in value.items() if k != 'manifest_digest'}))


def _sha(value):
    if not isinstance(value, str) or re.fullmatch('[0-9a-f]{40}', value) is None:
        _refuse('invalid-sha')


def _canonical(path):
    return os.path.normcase(str(Path(path).resolve()))


def _git(source, *args):
    # Never inherit a caller's alternate index/object directory or replacement
    # refs. No command in this module changes refs, the index or the checkout.
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith('GIT_')}
    env.update(GIT_OPTIONAL_LOCKS='0', GIT_NO_REPLACE_OBJECTS='1')
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(['git', '-C', str(source), *args], stdout=output,
                                stderr=subprocess.DEVNULL, env=env)
        if result.returncode:
            _refuse('git-probe-failed')
        # Bound compressed archives and pathological tree/status output too.
        if output.tell() > 2 * MAX_EXPANDED:
            _refuse('archive-too-large')
        output.seek(0)
        return output.read()


def _identity(path):
    return _hash(Path(path).read_bytes())


def _free(path):
    return shutil.disk_usage(path).free


def _state_root(enrolled):
    return Path(enrolled.get('state_root', str(Path(enrolled['paths']['ledger']).parent / 'hub-runtime')))


def _enrollment(enrolled):
    # Name the required-schema refusal rather than hiding it in a shape error.
    required, actual = enrolled.get('required_schema'), enrolled.get('ledger_schema')
    if (type(required) is not int or required < 0 or type(actual) is not int or
            actual < 0 or actual != required):
        _refuse('enrollment-schema-mismatch')
    try:
        validate_record('enrollment', enrolled)
    except ProtocolError:
        _refuse('invalid-enrollment')
    if enrolled.get('run_format', RUN_FORMAT) != RUN_FORMAT or type(enrolled.get('run_format', RUN_FORMAT)) is not int:
        _refuse('run-format-mismatch')
    if not _state_root(enrolled).is_absolute():
        _refuse('pinned-path-mismatch')


def _pinned(enrolled):
    return {k: enrolled[k] for k in
            ('source_root', 'common_dir', 'default_branch', 'interpreter', 'paths')} | {
                'state_root': str(_state_root(enrolled))}


def _path_name(name):
    # Validate for the Windows target even when these tests run on POSIX.
    if (not isinstance(name, str) or not name or '\\' in name or '\x00' in name or
            PurePosixPath(name).is_absolute() or PureWindowsPath(name).drive):
        _refuse('archive-path')
    for part in name.split('/'):
        if (part.casefold() in ('', '.', '..', '.git') or part[-1:] in (' ', '.') or
                any(ord(c) < 32 or c in '<>:"|?*' for c in part) or
                re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', part)):
            _refuse('archive-path')


def _tree(source, sha, git):
    _sha(sha)
    if git(source, 'rev-parse', '--verify', sha + '^{commit}').decode().strip() != sha:
        _refuse('not-exact-commit')
    files = {}
    aliases = {}
    for item in git(source, 'ls-tree', '-r', '-z', '--full-tree', sha).split(b'\0'):
        if not item:
            continue
        header, name = item.split(b'\t', 1)
        mode, kind, blob = header.decode('ascii').split()
        name = name.decode('utf-8')
        _path_name(name)
        if mode not in ('100644', '100755') or kind != 'blob':
            _refuse('archive-type')
        # Detect case aliases for directories as well as leaf files.
        parts = name.split('/')
        for index in range(1, len(parts) + 1):
            path = '/'.join(parts[:index])
            key = path.casefold()
            if key in aliases and aliases[key] != path:
                _refuse('archive-path-collision')
            aliases[key] = path
        files[name] = {'mode': mode, 'blob': blob}
    if MANIFEST in files or not files:
        _refuse('archive-reserved-path')
    return files


def _blob_digests(source, sha, git):
    files = _tree(source, sha, git)
    digests = {}
    for name in (SCHEMA, *DEPENDENCIES, *BOOTSTRAP):
        if name not in files:
            if name in (SCHEMA, *DEPENDENCIES):
                _refuse('missing-contract-file')
            digests[name] = None
        else:
            digests[name] = _hash(git(source, 'show', sha + ':' + name))
    return _contracts(digests)


def _contracts(files):
    if any(name not in files for name in (SCHEMA, *DEPENDENCIES)):
        _refuse('missing-contract-file')
    return dict(schema_digest=files[SCHEMA],
                dependency_digests={name: files[name] for name in DEPENDENCIES},
                bootstrap_digest=_hash(_json({name: files.get(name) for name in BOOTSTRAP})))


def _read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                _refuse('manifest-corrupt')
            result[key] = value
        return result
    try:
        value = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique)
        if not isinstance(value, dict):
            _refuse('manifest-corrupt')
        return value
    except (OSError, ValueError, UnicodeError):
        _refuse('manifest-corrupt')


def _verify_release(release, *, _directory=None):
    if not isinstance(release, dict):
        _refuse('invalid-release')
    if (type(release.get('required_schema')) is not int or
            release.get('required_schema') != release.get('ledger_schema') or
            type(release.get('ledger_schema')) is not int):
        _refuse('release-schema-mismatch')
    if release.get('protocol') != PROTOCOL or type(release.get('protocol')) is not int:
        _refuse('protocol-mismatch')
    if release.get('run_format') != RUN_FORMAT or type(release.get('run_format')) is not int:
        _refuse('run-format-mismatch')
    try:
        validate_record('release', release)
    except ProtocolError:
        _refuse('invalid-release')
    if release.get('manifest_digest') != _manifest_digest(release):
        _refuse('manifest-digest-mismatch')
    root = Path(release['release_path'])
    if str(root.resolve()) != str(root) or root.name != release['sha']:
        _refuse('release-path-mismatch')
    if _directory is not None:
        root = _directory
    if (root / MANIFEST).is_symlink() or _read_json(root / MANIFEST) != release:
        _refuse('manifest-corrupt')
    actual = {}
    total = 0
    def regular(path, kind):
        metadata = path.lstat()
        if (stat.S_IFMT(metadata.st_mode) != kind or
                getattr(metadata, 'st_file_attributes', 0) & 0x400):
            _refuse('retained-content-type')
        return metadata
    try:
        regular(root, stat.S_IFDIR)
        for directory, dirs, names in os.walk(root, followlinks=False):
            for name in dirs:
                regular(Path(directory) / name, stat.S_IFDIR)
            for name in names:
                path = Path(directory) / name
                relative = path.relative_to(root).as_posix()
                metadata = regular(path, stat.S_IFREG)
                if relative == MANIFEST:
                    continue
                _path_name(relative)
                size = metadata.st_size
                total += size
                if total > MAX_EXPANDED:
                    _refuse('retained-content-size')
                actual[relative] = _hash(path.read_bytes())
    except OSError:
        _refuse('retained-content-unreadable')
    if actual != release['files']:
        _refuse('retained-content-mismatch')
    if _hash(_json(actual)) != release['content_digest']:
        _refuse('content-digest-mismatch')
    if any(release[key] != value for key, value in _contracts(actual).items()):
        _refuse('retained-contract-mismatch')
    return release


def _match_enrollment(enrolled, release):
    if release['ledger_schema'] != enrolled['ledger_schema'] or release['required_schema'] != enrolled['required_schema']:
        _refuse('release-schema-mismatch')
    if any(release.get(key) != value for key, value in _pinned(enrolled).items()):
        _refuse('pinned-path-mismatch')
    expected = _state_root(enrolled) / 'releases' / release['sha']
    if _canonical(expected) != _canonical(release['release_path']):
        _refuse('release-path-mismatch')
    if release['bootstrap_digest'] != enrolled['bootstrap_digest']:
        _refuse('bootstrap-change')


def _anchors(enrolled, baseline=None):
    if baseline is None and 'baseline' in enrolled:
        anchor = enrolled['baseline']
        baseline = _read_json(Path(anchor['release_path']) / MANIFEST)
        if any(baseline.get(k) != anchor.get(k) for k in ('sha', 'release_path', 'manifest_digest')):
            _refuse('baseline-digest-mismatch')
    if baseline is not None:
        if 'baseline' in enrolled and any(
                baseline.get(k) != enrolled['baseline'].get(k)
                for k in ('sha', 'release_path', 'manifest_digest')):
            _refuse('baseline-digest-mismatch')
        _verify_release(baseline)
        _match_enrollment(enrolled, baseline)
        anchors = {key: baseline[key] for key in ('schema_digest', 'dependency_digests')}
        for key, value in anchors.items():
            if key in enrolled and enrolled[key] != value:
                _refuse('enrollment-contract-mismatch')
        return anchors
    if not all(key in enrolled for key in ('schema_digest', 'dependency_digests')):
        _refuse('missing-schema-anchor')
    anchors = {key: enrolled[key] for key in ('schema_digest', 'dependency_digests')}
    # Reuse the protocol's digest validation without accepting arbitrary values.
    sample = dict(protocol=PROTOCOL, run_format=RUN_FORMAT, sha='0' * 40,
                  release_path=str(_state_root(enrolled) / 'releases' / ('0' * 40)),
                  tree_digest='0' * 64, content_digest='0' * 64, files={SCHEMA: '0' * 64},
                  bootstrap_digest=enrolled['bootstrap_digest'],
                  ledger_schema=enrolled['ledger_schema'], required_schema=enrolled['required_schema'], **anchors)
    try:
        validate_record('release', sample)
    except ProtocolError:
        _refuse('enrollment-contract-mismatch')
    return anchors


def _machine(enrolled, release_path, interpreter_probe, wsl_probe, wsl_live_run_ids):
    try:
        if interpreter_probe(Path(enrolled['interpreter']['path'])) != enrolled['interpreter']['identity']:
            _refuse('interpreter-change')
    except OSError:
        _refuse('interpreter-unavailable')
    needed = wsl_live_run_ids if wsl_live_run_ids is not None else enrolled.get('wsl_live_run_ids', [])
    if needed:
        if wsl_probe is None or not wsl_probe(Path(release_path)):
            _refuse('wsl-release-unreachable')


def _source_identity(enrolled, target_sha, git):
    source = Path(enrolled['source_root'])
    top = git(source, 'rev-parse', '--show-toplevel').decode().strip()
    common = git(source, 'rev-parse', '--path-format=absolute', '--git-common-dir').decode().strip()
    if _canonical(top) != _canonical(source) or _canonical(common) != _canonical(enrolled['common_dir']):
        _refuse('wrong-source-checkout')
    try:
        branch = git(source, 'symbolic-ref', '--quiet', 'HEAD').decode().strip()
    except (ReleaseError, subprocess.SubprocessError):
        _refuse('wrong-default-branch')
    if branch != 'refs/heads/' + enrolled['default_branch']:
        _refuse('wrong-default-branch')
    if git(source, 'status', '--porcelain', '--untracked-files=all'):
        _refuse('source-dirty')
    if git(source, 'rev-parse', '--verify', 'HEAD').decode().strip() != target_sha:
        _refuse('source-head-mismatch')


def _sync_directory(path):
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def prepare_release(source: Path, sha: str, root: Path, enrolled: dict, *,
                    git=_git, disk=_free, interpreter_probe=_identity, wsl_probe=None,
                    wsl_live_run_ids=None, baseline=None, first_enrollment=False,
                    baseline_probe=None) -> dict:
    """Export exact objects, validate, then create once; never delete a release.

    This stages bytes only; source cleanliness/ancestry and fresh live schema
    are check_eligibility's job. An existing corrupt/different release blocks.
    """
    _enrollment(enrolled)
    _sha(sha)
    source, root = Path(source), Path(root)
    if _canonical(source) != _canonical(enrolled['source_root']):
        _refuse('wrong-source-checkout')
    if _canonical(root) != _canonical(_state_root(enrolled) / 'releases'):
        _refuse('release-path-mismatch')
    destination = root.resolve() / sha
    _machine(enrolled, destination, interpreter_probe, wsl_probe, wsl_live_run_ids)
    anchors = None if first_enrollment else _anchors(enrolled, baseline)
    if first_enrollment and (baseline_probe is None or 'baseline' in enrolled):
        _refuse('baseline-probe-required')
    if first_enrollment:
        _source_identity(enrolled, sha, git)
    tree = _tree(source, sha, git)
    payload = git(source, 'archive', '--format=zip', sha)
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.' + sha + '.', dir=root))
    try:
        files = {}
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                entries = archive.infolist()
                seen = set()
                expanded = 0
                for entry in entries:
                    name = entry.filename[:-1] if entry.is_dir() else entry.filename
                    _path_name(name)
                    if name in seen:
                        _refuse('archive-path-collision')
                    seen.add(name)
                    mode = entry.external_attr >> 16
                    kind = stat.S_IFMT(mode)
                    if kind not in (0, stat.S_IFDIR if entry.is_dir() else stat.S_IFREG):
                        _refuse('archive-type')
                    if entry.flag_bits & 1:
                        _refuse('archive-encrypted')
                    if entry.is_dir():
                        if entry.file_size:
                            _refuse('archive-type')
                        if not any(path.startswith(name + '/') for path in tree):
                            _refuse('archive-tree-mismatch')
                    else:
                        if name not in tree:
                            _refuse('archive-tree-mismatch')
                        expanded += entry.file_size
                if expanded > MAX_EXPANDED:
                    _refuse('archive-too-large')
                if disk(root) < 2 * expanded + DISK_RESERVE:
                    _refuse('insufficient-disk')
                for entry in entries:
                    if entry.is_dir():
                        continue
                    data = archive.read(entry)
                    # Git archive honors export-subst/export-ignore. Neither
                    # may silently turn this into anything but the exact tree.
                    blob = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
                    if len(data) != entry.file_size or blob != tree[entry.filename]['blob']:
                        _refuse('archive-blob-mismatch')
                    path = staging / entry.filename
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open('xb') as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    path.chmod(0o755 if tree[entry.filename]['mode'] == '100755' else 0o644)
                    files[entry.filename] = _hash(data)
        except (zipfile.BadZipFile, RuntimeError, EOFError, zlib.error):
            _refuse('archive-corrupt')
        if files.keys() != tree.keys():
            _refuse('archive-tree-mismatch')
        contracts = _contracts(files)
        for key, value in (anchors or {}).items():
            if contracts[key] != value:
                _refuse('schema-change' if key == 'schema_digest' else 'dependency-change')
        if contracts['bootstrap_digest'] != enrolled['bootstrap_digest']:
            _refuse('bootstrap-change')
        if first_enrollment:
            proof = baseline_probe(staging, enrolled)
            if (not isinstance(proof, dict) or type(proof.get('ledger_schema')) is not int or
                    type(proof.get('required_schema')) is not int or
                    proof['ledger_schema'] != enrolled['ledger_schema'] or
                    proof['required_schema'] != enrolled['required_schema']):
                _refuse('baseline-schema-mismatch')
        release = dict(protocol=PROTOCOL, run_format=RUN_FORMAT, sha=sha,
                       release_path=str(destination), tree_digest=_hash(_json(tree)),
                       content_digest=_hash(_json(files)), files=files, **contracts,
                       ledger_schema=enrolled['ledger_schema'], required_schema=enrolled['required_schema'],
                       **_pinned(enrolled))
        release['manifest_digest'] = _manifest_digest(release)
        validate_record('release', release)
        atomic_json(staging / MANIFEST, release)
        _verify_release(release, _directory=staging)
        if destination.exists() or destination.is_symlink():
            existing = _verify_release(_read_json(destination / MANIFEST))
            if existing != release:
                _refuse('release-not-identical')
            return existing
        # A valid published directory is nonempty; rename cannot overwrite it.
        # Empty preexisting directories are refused above as corrupt releases.
        for directory, _, _ in os.walk(staging, topdown=False):
            _sync_directory(directory)
        try:
            staging.rename(destination)
        except OSError:
            if not destination.exists():
                raise
            existing = _verify_release(_read_json(destination / MANIFEST))
            if existing != release:
                _refuse('release-not-identical')
        _sync_directory(root)
        return release
    finally:
        if staging.exists():
            shutil.rmtree(staging)  # Only our unpublished temporary export.


def _result(check):
    try:
        check()
    except ReleaseError as error:
        return {'eligible': False, 'reasons': [str(error)]}
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, subprocess.SubprocessError):
        return {'eligible': False, 'reasons': ['runtime-probe-failed']}
    return {'eligible': True, 'reasons': []}


def check_eligibility(enrolled: dict, running_sha: str, target_sha: str,
                      ledger_schema=None, *, git=_git, interpreter_probe=_identity,
                      wsl_probe=None, wsl_live_run_ids=None, baseline=None) -> dict:
    """Check a candidate before drain/spawn; caller verifies schema freshness."""
    def check():
        _enrollment(enrolled)
        _sha(running_sha)
        _sha(target_sha)
        if type(ledger_schema) is not int or ledger_schema != enrolled['required_schema']:
            _refuse('actual-schema-mismatch')
        source = Path(enrolled['source_root'])
        _source_identity(enrolled, target_sha, git)
        try:
            ancestor = git(source, 'merge-base', running_sha, target_sha).decode().strip()
        except (ReleaseError, subprocess.SubprocessError):
            _refuse('not-descendant')
        if ancestor != running_sha:
            _refuse('not-descendant')
        anchors = _anchors(enrolled, baseline)
        for sha in dict.fromkeys((running_sha, target_sha)):
            contracts = _blob_digests(source, sha, git)
            if contracts['schema_digest'] != anchors['schema_digest']:
                _refuse('schema-change')
            if contracts['dependency_digests'] != anchors['dependency_digests']:
                _refuse('dependency-change')
            if contracts['bootstrap_digest'] != enrolled['bootstrap_digest']:
                _refuse('bootstrap-change')
        _machine(enrolled, _state_root(enrolled) / 'releases' / target_sha,
                 interpreter_probe, wsl_probe, wsl_live_run_ids)
    return _result(check)


def check_recovery(enrolled: dict, release: dict, healthy: dict, *,
                   interpreter_probe=_identity, wsl_probe=None, wsl_live_run_ids=None,
                   baseline=None) -> dict:
    """Validate retained healthy bytes only. Intentionally has no Git adapter."""
    def check():
        _enrollment(enrolled)
        if healthy != enrolled.get('baseline'):
            try:
                validate_record('healthy', healthy)
            except ProtocolError:
                _refuse('invalid-healthy-receipt')
        if not isinstance(healthy, dict) or any(healthy.get(k) != release.get(k) for k in ('sha', 'manifest_digest')):
            _refuse('unapproved-release')
        _verify_release(release)
        _match_enrollment(enrolled, release)
        anchors = _anchors(enrolled, baseline)
        if release['schema_digest'] != anchors['schema_digest']:
            _refuse('schema-change')
        if release['dependency_digests'] != anchors['dependency_digests']:
            _refuse('dependency-change')
        _machine(enrolled, release['release_path'], interpreter_probe, wsl_probe, wsl_live_run_ids)
    return _result(check)


def child_command(enrolled: dict, release: dict, generation: int, instance: str, *,
                  env=None) -> tuple[list[str], dict[str, str], str]:
    """Build an argv array for the existing interpreter, with retained imports.

    Consumer sets operation identity/creation proof and starts admission closed;
    this builder does not replace candidate/recovery or child readiness checks.
    """
    _enrollment(enrolled)
    _verify_release(release)
    _match_enrollment(enrolled, release)
    if type(generation) is not int or generation < 0:
        _refuse('invalid-generation')
    try:
        if str(UUID(instance)) != instance:
            _refuse('invalid-instance')
    except (ValueError, TypeError, AttributeError):
        _refuse('invalid-instance')
    pinned_names = ('PYTHONPATH', 'PYTHONHOME', 'PYTHONSAFEPATH', 'PYTHONDONTWRITEBYTECODE',
                    'DROVER_OVERLAY', 'DROVER_HOME', 'DROVER_REPO_CONFIGS_DIR')
    result = {k: v for k, v in (os.environ if env is None else env).items()
              if k.upper() not in pinned_names and
              not k.upper().startswith('DROVER_RELOAD_')}
    path = release['release_path']
    result.update(PYTHONPATH=path, PYTHONSAFEPATH='1', PYTHONDONTWRITEBYTECODE='1',
                  DROVER_OVERLAY=enrolled['paths']['overlay'],
                  DROVER_HOME=enrolled['paths']['config_home'],
                  DROVER_REPO_CONFIGS_DIR=enrolled['paths']['repo_config'],
                  DROVER_RELOAD_STATE_ROOT=str(_state_root(enrolled)),
                  DROVER_RELOAD_SOURCE_ROOT=enrolled['source_root'],
                  DROVER_RELOAD_RELEASE_PATH=path, DROVER_RELOAD_RUNTIME_SHA=release['sha'],
                  DROVER_RELOAD_GENERATION=str(generation), DROVER_RELOAD_INSTANCE=instance,
                  DROVER_RELOAD_REQUIRED_SCHEMA=str(release['required_schema']))
    for name, value in enrolled['paths'].items():
        result['DROVER_RELOAD_' + name.upper()] = value
    return [enrolled['interpreter']['path'], '-P', '-m', 'hub'], result, path
