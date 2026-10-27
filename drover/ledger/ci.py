"""Ranked CI runner tickets; all ticket SQL belongs to the ledger."""
import sys
import time

PRIORITIES = ('P0', 'P1', 'P2', 'P3', 'P4')


class SlotExpired(RuntimeError):
    pass


DEFAULT_SLOTS, DEFAULT_RESERVED, DEFAULT_RESERVED_RANK, DEFAULT_TTL = 5, 1, 2, 60.0
DEFAULT_WINDOWS_SLOTS = 1
MAX_SLOTS = 32
AGING_S = 1800.0   # a waiting ticket gains one effective rank per 30 minutes, so P4 work cannot starve


def rank_for(priority):
    """Task priority P0..P4 -> rank 1..5. A lower rank is served first."""
    return PRIORITIES.index(priority) + 1


def _tx(conn):
    from .db import tx
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
                print(f"drover.ci: ignoring {key}={raw!r}: not an integer, using {default}", file=sys.stderr)
            out.append(default)
    return lease_limits(*out)


def configured_windows_limit(get_setting):
    """Maximum simultaneous Windows gates; legacy tickets conservatively count as Windows."""
    raw = get_setting('gate_windows_slots', None)
    try:
        return max(1, min(MAX_SLOTS, DEFAULT_WINDOWS_SLOTS if raw in (None, '') else int(str(raw).strip())))
    except ValueError:
        if _BAD_LIMITS.get('gate_windows_slots') != raw:
            _BAD_LIMITS['gate_windows_slots'] = raw
            print(f"drover.ci: ignoring gate_windows_slots={raw!r}: not an integer, using {DEFAULT_WINDOWS_SLOTS}",
                  file=sys.stderr)
        return DEFAULT_WINDOWS_SLOTS


def lease_request(conn, task_id, priority, holder='gate', at=None, *, platform='windows'):
    """Queue a ticket; returns its id. Order is rank, then ticket id (FIFO)."""
    at = time.time() if at is None else at
    with _tx(conn):
        return conn.execute("INSERT INTO gate_tickets(task_id,rank,holder,requested,heartbeat,platform) VALUES(?,?,?,?,?,?)",
                            (task_id, rank_for(priority), holder, at, at, platform)).lastrowid


def lease_acquire(conn, ticket, slots=DEFAULT_SLOTS, reserved=DEFAULT_RESERVED, reserved_rank=DEFAULT_RESERVED_RANK,
                  ttl=DEFAULT_TTL, at=None, *, windows_slots=None):
    """The ticket's slot number once it is the best waiting ticket that a free slot can serve, else None.

    The last `reserved` slots serve only tickets of rank <= reserved_rank (their own rank: aging never grants the reserve).
    Waiting gains one effective rank per 30 minutes. Tickets that stop heartbeating for
    `ttl` seconds are dropped, except pinned external executions whose cessation
    still needs reconciliation. `windows_slots` optionally caps Windows executions;
    blocked Windows waiters do not prevent WSL admission. Existing acquired slots
    remain valid after a cap reduction.
    """
    at = time.time() if at is None else at
    reserved = max(0, min(reserved, slots - 1))
    with _tx(conn):
        conn.execute("DELETE FROM gate_tickets WHERE heartbeat < ? AND pinned=0", (at - ttl,))
        mine = conn.execute("SELECT * FROM gate_tickets WHERE id=?", (ticket,)).fetchone()
        if mine is None:
            raise SlotExpired(f'ticket {ticket} expired or was cancelled')
        conn.execute("UPDATE gate_tickets SET heartbeat=? WHERE id=?", (at, ticket))
        if mine['slot'] is not None:
            return mine['slot']
        held = {r['slot'] for r in conn.execute("SELECT slot FROM gate_tickets WHERE slot IS NOT NULL")}
        free = [s for s in range(slots) if s not in held]
        general = slots - reserved
        windows_held = conn.execute("SELECT COUNT(*) FROM gate_tickets WHERE slot IS NOT NULL AND platform='windows'").fetchone()[0]
        waiting = conn.execute("SELECT id, rank, requested, platform FROM gate_tickets WHERE slot IS NULL").fetchall()
        waiting.sort(key=lambda t: (t['rank'] - int(max(0.0, at - t['requested']) // AGING_S), t['id']))
        for t in waiting:
            if t['platform'] == 'windows' and windows_slots is not None and windows_held >= windows_slots:
                continue
            eligible = [s for s in free if s < general or t['rank'] <= reserved_rank]
            if not eligible:
                continue
            pick = eligible[-1] if t['rank'] <= reserved_rank else eligible[0]  # top ranks use the reserve first
            free.remove(pick)
            if t['platform'] == 'windows':
                windows_held += 1
            if t['id'] == ticket:
                conn.execute("UPDATE gate_tickets SET slot=? WHERE id=?", (pick, ticket))
                return pick
    return None


def lease_heartbeat(conn, ticket, at=None):
    conn.execute("UPDATE gate_tickets SET heartbeat=? WHERE id=?", (time.time() if at is None else at, ticket))


def lease_pin(conn, ticket):
    """Retain an acquired external execution's slot until explicit reconciliation."""
    with _tx(conn):
        changed = conn.execute('UPDATE gate_tickets SET pinned=1 WHERE id=? AND slot IS NOT NULL', (ticket,))
        if not changed.rowcount:
            raise SlotExpired(f'ticket {ticket} has no acquired slot to retain')


def lease_release(conn, ticket):
    """Release or cancel a ticket; safe to call twice."""
    conn.execute("DELETE FROM gate_tickets WHERE id=?", (ticket,))


def lease_queue(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM gate_tickets ORDER BY rank, id")]


class Runners:
    """Per-caller ranked CI reservations, renewed and released through one interface."""
    def __init__(self, conn, get_setting=None):
        from . import ops
        self.conn = conn
        self.get_setting = get_setting or (lambda key, default: ops.get_setting(conn, key, default))
        self.tickets = {}
        cleared = {e['data']['run'] for e in ops.events(conn, kind='launch-cancel-cleared')}
        cancelling = {e['task_id'] for e in ops.events(conn, kind='launch-cancel') if e['data']['run'] not in cleared}
        # finish_run saves this note before launch-cancel is appended. A crash in
        # between still means execution is uncertain until a clean scan clears it.
        cancelling.update(r['task_id'] for r in conn.execute(
            "SELECT id, task_id, note FROM runs WHERE role='gate' AND outcome='launch-error'")
            if r['id'] not in cleared and 'cancel-requested' in (r['note'] or ''))
        # Restore the durable reservations before any acquisition can expire them.
        for row in conn.execute("SELECT * FROM gate_tickets WHERE holder='dispatcher'").fetchall():
            active = conn.execute("SELECT 1 FROM runs WHERE task_id=? AND role='gate' AND ended IS NULL",
                                  (row['task_id'],)).fetchone()
            if row['pinned'] and not active and row['task_id'] not in cancelling:
                lease_release(conn, row['id'])  # crash after terminal ledger write, before release
                continue
            self.tickets[row['task_id']] = row['id']
            if active and row['slot'] is not None:
                lease_pin(conn, row['id'])

    def reserve(self, task, *, platform='windows'):
        tid = task['id']
        for _ in range(2):
            ticket = self.tickets.get(tid)
            if ticket is None:
                ticket = self.tickets[tid] = lease_request(self.conn, tid, task['priority'], holder='dispatcher', platform=platform)
            try:
                # A waiting request may select a different platform after restart/reconfiguration.
                self.conn.execute("UPDATE gate_tickets SET platform=? WHERE id=? AND slot IS NULL", (platform, ticket))
                return lease_acquire(self.conn, ticket, *configured_limits(self.get_setting), ttl=600,
                                     windows_slots=configured_windows_limit(self.get_setting))
            except SlotExpired:
                self.tickets.pop(tid, None)
        return None

    def pin(self, task_id):
        lease_pin(self.conn, self.tickets[task_id])

    def release(self, task_id):
        ticket = self.tickets.pop(task_id, None)
        if ticket is not None:
            lease_release(self.conn, ticket)

    def heartbeat(self, live_tasks):
        for tid, ticket in list(self.tickets.items()):
            if tid in live_tasks:
                lease_heartbeat(self.conn, ticket)

    def snapshot(self):
        waits = [time.time() - row['requested'] for row in lease_queue(self.conn) if row['slot'] is None]
        return {'waiting': len(waits), 'oldest_s': int(max(waits, default=0)),
                'slots': configured_limits(self.get_setting)[0]}
