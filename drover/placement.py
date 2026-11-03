"""Placement: Windows and WSL command execution, paths and process ownership.

Commands are argument lists. Only explicitly identified paths are translated;
configured worker/check argv is never rewritten. The hub binds its live settings
once; all callers then resolve the same ledger override above configuration.
"""
import os
import re
import sys
import json
import signal
import shutil
import time
from contextlib import contextmanager
from pathlib import Path

from . import git_client, git_worktrees, process
from .placement_paths import (
    IS_WINDOWS, MNT_RE, WIN_RE, _ordinal_case, _ordinal_char, canonical, cleanup_link, cleanup_normalize,
    cleanup_parts, cleanup_path, cleanup_path_side, cleanup_side, config_path, drive, is_linux_native,
    native, path_parts, routes_through_wsl, sandbox_roots, to_windows_path, to_wsl_path,
    windows_fold_uncertain, windows_system_path, worktree_side,
)
from .placement_tree import (
    KILL_AFTER_S, PosixTree, WindowsTree,
)
from .placement_exec import (
    CREATE_BREAKAWAY_FROM_JOB, CREATE_NEW_PROCESS_GROUP, CREATE_SUSPENDED,
    TELEMETRY_OPTOUT, WSL, Windows, _name, _value, _wslenv, bind_settings, ci_command,
    ci_invocation, ci_path, ci_runner_command,
    detach, distro, executable, native_runner_cwd, spawn_worker,
)
from .placement_git import (
    _METADATA_IO, _PATH_OPTS, _WORKTREE_PATH_SUBS, _git_command, _git_location, _map_paths,
    git_argv, prepare_worktree, reachable, relativize_gitdir, status_args, worktree_metadata,
)
from .placement_process import (
    ScanError, _cim_rows, _cleanup_cim, _powershell, _proc_cmdlines_linux,
    _proc_cmdlines_windows, stop_ci_children, _read_windows_birth, _split_windows_cmdline,
    _windows_birth, _windows_cwd, _windows_processes, cleanup_identity, cleanup_process_record,
    process_cmdlines,
)
from .placement_proof import (
    _CleanupProcesses, _process_os_location,
    cleanup_processes, cleanup_windows_proof, proof_diagnostics, seed_cleanup_process_policy,
)
from .placement_host import (
    wsl_free_gb, wsl_free_known, wsl_used_gb, wsl_used_known, wsl_running, _WSL_FREE, _WSL_USED,
    CLEANUP_CACHE_COMMANDS, CLEANUP_CACHE_FOLDERS, CLEANUP_SCRATCH_ROOTS, LXSS, REG, _VHDX_CACHE,
    _vhdx_dir, _vhdx_reading, _wsl_vhdx_paths, cleanup_vhdx_report, decode_output,
    detect_distros, detect_vhdx_drives, detected_cleanup_policy, is_drvfs, linux_mount, parse_distros,
    physical_disk_path, trim_argv, vhdx_dir, wsl_zoneinfo_path,
)
from .placement_worker import (
    BELOW_NORMAL_PRIORITY_CLASS, CLEANUP_NICE, CleanupWorkerError, _CLEANUP_STARTUP_PROOF, _CleanupJob,
    cleanup_worker, disk_toast, limit_cpus, lower_priority,
)

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes
    from .placement_tree import _k32, _BASIC, _IOC, _EXT, _resume, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, JOB_BASIC_AND_IO_ACCOUNTING_CLASS, JOB_EXTENDED_LIMIT_CLASS


def cleanup_scope(drive_path, paths):
    """Keep only this physical drive; path spelling/translation lives here. (scoped_paths, scope_errors): `paths`, the
    disk_cleanup policy, is left as it was, and the scoped copy keeps only the roots and caches on `drive_path`."""
    import copy
    scoped, errors = copy.deepcopy(paths), []
    if physical_disk_path(drive_path) is None:
        errors.append(dict(path=drive_path, reason_code='physical_disk_unknown'))
    def belongs(path):
        if physical_disk_path(path) is None or physical_disk_path(drive_path) is None:
            errors.append(dict(path=str(path), reason_code='physical_disk_unknown'))
            return False
        return cleanup_contains(drive_path, path)
    # Repositories may have worktrees on another drive. Stages scope every candidate,
    # rather than dropping the repository that owns that drive's registrations.
    for name in ('temp_roots', 'report_roots'):
        roots = paths.get(name, [])
        scoped[name] = list(dict.fromkeys(native(p) for p in roots if belongs(p)))
    scoped['caches'] = [entry for entry in scoped.get('caches', [])
                        if entry['placement'] == cleanup_side() and belongs(entry.get('path', entry.get('cwd')))]
    if 'build_roots' in scoped:
        scoped['build_roots'] = [dict(entry, path=native(entry['path'])) for entry in scoped['build_roots']
                                 if entry['placement'] == cleanup_side() and belongs(entry['path'])]
    for entry in scoped['caches']:
        key = 'path' if 'path' in entry else 'cwd'
        entry[key] = native(entry[key])
    return scoped, errors


def cleanup_contains(drive_path, path):
    """Whether this concrete candidate belongs to the physical drive at `drive_path`."""
    if cleanup_path_side(path) != cleanup_side():
        return False
    declared = drive_path
    host = physical_disk_path(path)
    drive_host = physical_disk_path(declared) if declared is not None else None
    if host is not None and drive_host is not None:
        return cleanup_normalize(host) == cleanup_normalize(drive_host)
    return False


def cleanup_claim_alive(identity):
    if (identity.get('side') != cleanup_side() or type(identity.get('pid')) is not int
            or identity['pid'] <= 0 or not identity.get('birth')):
        return None
    if identity.get('birth') == 'absent':
        return False
    if identity['side'] == 'wsl' and ':' not in identity['birth']:
        return None  # Old start ticks without boot identity cannot prove a process dead.
    current = cleanup_identity(identity['pid'])
    return None if not current['birth'] else current['birth'] == identity['birth']


def cleanup_free(path):
    """Read the owning side's physical drive, never a virtual filesystem substitute."""
    physical = physical_disk_path(str(path))
    if physical is None:
        return None
    try:
        return shutil.disk_usage(physical).free / 1024 ** 3
    except OSError:
        return None


def add_worktree(repo, dest, *tail, error=RuntimeError, timeout_s=git_client.LONG):
    """Create a worktree; old git may use absolute pointers only on a native filesystem. `LONG` by default: callers
    outside a stage named no limit before, and a slow `worktree add` that succeeds must not fail at 60 s."""
    # An ext4 destination is written by the WSL git so its `.git` file and the repository's `gitdir` record are
    # paths WSL can resolve; Windows git would turn the Linux path into a Windows one.
    side = 'wsl' if routes_through_wsl(dest) else None
    # Locked at birth with the creating side's name so the other OS's `git worktree prune` cannot drop it.
    lock = ('--lock', '--reason', 'wsl' if routes_through_wsl(dest) or not IS_WINDOWS else 'windows')

    def mutate(*args):
        return git_client.mutate(repo, *args, timeout_s=timeout_s, side=side)
    r = mutate('-c', 'core.autocrlf=false', 'worktree', 'add', *lock, '--relative-paths', *tail)
    rejected = re.search(r'(unknown|unrecognized|unsupported|invalid) (option|argument|switch)[^\n]*relative-paths',
                         r.err, re.IGNORECASE)
    if r.why == 'exit' and rejected:
        if not is_linux_native(dest):
            raise error('this git has no `worktree add --relative-paths`, and a worktree under '
                        f'{dest} is opened from both Windows and WSL: update git (2.48 or newer)')
        r = mutate('-c', 'core.autocrlf=false', 'worktree', 'add', *lock, *tail)
    if not r.ok:
        said = f'timed out after {timeout_s:g}s' if r.why == 'timeout' else r.err.strip()[-300:]
        raise error(f'git worktree add failed: {said}')
    # The repository is shared by both OSes: neither side's gc may expire an entry the other cannot see. Best effort.
    mutate('config', 'gc.worktreePruneExpire', 'never')


def unlock_worktree(repo, path):
    """Release only this creator's side marker; owner notes remain locked."""
    expected = 'wsl' if routes_through_wsl(path) or not IS_WINDOWS else 'windows'
    side = {'side': 'wsl'} if expected == 'wsl' else {}      # only the WSL git spells ext4 worktrees correctly
    try:
        listed = git_worktrees.listing(repo, timeout_s=git_client.NORMAL, **side)
    except git_worktrees.RegistrationsUnknown as error:
        if isinstance(error.__cause__, git_client.GitFailed) and error.__cause__.why == 'timeout':
            raise       # as today's TimeoutExpired did; any other failure releases nothing
        return
    for fields in listed.rows:
        if cleanup_normalize(fields['worktree']) == cleanup_normalize(path) and fields.get('locked') == expected:
            done = git_client.mutate(repo, 'worktree', 'unlock', str(path), timeout_s=git_client.NORMAL, **side)
            if done.why == 'timeout':      # raised, as the listing's timeout is; a refused unlock is not an error
                raise git_client.GitFailed(('worktree', 'unlock'), done.why, done.code, done.err, done.out)


def recover_ci_children(platform, status, drover_dir, distro_name=None):
    """Confirm child termination on the execution's placement, including remote WSL."""
    if IS_WINDOWS and platform == 'wsl':
        code = ('import json,sys; from drover.placement import stop_ci_children; '
                'sys.exit(0 if stop_ci_children(json.loads(sys.argv[1])) else 1)')
        identity = {key: status.get(key) for key in ('pid', 'runner_pid', 'run_id', 'nonce')}     # nonce: the sweep's
        command = WSL(distro_name).command(['python3', '-c', code, json.dumps(identity)], cd=drover_dir)
        result = process.run(process.Command(command, 90))
        if result.kind != 'exited':
            raise ScanError(f'cannot confirm WSL CI child termination: {result.detail}')
        if result.code not in (0, 1):
            raise ScanError('cannot confirm WSL CI child termination')
        return result.code == 0
    return stop_ci_children(status)
