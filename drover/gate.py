"""Gates: exact-commit receipts and ranked execution leases. A command passes by its exit code."""
import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import placement

VERSION = 1
PRIORITIES = ('P0', 'P1', 'P2', 'P3', 'P4')
GIT_PROMPT_ENV = {'GIT_TERMINAL_PROMPT': '0', 'GCM_INTERACTIVE': 'never', 'GIT_ASKPASS': ''}
WEIGHTS = {'P0': 1000, 'P1': 1000, 'P2': 100, 'P3': 30, 'P4': 10}


class GateError(RuntimeError):
    pass


# ---- running a command ----------------------------------------------------------------------------------

def _default_interrupt():
    """A gate launched with `cmd &` inherits SIGINT ignored, and its tests of Ctrl+C then hang; give the gate the default."""
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGQUIT, signal.SIG_DFL)


def systemd_run_works():
    """True on Linux when `systemd-run --user --scope` can start a process; anything else runs unwrapped."""
    if not sys.platform.startswith('linux') or not shutil.which('systemd-run'):
        return False
    try:
        probe = subprocess.run(['systemd-run', '--user', '--scope', '--quiet', 'true'], capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


def wrap_command(command, priority, memory_max='8G', max_seconds=1800, available=None):
    """(argv to run, wrapper argv or None). CPU and IO weights by class; P4 also gets a memory cap and timeout."""
    if available is None:
        available = systemd_run_works()
    if not available:
        return list(command), None
    weight = WEIGHTS[priority]
    wrapper = ['systemd-run', '--user', '--scope', '--quiet', '-p', f'CPUWeight={weight}', '-p', f'IOWeight={weight}']
    inner = list(command)
    if priority == 'P4':
        wrapper += ['-p', f'MemoryMax={memory_max}']
        inner = ['timeout', str(max_seconds)] + inner
    return wrapper + ['--'] + inner, wrapper


def _spawn(argv, repo, stream, env=None):
    kw = {'preexec_fn': _default_interrupt} if os.name == 'posix' else {}
    placement.prepare_worktree(repo)
    return subprocess.run(argv, cwd=repo, stdout=stream, stderr=subprocess.STDOUT,
                          env=git_environment(env), **kw).returncode


def git_environment(base=None):
    """Environment that keeps Git noninteractive even when a machine has an interactive credential helper."""
    env = os.environ.copy() if base is None else dict(base)
    env.update(GIT_PROMPT_ENV)
    return env


# ---- git ------------------------------------------------------------------------------------------------

def git(repo, *args, check=True):
    try:
        result = placement.git(repo, *args, env=git_environment(), timeout=60)
    except subprocess.TimeoutExpired as error:
        raise GateError(f'git {" ".join(args)} timed out after 60 seconds') from error
    if check and result.returncode:
        raise GateError(f'git {" ".join(args)}: {result.stderr.strip()}')
    return result


def head(repo):
    return git(repo, 'rev-parse', 'HEAD').stdout.strip()


def dirty(repo, *, post_run=False):
    """Tracked edits, plus bounded untracked discovery before testing or verifying a commit."""
    tracked = git(repo, *placement.status_args(repo)).stdout.strip()
    if tracked or post_run:
        return tracked
    return git(repo, 'ls-files', '--others', '--exclude-standard', '--directory', '--no-empty-directory').stdout.strip()


def contains(repo, base_ref, sha):
    base = git(repo, 'rev-parse', '--verify', '--quiet', base_ref + '^{commit}', check=False).stdout.strip()
    if not base:
        return None, False
    return base, git(repo, 'merge-base', '--is-ancestor', base, sha, check=False).returncode == 0


def pin(name):
    value = os.environ.get(name)
    entry = {'name': name, 'value': value}
    if value and Path(value).is_file():
        entry['sha256'] = hashlib.sha256(Path(value).read_bytes()).hexdigest()
    return entry


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


# ---- changed paths and documentation guards ---------------------------------------------------------------

def changed_paths(repo, base, tip='HEAD'):
    return [p for p in git(repo, '-c', 'core.quotepath=false', 'diff', '--name-only', '--no-renames', '-z', f'{base}...{tip}').stdout.split('\0') if p]


def _norm(path):
    return str(path).replace('\\', '/')


def _glob_re(pattern):
    out, i, p = '', 0, _norm(pattern)
    while i < len(p):
        if p.startswith('**/', i):
            out += '(?:.*/)?'
            i += 3
        elif p.startswith('**', i):
            out += '.*'
            i += 2
        elif p[i] == '*':
            out += '[^/]*'
            i += 1
        elif p[i] == '?':
            out += '[^/]'
            i += 1
        else:
            out += re.escape(p[i])
            i += 1
    return re.compile(out + r'\Z')


DOCS_EXTENSIONS = ('.md', '.rst', '.txt', '.adoc')
DOCS_EXACT = ('LICENSE', 'GLOSSARY.md')


def _glob_hit(path, pattern):
    rx = _glob_re(pattern)
    return bool(rx.match(path) or ('/' not in _norm(pattern) and rx.match(path.rsplit('/', 1)[-1])))


def docs_only(paths, globs):
    """True when there is at least one path and every path is docs: it matches a positive glob, no `!` glob, and
    has a docs extension (.md .rst .txt .adoc) or is exactly LICENSE or GLOSSARY.md. `**` crosses directories, `*`
    does not; a glob without a slash matches the file name at any depth (`*.md` covers docs/a/b.md)."""
    paths = [_norm(p) for p in paths]
    positive = [g for g in globs or () if not g.startswith('!')]
    negative = [g[1:] for g in globs or () if g.startswith('!')]
    if not paths or not positive:
        return False
    return all((p in DOCS_EXACT or p.lower().endswith(DOCS_EXTENSIONS))
               and any(_glob_hit(p, g) for g in positive) and not any(_glob_hit(p, g) for g in negative)
               for p in paths)


def changed_docs_only(repo, base, tip, globs):
    """docs_only for `base...tip`, and false when any changed path is or was a symlink (git mode 120000)."""
    paths = changed_paths(repo, base, tip)
    if not docs_only(paths, globs):
        return False
    raw = git(repo, 'diff', '--raw', '--no-renames', '-z', f'{base}...{tip}').stdout
    return not any(f.startswith(':') and '120000' in f.split()[:2] for f in raw.split('\0'))


def docs_commands(base, sha, extra=None):
    """The generic whitespace guard, followed by the repo's optional docs script."""
    return [['git', '-c', 'core.whitespace=-blank-at-eol', 'diff', '--check', f'{base}...{sha}']] + ([list(extra)] if extra else [])


# ---- receipts ----------------------------------------------------------------------------------------------

def run_gate(repo, out, command, *, base='origin/main', pins=(), priority='P2', memory_max='8G', max_seconds=1800,
             log=None, available=None, env=None, writable_roots=()):
    """Run the composed command verbatim; an exit code and output tail form the receipt."""
    repo = Path(repo).resolve()
    command = [str(c) for c in command]
    if not command:
        raise GateError('missing gate command')
    if dirty(repo):
        raise GateError('refused: working tree has uncommitted changes; commit the task before gating it')
    sha = head(repo)
    base_sha, has_base = contains(repo, base, sha)
    out = Path(out).resolve()
    log = Path(log).resolve() if log else out.with_suffix('.log')
    out.parent.mkdir(parents=True, exist_ok=True)
    started = now()
    argv, wrapper = wrap_command(command, priority, memory_max, max_seconds, available)
    from .runner import prepare_writable_roots
    prepare_writable_roots(writable_roots)
    with open(log, 'w') as stream:
        code = _spawn(argv, repo, stream, env=env)
    unchanged = head(repo) == sha and not dirty(repo, post_run=True)
    passed = code == 0 and unchanged
    receipt = dict(version=VERSION, sha=sha, repo=str(repo), base_ref=base, base_sha=base_sha, contains_base=has_base,
                   command=command, pins=[pin(n) for n in pins], started=started, finished=now(),
                   exit_code=code, priority=priority, priority_enforced=wrapper is not None,
                   wrapper=wrapper, tree_unchanged=unchanged, passed=passed,
                   status='passed' if passed else 'failed', log=str(log),
                   log_tail=log.read_text(errors='replace').splitlines()[-40:])
    out.write_text(json.dumps(receipt, indent=2) + '\n')
    return receipt


def status_problems(receipt):
    """Version and outcome problems that need no repository."""
    found = []
    if receipt.get('version') != VERSION:
        found.append(f'unknown receipt version {receipt.get("version")}')
    if not receipt.get('passed'):
        found.append(f'gate did not pass (exit {receipt.get("exit_code")}, tree unchanged {receipt.get("tree_unchanged")})')
    return found


def problems(receipt, repo, base_ref):
    """Why `receipt` does not license landing the repo's HEAD ([] = it does)."""
    found = status_problems(receipt)
    current = head(repo)
    if receipt.get('sha') != current:
        found.append(f'receipt is for {str(receipt.get("sha"))[:12]}, HEAD is {current[:12]}')
    if dirty(repo):
        found.append('working tree has uncommitted changes')
    base, has_base = contains(repo, base_ref, current)
    if base is None:
        found.append(f'base {base_ref} not found; fetch it first')
    elif not has_base:
        found.append(f'HEAD does not contain {base_ref} {base[:12]}; rebase and regate')
    return found


# ---- ranked gate lease (in-ledger slots table) ----------------------------------------------------------------

LEASE_SCHEMA = ("CREATE TABLE IF NOT EXISTS gate_tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,"
                " rank INTEGER NOT NULL, holder TEXT NOT NULL, slot INTEGER, requested REAL NOT NULL,"
                " heartbeat REAL NOT NULL)")
DEFAULT_SLOTS, DEFAULT_RESERVED, DEFAULT_RESERVED_RANK, DEFAULT_TTL = 5, 1, 2, 60.0
MAX_SLOTS = 32
AGING_S = 1800.0   # a waiting ticket gains one effective rank per 30 minutes, so P4 work cannot starve


def rank_for(priority):
    """Task priority P0..P4 -> rank 1..5. A lower rank is served first."""
    return PRIORITIES.index(priority) + 1


def _tx(conn):
    from .ledger.db import tx
    return tx(conn)


def lease_limits(slots, reserved):
    """(slots, reserved) from raw hub-setting values: slots clamped to 1..MAX_SLOTS, reserved to 0..slots-1.
    A value that is not an integer raises ValueError (the caller falls back to the default)."""
    slots = max(1, min(MAX_SLOTS, int(str(slots).strip())))
    return slots, max(0, min(int(str(reserved).strip()), slots - 1))


_BAD_LIMITS = {}   # setting key -> last invalid raw value logged


def configured_limits(get_setting):
    """(slots, reserved) from the hub settings `gate_slots`/`gate_reserved`, read through get_setting(key, default).
    Missing or invalid values use the defaults (an invalid one logs once per value); the result is clamped."""
    out = []
    for key, default in (("gate_slots", DEFAULT_SLOTS), ("gate_reserved", DEFAULT_RESERVED)):
        raw = get_setting(key, None)
        try:
            out.append(default if raw in (None, "") else int(str(raw).strip()))
        except ValueError:
            if _BAD_LIMITS.get(key) != raw:
                _BAD_LIMITS[key] = raw
                print(f"drover.gate: ignoring {key}={raw!r}: not an integer, using {default}", file=sys.stderr)
            out.append(default)
    return lease_limits(*out)


def lease_request(conn, task_id, priority, holder='gate', at=None):
    """Queue a ticket; returns its id. Order is rank, then ticket id (FIFO)."""
    at = time.time() if at is None else at
    conn.execute(LEASE_SCHEMA)
    with _tx(conn):
        return conn.execute("INSERT INTO gate_tickets(task_id,rank,holder,requested,heartbeat) VALUES(?,?,?,?,?)",
                            (task_id, rank_for(priority), holder, at, at)).lastrowid


def lease_acquire(conn, ticket, slots=DEFAULT_SLOTS, reserved=DEFAULT_RESERVED, reserved_rank=DEFAULT_RESERVED_RANK,
                  ttl=DEFAULT_TTL, at=None):
    """The ticket's slot number once it is the best waiting ticket that a free slot can serve, else None.

    The last `reserved` slots serve only tickets of rank <= reserved_rank (their own rank: aging never grants the reserve).
    Waiting gains one effective rank per 30 minutes. Tickets that stop heartbeating for
    `ttl` seconds are dropped, so a crashed gate frees its slot.
    """
    at = time.time() if at is None else at
    reserved = max(0, min(reserved, slots - 1))
    with _tx(conn):
        conn.execute("DELETE FROM gate_tickets WHERE heartbeat < ?", (at - ttl,))
        mine = conn.execute("SELECT * FROM gate_tickets WHERE id=?", (ticket,)).fetchone()
        if mine is None:
            raise GateError(f'ticket {ticket} expired or was cancelled')
        conn.execute("UPDATE gate_tickets SET heartbeat=? WHERE id=?", (at, ticket))
        if mine['slot'] is not None:
            return mine['slot']
        held = {r['slot'] for r in conn.execute("SELECT slot FROM gate_tickets WHERE slot IS NOT NULL")}
        free = [s for s in range(slots) if s not in held]
        general = slots - reserved
        waiting = conn.execute("SELECT id, rank, requested FROM gate_tickets WHERE slot IS NULL").fetchall()
        waiting.sort(key=lambda t: (t['rank'] - int(max(0.0, at - t['requested']) // AGING_S), t['id']))
        for t in waiting:
            eligible = [s for s in free if s < general or t['rank'] <= reserved_rank]
            if not eligible:
                continue
            pick = eligible[-1] if t['rank'] <= reserved_rank else eligible[0]  # top ranks use the reserve first
            free.remove(pick)
            if t['id'] == ticket:
                conn.execute("UPDATE gate_tickets SET slot=? WHERE id=?", (pick, ticket))
                return pick
    return None


def lease_heartbeat(conn, ticket, at=None):
    conn.execute("UPDATE gate_tickets SET heartbeat=? WHERE id=?", (time.time() if at is None else at, ticket))


def lease_release(conn, ticket):
    """Release or cancel a ticket; safe to call twice."""
    conn.execute("DELETE FROM gate_tickets WHERE id=?", (ticket,))


def lease_queue(conn):
    conn.execute(LEASE_SCHEMA)
    return [dict(r) for r in conn.execute("SELECT * FROM gate_tickets ORDER BY rank, id")]


def run_leased(db_path, task_id, priority, fn, *, slots=None, reserved=None,
               reserved_rank=DEFAULT_RESERVED_RANK, ttl=DEFAULT_TTL, poll=0.25, timeout=None, holder='gate'):
    """Wait for a slot in the ledger at `db_path` (slots/reserved default to the hub settings), call fn(slot) while a heartbeat thread keeps the lease, release."""
    from .ledger.db import connect
    conn = connect(db_path)
    if slots is None or reserved is None:       # the owner's gate_slots/gate_reserved, shared with the dispatcher
        from .ledger import ops
        cfg = configured_limits(lambda k, d: ops.get_setting(conn, k, d))
        slots, reserved = cfg[0] if slots is None else slots, cfg[1] if reserved is None else reserved
    ticket = lease_request(conn, task_id, priority, holder)
    stop, thread = threading.Event(), None
    try:
        deadline = None if timeout is None else time.monotonic() + timeout
        slot = lease_acquire(conn, ticket, slots, reserved, reserved_rank, ttl)
        while slot is None:
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError(f'no gate slot for {task_id} within {timeout}s')
            time.sleep(poll)
            slot = lease_acquire(conn, ticket, slots, reserved, reserved_rank, ttl)

        def beat():
            side = connect(db_path, migrate_db=False)
            while not stop.wait(ttl / 3):
                try:
                    lease_heartbeat(side, ticket)
                except sqlite3.Error:
                    pass
            side.close()
        thread = threading.Thread(target=beat, daemon=True)
        thread.start()
        return fn(slot)
    finally:
        stop.set()
        if thread:
            thread.join(5)
        try:
            lease_release(conn, ticket)
        finally:
            conn.close()


# ---- CLI ---------------------------------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m drover.gate', description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='action', required=True)
    r = sub.add_parser('run')
    r.add_argument('--out', required=True)
    r.add_argument('--log')
    r.add_argument('--repo', default='.')
    r.add_argument('--base', default='origin/main')
    r.add_argument('--pin', action='append', default=[])
    r.add_argument('--priority', choices=PRIORITIES, default='P2')
    r.add_argument('--memory-max', default='8G')
    r.add_argument('--max-seconds', type=int, default=1800)
    r.add_argument('--env', type=json.loads, default={})
    r.add_argument('--writable-roots', type=json.loads, default=[])
    r.add_argument('--json', action='store_true')
    r.add_argument('command', nargs=argparse.REMAINDER)
    v = sub.add_parser('verify')
    v.add_argument('--receipt', required=True)
    v.add_argument('--repo', default='.')
    v.add_argument('--base', default='origin/main')
    a = parser.parse_args(argv)
    try:
        if a.action == 'run':
            command = a.command[1:] if a.command[:1] == ['--'] else a.command
            rec = run_gate(a.repo, a.out, command, base=a.base, pins=a.pin, priority=a.priority,
                           memory_max=a.memory_max, max_seconds=a.max_seconds, log=a.log,
                           env={**os.environ, **a.env}, writable_roots=a.writable_roots)
            print(json.dumps(rec) if a.json else f'receipt {a.out}: sha {rec["sha"][:12]} exit {rec["exit_code"]}')
            return 0 if rec['passed'] else 1
        receipt = json.loads(Path(a.receipt).read_text())
        found = problems(receipt, Path(a.repo).resolve(), a.base)
        for text in found:
            print('refused: ' + text)
        if not found:
            print(f'receipt verified: {receipt["sha"][:12]}')
        return 1 if found else 0
    except GateError as e:
        print(f'drover.gate: {e}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
