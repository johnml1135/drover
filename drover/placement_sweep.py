"""The POSIX marker sweep of a lost run's processes: which live processes of this user carry the run's exact marker,
and which members of its group are still there and are the run's. Reads `/proc` only; signals nothing."""
import os

PROC = '/proc'      # the POSIX process table


def _stat(pid):
    """The fields of a process's `stat` after its command name, which may hold spaces and parentheses."""
    with open(f'{PROC}/{pid}/stat') as stream:
        return stream.read().rsplit(')', 1)[1].split()


def _environ(pid):
    with open(f'{PROC}/{pid}/environ', 'rb') as stream:
        return set(stream.read().split(b'\0'))


def _ancestors():
    """This process and its ancestors, which the marker sweep never touches."""
    seen, pid = set(), os.getpid()
    while pid > 0 and pid not in seen:
        seen.add(pid)
        try:
            pid = int(_stat(pid)[1])
        except (OSError, ValueError, IndexError):
            break
    return seen


def marks(status):
    """The exact environment entries that make a process this run's, or None when they would not be unique: a gate's
    run id is a per-ledger integer, so it needs its launch nonce; a disk worker's is uuid4 hex on its own."""
    run_id, nonce = str(status['run_id']), status.get('nonce')
    if nonce:
        return (f'DROVER_RUN_ID={run_id}'.encode(), f'DROVER_RUN_ID_NONCE={nonce}'.encode())
    unique = len(run_id) == 32 and all(c in '0123456789abcdef' for c in run_id)
    return (f'DROVER_RUN_ID={run_id}'.encode(),) if unique else None


def scan(entries, group):
    """One walk of `/proc`, reading each process's `stat` once: the live processes of this user whose environment
    holds every one of `entries` exactly (none when `entries` is None), and the live members of process group `group`.

    A process gone before its `stat` is read is skipped; any other failure to read a `stat` raises, since a member
    may hide behind it. A marker candidate that cannot be read further (gone, EACCES) is skipped and never fails the
    sweep. This process and its ancestors are never marked."""
    mine, skip = (os.getuid(), _ancestors()) if entries is not None else (None, ())
    marked, members = [], []
    for entry in os.scandir(PROC):
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            fields = _stat(pid)
        except FileNotFoundError:
            continue
        if fields[0] == 'Z':
            continue
        if int(fields[2]) == group:
            members.append(pid)
        if entries is None or pid in skip:
            continue
        try:
            if entry.stat().st_uid == mine and set(entries) <= _environ(pid):
                marked.append(pid)
        except (OSError, ValueError, IndexError):
            continue
    return marked, members


def owned(members, known, entries):
    """The group members still present, and whether one carries all of `entries`, read before any kill: a killed
    member that is still exiting shows an empty environ, and one gone on this read is gone, not an error."""
    present, ours = [], False
    for pid in members:
        try:
            if pid in known or set(entries) <= _environ(pid):
                ours = True
        except (FileNotFoundError, ProcessLookupError):
            continue
        present.append(pid)
    return present, ours


def live(pids):
    """Those of `pids` still listed and not zombies: a killed process counts until it is gone."""
    found = []
    for pid in pids:
        try:
            if _stat(pid)[0] != 'Z':
                found.append(pid)
        except FileNotFoundError:
            continue
    return found
