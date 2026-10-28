"""Placement proof operations."""
import os
import re
import subprocess
import json
import time
from pathlib import Path

from .placement_paths import (
    IS_WINDOWS,
    _ordinal_case,
    cleanup_normalize,
    drive,
    windows_fold_uncertain,
)
from .placement_process import (
    _cim_rows,
    _cleanup_cim,
    _split_windows_cmdline,
    _windows_cwd,
    _windows_processes,
    cleanup_identity,
)


def _process_os_location(exe, windows):
    if not exe:
        return True
    roots = ([os.environ.get('SystemRoot', r'C:\Windows'),
              os.environ.get('ProgramFiles', r'C:\Program Files'),
              os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'),
              os.environ.get('ProgramData', r'C:\ProgramData') + r'\Microsoft',
              r'C:\Windows\SystemApps'] if windows else ['/usr', '/bin', '/sbin', '/lib', '/lib64', '/opt'])
    path = cleanup_normalize(exe, windows)
    return any(path.startswith(cleanup_normalize(root, windows).rstrip('/') + '/') for root in roots)


_PROCESS_IGNORE_R11_WINDOWS = ['ctfmon.exe', 'TabTip.exe', 'TextInputHost.exe', 'DefenderSessionHelper.exe',
                               'Wacom_TouchUser.exe', 'Taskmgr.exe', 'SecurityHealthSystray.exe', 'sihost.exe']


_PROCESS_IGNORE_WSL = ['systemd', '(sd-pam)']


def default_process_ignore():
    """Owner-editable non-workspace helpers; shells are deliberately excluded."""
    return [*_PROCESS_IGNORE_R11_WINDOWS,
            'csrss.exe', 'winlogon.exe', 'dwm.exe', 'fontdrvhost.exe', 'NVDisplay.Container.exe',
            'RtkAudUService64.exe', 'WacomHost.exe', 'Wacom_Tablet.exe', 'remoting_desktop.exe', *_PROCESS_IGNORE_WSL]


def seed_cleanup_process_policy(policy):
    """Upgrade the exact prior factory list; preserve every owner-modified list."""
    previous = [name for name in default_process_ignore() if name != 'remoting_desktop.exe']
    if 'process_ignore' not in policy or policy['process_ignore'] in (
            _PROCESS_IGNORE_R11_WINDOWS + _PROCESS_IGNORE_WSL, previous):
        policy['process_ignore'] = default_process_ignore()
    policy.setdefault('process_query_timeout_s', 240)


def cleanup_processes(*, proc_root=None, cim=None, windows=None, request=None):
    """Snapshot invoking-user visibility; foreign processes still contribute path evidence."""
    proof = _CleanupProcesses(proc_root=proc_root, cim=cim, windows=windows, request=request)
    if request is not None:
        diagnostics = request.setdefault('_process_diagnostics', {})
        for row in proof.unreadable:
            diagnostics[(row['pid'], row['name'])] = row
        if proof.query_failure:
            request.setdefault('_process_query_failures', []).append(proof.query_failure)
    return proof


class _CleanupProcesses:
    """One invoking-user snapshot. Unknown ownership fails closed; foreign argv still retains named paths."""

    def __init__(self, proc_root=None, windows=None, cim=None, request=None):
        self.rows, self.unreadable, self.out_of_scope, self.unknown = [], [], 0, False
        self._indexed, self._prefixes = -1, (set(), False)
        self.windows = IS_WINDOWS if windows is None else windows
        self.ignored = (request or {}).get('paths', {}).get('process_ignore', default_process_ignore())
        self.ignored = {_ordinal_case(name) if self.windows else name for name in self.ignored}
        self.query_failure = None
        self.request = request or {}
        if self.windows:
            try:
                current = _windows_processes() if not cim and request else None
                self.current = current
                answer = dict(rows=cim()) if cim else _cleanup_cim(request, current)
                self.query_failure = {k: v for k, v in answer.items() if k not in ('rows', 'identities')} or None
                rows = answer['rows']
                self.unknown = bool(self.query_failure) or (not rows and current is None)
                if current is not None:
                    known = {str(row['pid']): row for row in rows}
                    baseline = answer.get('identities') or {}
                    refresh = []
                    for pid, identity in current.items():
                        if pid in known and identity[1] and baseline.get(pid) == identity:
                            continue
                        refresh.append(pid)
                    rows = list(known.values())
                    if refresh:
                        # Never inherit ownership/argv from a PID whose birth is unknown or changed.
                        started = time.monotonic()
                        try:
                            fresh = {str(row['pid']): row for row in _cim_rows(
                                self.request.get('paths', {}).get('process_query_timeout_s', 240), refresh)}
                        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
                            fresh = {}
                            self.query_failure = dict(error=str(error), elapsed_s=time.monotonic() - started,
                                reason_code='process_query_timeout' if isinstance(error, subprocess.TimeoutExpired)
                                else 'process_visibility_unavailable')
                        rows = [row for row in rows if str(row['pid']) not in refresh]
                        try:
                            after = _windows_processes()
                        except OSError as error:
                            after = {}
                            self.query_failure = dict(reason_code='process_visibility_unavailable', error=str(error))
                        fresh = {pid: row for pid, row in fresh.items()
                                 if pid in refresh and current[pid][1]
                                 and after.get(pid) == current[pid]}
                        rows.extend(fresh.values())
                        for pid in refresh:
                            if pid not in fresh:
                                self.unknown = True
                                self._unreadable(int(pid), current[pid][0], evidence='process_identity_unavailable')
                    rows = [row for row in rows if str(row['pid']) in current]
                for row in rows:
                    # Refresh readable cwd evidence even when reusing the run's CIM snapshot.
                    self._record(**dict(row, cwd=row.get('cwd') if 'cwd' in row else _windows_cwd(row['pid'])))
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
                self.unknown = True
                self.query_failure = dict(reason_code='process_visibility_unavailable', error=str(error))
            return
        root = Path(proc_root if proc_root is not None else '/proc')
        try:
            entries = list(root.iterdir())
        except OSError:
            self.unknown = True
            return
        for entry in entries:
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            same_user, name = None, entry.name
            try:
                fields = dict(line.split(':', 1) for line in (entry / 'status').read_text().splitlines() if ':' in line)
                same_user = int(fields['Uid'].split()[0]) == os.getuid()
                name = fields.get('Name', name).strip()
            except (OSError, ValueError, KeyError, IndexError):
                if not entry.exists():
                    continue
            try:
                cmd = [arg.decode(errors='replace') for arg in (entry / 'cmdline').read_bytes().split(b'\0') if arg]
            except OSError:
                cmd = None
            command_unreadable = cmd is None
            cmd = cmd or ''
            exe = None
            try:
                exe = str((entry / 'exe').resolve(strict=True))
            except OSError:
                pass
            try:
                cwd = str((entry / 'cwd').resolve(strict=True))
            except OSError:
                if not entry.exists():
                    continue
                # Kernel threads have no userspace cwd/argv. A known foreign UID
                # can be excluded; unknown ownership remains conservative.
                cwd = None
            self._record(pid=int(entry.name), name=name, same_user=same_user, cwd=cwd, cmd=cmd,
                         exe=exe, command_unreadable=command_unreadable)

    def _unreadable(self, pid, name, **extra):
        row = dict(pid=pid, name=name, **extra)
        if extra.get('evidence') in (None, 'owner') and extra.get('cwd_unreadable', True):
            row['owner_action'] = f'close {name} (pid {pid}) or add it to [disk_cleanup] process_ignore'
        self.unreadable.append(row)

    def _record(self, *, pid, name, same_user, cwd, cmd, session_id=None, exe=None, command_unreadable=False):
        command_unreadable = command_unreadable or cmd is None
        own_cwd = self._own_cwd(pid) if not cwd or same_user is None or command_unreadable else None
        if own_cwd:
            cwd, same_user = cwd or own_cwd, True
        image = re.split(r'[/\\]', name or '')[-1]
        image = _ordinal_case(image) if self.windows else image
        ignored = image in self.ignored and _process_os_location(exe, self.windows)
        foreign = same_user is False or (self.windows and same_user is None and session_id == 0)
        if ignored and (same_user is None or not cwd):
            self.out_of_scope += 1
        elif self.windows and same_user is None and not foreign:
            self.unknown = True
            self._unreadable(pid, name, evidence='owner', cwd_unreadable=not bool(cwd))
        elif not cwd:
            if foreign:
                self.out_of_scope += 1
            else:
                self.unknown = True
                self._unreadable(pid, name)
        elif command_unreadable and not foreign:
            self.unknown = True
            self._unreadable(pid, name, evidence='command_line')
        self.rows.append((cwd, cmd, exe))

    def _own_cwd(self, pid):
        if not self.request.get('state_dir'):
            return None
        side = 'windows' if self.windows else 'wsl'
        path = Path(self.request['state_dir']) / 'disk' / f'process-{side}-{pid}.json'
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
            identity = record['identity']
            if self.windows and getattr(self, 'current', None) is not None:
                birth = (self.current.get(str(pid)) or [None, None])[1]
                current = dict(pid=pid, side=side, birth=str(birth) if birth else None)
            else:
                current = cleanup_identity(pid)
            if (identity.get('pid') == pid and identity.get('side') == side
                    and identity.get('birth') not in (None, '', 'absent')
                    and current == identity):
                return record['cwd']
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            pass
        return None

    @property
    def reason(self):
        return 'process visibility unavailable' if self.unknown else None

    def _index(self):
        """Every ancestor prefix of every path the processes name, built once per snapshot, so a lookup is
        a set test instead of a re-parse of every command line for every candidate."""
        if self._indexed == len(self.rows):
            return self._prefixes
        prefixes, bare = set(), False

        def add(value):
            nonlocal bare
            value = cleanup_normalize(value, self.windows)
            bare |= value.startswith('/')
            prefixes.add(value)
            prefixes.add(value.rstrip('/'))
            at = value.find('/', 1)
            while at != -1:
                prefixes.add(value[:at])
                at = value.find('/', at + 1)
        for cwd, cmd, exe in self.rows:
            for path in (cwd, exe):
                if path:
                    add(path)
            # Preserve quoted paths with spaces and option=value paths. Every path
            # goes through the same identity normalization as cwd and executable.
            if isinstance(cmd, list):
                tokens = cmd
            elif self.windows:
                tokens = _split_windows_cmdline(cmd) if cmd else []
            else:
                tokens = [next(value for value in match.groups() if value is not None)
                          for match in re.finditer(r"\"([^\"]*)\"|'([^']*)'|([^\s\"']+)", cmd or '')]
            for token in tokens:
                for value in (token, token.partition('=')[2]):
                    if value and (drive(value) or value.startswith(('/', '\\\\'))):
                        add(value)
        self._prefixes, self._indexed = (prefixes, bare), len(self.rows)
        return self._prefixes

    def busy(self, worktree):
        if self.unknown:
            return True
        if self.windows and windows_fold_uncertain(str(worktree)):
            return True  # identity cannot be proven; unknown evidence retains
        prefixes, bare = self._index()
        worktree = cleanup_normalize(worktree, self.windows)
        stem = worktree.rstrip('/')
        # An empty stem is the filesystem root: it contains every absolute path.
        return worktree in prefixes or stem in prefixes or (not stem and bare)
