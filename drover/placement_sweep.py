"""The POSIX marker sweep of a lost run's processes: which live processes of this user carry the run's exact marker,
and which members of its group are still there and are the run's. Reads `/proc` only; signals nothing."""
import os
from pathlib import Path

PROC = Path('/proc')      # the POSIX process table


def _ancestors():
    """This process and its ancestors, which the marker sweep never touches."""
    seen, pid = set(), os.getpid()
    while pid > 0 and pid not in seen:
        seen.add(pid)
        try:
            pid = int((PROC / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()[1])
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


def marked(entries):
    """Live processes of this user whose environment holds every one of `entries`, exactly. One that cannot be read
    (gone, EACCES) is skipped and never fails the sweep."""
    mine, skip, found = os.getuid(), _ancestors(), []
    for entry in PROC.iterdir():
        if not entry.name.isdigit() or int(entry.name) in skip:
            continue
        try:
            if entry.stat().st_uid != mine or (entry / 'stat').read_text().rsplit(')', 1)[1].split()[0] == 'Z':
                continue
            if set(entries) <= set((entry / 'environ').read_bytes().split(b'\0')):
                found.append(int(entry.name))
        except (OSError, ValueError, IndexError):
            continue
    return found


def owned(members, known, entries):
    """The group members still present, and whether one carries all of `entries`, read before any kill: a killed
    member that is still exiting shows an empty environ, and one gone on this read is gone, not an error."""
    present, ours = [], False
    for entry in members:
        try:
            if int(entry.name) in known:
                ours = True
            elif set(entries) <= set((entry / 'environ').read_bytes().split(b'\0')):
                ours = True
        except (FileNotFoundError, ProcessLookupError):
            continue
        present.append(entry)
    return present, ours


def live(pids):
    """Those of `pids` still listed and not zombies: a killed process counts until it is gone."""
    found = []
    for pid in pids:
        try:
            if (PROC / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()[0] != 'Z':
                found.append(pid)
        except FileNotFoundError:
            continue
    return found
