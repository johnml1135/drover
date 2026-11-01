"""A director as a resident run in a pane: what the runtime asks of the pane, answered from Herdr and the ledger.

A read decides a row's fate, and only a clear read may. Herdr unreachable, or any refusal but the missing-pane code,
changes no row: that is `unknown`, and unknown never spends a replacement. `gone` needs Herdr up and the pane missing or
its terminal changed. A `live` agent that left its pane is not `gone` either: the row stays, its session is left to
expire, and one notice says closing the pane frees the role. Unknown with Herdr answering is a doubt that may not last:
past `UNKNOWN_LIMIT_S` it is one notice and one role question, and the session is left to expire.

A `starting` row outlives its launch only if the hub died, so past its deadline it ends `failed`. A `failed` row frees
its role only when its pane is gone or, once any late launch line has had time to land, holds no agent; such a pane
retires. A role whose failed pane holds an agent stays held for the owner.

A session that lapsed, or that a lifted STOP fenced, comes back on its own pane: a new run resumes it on a new row once
the pane's shell shows alone, with a new credential. Until then no interrupt reaches the agent and one notice says what
frees the pane. Ctrl+c ends a turn and not the CLI, so a pane holding an agent never gets a second one beside it; any
other refusal ends the row `failed`, flagged, and a fresh agent starts from the repo, the ledger and the brief.
"""
import logging
import time

from . import compaction_hold, config, pane_close, pane_compaction, pane_handoff, pane_launch, pane_send, pane_wake, placement
from .herdr_client import HerdrRefused, HerdrUnavailable
from .launch import AdmissionHeld
from .ledger import disk_notices, ops_panes
from .notifications import Conflict, Notifications, roles
from .pane_send import PaneRefused
from .resources import wsl_running

ALIVE, GONE, UNKNOWN = 'alive', 'gone', 'unknown'
CHANGED, EMPTY, LEFT = 'changed', 'empty', 'left'
MISSING_PANE = 'pane_not_found'
SESSION_TTL = 86400
RENEW_BELOW_S = SESSION_TTL / 2
UNKNOWN_LIMIT_S = 1800
OUTAGE = 'pane_herdr_outage'            # the open outage's notice id, so a hub restart does not publish it again
CLOSE_ENABLED = 'pane_close_enabled'    # the owner's switch: without it `pane_close` only reports
TRUTHY = ('true', '1', 'yes', 'on')
log = logging.getLogger(__name__)


def _unknown_id(row, since):
    return f"pane-unknown:{row['id']}:{since}"


def _same_folder(one, other):
    if not one or not other:
        return False
    one, other = (placement.to_windows_path(path).rstrip('\\/').casefold() for path in (one, other))
    return one == other


class PaneService:
    """`maintain` keeps one run; `sweep` keeps the rows no run may be looking after."""

    def __init__(self, port, herdr, *, cfg=None, now=time.time):
        self.port, self.herdr, self._cfg, self.now = port, herdr, cfg, now
        self.conn = port.conn
        self.notifications = Notifications(port.conn, now=now)

    @property
    def cfg(self):
        return config.get() if self._cfg is None else self._cfg

    def maintain(self, run, guard) -> None:
        """Keep one run. Nothing leaves this: a step that fails is logged, without its text, and the rest still run."""
        self._step(self.sweep)
        self._step(self._keep, run, guard)

    def _step(self, action, *args):
        try:
            return action(*args)
        except Exception as error:      # no exception text: it may quote a path or a credential
            log.warning('pane maintenance step failed: %s', type(error).__name__)
            return None

    def _keep(self, run, guard):
        row = ops_panes.by_run(self.conn, run['run_id'])
        if row is None or row['state'] not in pane_launch.RUNNING:
            return
        if row['state'] == pane_handoff.ROTATING:       # it was told to leave, so no fence or stop has more to say to it
            return self._step(pane_handoff.follow_exit, self, run)
        if run.get('stop_action_id') or self._stale(run):
            return
        paused = False
        try:
            guard()
        except AdmissionHeld as held:   # a pause starts nothing new; any other fence is `recover`'s to act on
            if str(held) != roles.DISPATCH_PAUSED:
                return
            paused = True
        seen = self.inspect(row)
        lasting = self._lasting(row, seen)
        if seen == ALIVE or (seen == UNKNOWN and not lasting):
            self._step(self._renew, run)
        if seen not in (ALIVE, LEFT) or row['state'] != 'live':
            return
        launched = self._step(lambda: pane_launch.advance(self.port, self.herdr, row, run['spec'], guard, self.now(), cfg=self.cfg))
        if launched and seen == ALIVE and not paused:
            self._step(pane_wake.deliver, self, run)
            self._step(pane_handoff.maintain, self, run)

    def _lasting(self, row, seen):
        """True past the limit of unknown reads with Herdr answering. An outage has its own notice, so it does not count."""
        key = f"pane_unknown:{row['id']}"
        since = self.port.get_setting(key)
        doubt = seen == UNKNOWN and row['state'] == 'live' and not self.port.get_setting(OUTAGE)
        if not doubt:
            if since:
                self.port.set_setting(key, '')
                disk_notices.retract(self.conn, _unknown_id(row, since), kind='pane.notice', actor='panes')
            return False
        if not since:
            self.port.set_setting(key, str(int(self.now())))
            return False
        if self.now() - float(since) < UNKNOWN_LIMIT_S:
            return False
        pane_launch.publish(self.port, _unknown_id(row, since), 'A director cannot be read',
                            f"Seat {row['seat']} ({row['placement']}), pane {row['pane_id']}: Herdr answers but says nothing that "
                            'proves the agent. It gets no wakes and its session is left to expire.')
        return True

    def overdue(self, spec):
        """Whether a run's unknown has lasted, as `_lasting` last found it: the role question follows the notice."""
        row = ops_panes.by_run(self.conn, spec['run_id'])
        since = row and self.port.get_setting(f"pane_unknown:{row['id']}")
        return bool(since) and self.now() - float(since) >= UNKNOWN_LIMIT_S

    def revivable(self, spec):
        """A lapsed run may end for a resume only when a read shows its pane's shell alone. Otherwise the owner hears why not."""
        row = ops_panes.by_run(self.conn, spec['run_id'])
        if row is None or row['state'] != 'live':
            return False
        seen = self.inspect(row)
        self._lasting(row, seen)        # `_keep` stops at the fence, so this read is the lapsed run's clock
        if seen not in (ALIVE, CHANGED, LEFT):
            return False
        try:
            roles.admission(self.notifications, spec['role_id'])
            alone = pane_send.shell_alone(self.herdr, row)
        except (Conflict, PaneRefused):
            return False                # a pause starts no new agent, and a failed read proves nothing
        if not alone:
            pane_launch.publish(self.port, f"pane-lapsed:{row['id']}", 'A director lost its session',
                                f"Seat {row['seat']} ({row['placement']}), pane {row['pane_id']}: the agent has no session now and gets "
                                'no wakes. Drover resumes it here once it has exited; closing the pane frees the role.')
        return alone

    def blocked(self, role_id, cause):
        """One notice for one cause of one role, whoever restarts, closed when the cause clears or changes."""
        key = f'pane_blocked:{role_id}'
        current = self.port.get_setting(key)
        prefix = f'pane-blocked:{role_id}:{cause}:'
        if current and not (cause and current.startswith(prefix)):
            disk_notices.retract(self.conn, current, kind='pane.notice', actor='panes')
            self.port.set_setting(key, '')
            current = ''
        if cause and not current:
            notice_id = f'{prefix}{int(self.now())}'
            pane_launch.publish(self.port, notice_id, 'A director cannot start',
                                f'{role_id}: {cause}. Its mail waits, and it starts once this clears.')
            self.port.set_setting(key, notice_id)

    def orphan(self, role_id):
        """The role's `live` row while no run holds it: only a lapsed run leaves one, and a resume takes it up."""
        return next((row for row in ops_panes.open_agents(self.conn) if row['role_id'] == role_id and row['state'] == 'live'), None)

    def rotated(self, role_id):
        """The pane a rotated agent left, while nothing has taken it."""
        return next((row for row in reversed(ops_panes.open_agents(self.conn))
                     if row['role_id'] == role_id and row['state'] == 'retired' and row['handoff_state'] == 'rotated'), None)

    def _stale(self, run):
        return self.notifications.role_state(run['role_id'])['generation'] != run['generation']

    def _renew(self, run):
        session = self.conn.execute('SELECT session_id,expires FROM director_sessions WHERE role_id=? AND generation=?',
                                    (run['role_id'], run['generation'])).fetchone()
        if session and session['expires'] - self.now() < RENEW_BELOW_S:
            renewed = roles.renew_session(self.notifications, run['role_id'], session['session_id'], ttl=SESSION_TTL)
            self.port.append_event('director.session-renewed', dict(role_id=run['role_id'], generation=renewed['generation']),
                                   actor='panes')

    def sweep(self) -> None:
        """Rows left behind by a launch, and the panes retired so far. The runtime calls this when no run is active."""
        for row in ops_panes.open_agents(self.conn):
            if row['started_by'] == 'drover' and row['state'] in ('starting', 'failed'):
                self._step(self._reconcile, row)
        if not compaction_hold.active() and self._wsl_safe():
            pane_close.tick(self.port, self.herdr, self.now(), apply=self._close_enabled(), cfg=self._cfg)

    def _wsl_safe(self):
        """Judging a pane reads its checkout, and one on the WSL side must not wake it."""
        reads = any(row['state'] == 'retired' and row['cwd'] and placement.routes_through_wsl(row['cwd'])
                    for row in ops_panes.open_agents(self.conn))
        return not reads or wsl_running(placement.distro(self.cfg['paths']['wsl_distro']))

    def _close_enabled(self):
        return str(self.port.get_setting(CLOSE_ENABLED, 'false')).lower() in TRUTHY

    def _reconcile(self, row):
        if self.inspect(row) == EMPTY:
            ops_panes.update_agent(self.conn, row['id'], state='retired')
            self._event('pane-retired', row, 'no-agent')

    def _event(self, kind, row, reason):
        self.port.append_event(kind, dict(row=row['id'], pane_id=row['pane_id'], seat=row['seat'], placement=row['placement'],
                                          reason=reason), actor='panes')

    def probe(self, spec):
        row = ops_panes.by_run(self.conn, spec['run_id'])
        if row is None or row['state'] == 'retired':
            return GONE
        seen = self.inspect(row)
        return {CHANGED: ALIVE, EMPTY: GONE, LEFT: UNKNOWN}.get(seen, seen)

    def inspect(self, row):
        """What one read proves about a row: ALIVE, GONE, UNKNOWN, CHANGED (not the recorded agent), LEFT (a live row's
        agent has exited) or EMPTY (a failed row whose pane holds no agent). The moves a read proves are made here, except
        EMPTY's, which `sweep` makes after the run has seen how its row ended.
        """
        if row['state'] == 'compaction':
            return ALIVE
        try:
            pane = self.herdr.call('pane', 'get', row['pane_id']).get('pane')
        except HerdrUnavailable:
            self.herdr_down()
            return UNKNOWN
        except HerdrRefused as refused:
            self.herdr_up()
            return self._end(row, 'pane-missing') if refused.code == MISSING_PANE else UNKNOWN
        self.herdr_up()
        terminal = pane.get('terminal_id') if isinstance(pane, dict) else None
        if not terminal:
            return UNKNOWN
        if row['terminal_id'] and terminal != row['terminal_id']:
            return self._end(row, 'terminal-changed')
        if row['state'] == 'starting':
            if pane_launch.age(row['created'], self.now()) < pane_launch.START_DEADLINE_S:
                return UNKNOWN
            row = pane_launch.expire(self.port, self.herdr, row)
        if row['state'] == 'failed':
            return self._failed(row)
        return self._occupant(row, pane)

    def _end(self, row, reason):
        ops_panes.update_agent(self.conn, row['id'], state='gone')
        self._event('pane-gone', row, reason)
        return GONE

    def _failed(self, row):
        if pane_launch.age(row['updated'], self.now()) < pane_launch.STARTUP_S:
            return UNKNOWN              # a launch line that stalled may still land
        try:
            return EMPTY if pane_send.shell_alone(self.herdr, row) else UNKNOWN
        except PaneRefused:
            return UNKNOWN

    def _occupant(self, row, pane):
        try:
            agent = self.herdr.call('agent', 'get', row['pane_id']).get('agent')
            if isinstance(agent, dict):
                return self._compare(row, pane, agent)
            return UNKNOWN
        except HerdrUnavailable:
            self.herdr_down()
            return UNKNOWN
        except HerdrRefused as refused:
            if refused.code != 'agent_not_found':
                return UNKNOWN
        try:                            # Herdr forgets agents too, so its silence needs the process list to agree
            alone = pane_send.shell_alone(self.herdr, row)
        except PaneRefused:
            return UNKNOWN
        if not alone or row['state'] == pane_handoff.ROTATING:    # leaving is what a rotating agent was asked to do
            return ALIVE
        pane_launch.publish(self.port, f"pane-agent-left:{row['id']}", 'A director left its pane',
                            f"Seat {row['seat']} ({row['placement']}), pane {row['pane_id']}: the agent has exited. "
                            'Closing the pane frees the role.')
        return LEFT

    def _compare(self, row, pane, agent):
        session, kind = pane_launch.session_of(agent), agent.get('agent')
        if (session and session != row['session_id']) or (kind and kind != row['kind']):
            pane_launch.publish(self.port, f"pane-occupant:{row['id']}", 'A pane holds a different agent',
                               f"Seat {row['seat']} ({row['placement']}), pane {row['pane_id']}: the agent there is not the one "
                               'Drover started. No wake goes to it and the pane stays open.')
            return CHANGED
        if not agent.get('name') and row['name'] and _same_folder(pane.get('cwd'), row['cwd']):
            try:
                self.herdr.call('agent', 'rename', row['pane_id'], row['name'])
            except (HerdrUnavailable, HerdrRefused):
                pass                    # a name is a label, and the next read tries again
        return ALIVE

    def herdr_down(self):
        """One notice for one outage, however many reads fail and whoever restarts."""
        if not self.port.get_setting(OUTAGE):
            notice_id = f'pane-herdr-closed:{int(self.now())}'
            pane_launch.publish(self.port, notice_id, 'Herdr cannot be reached',
                               'Directors in panes get no input until it is back. Workers keep running and mail waits in '
                               'the inbox.')
            self.port.set_setting(OUTAGE, notice_id)

    def herdr_up(self):
        notice_id = self.port.get_setting(OUTAGE)
        if notice_id:
            disk_notices.retract(self.conn, notice_id, kind='pane.notice', actor='panes')
            self.port.set_setting(OUTAGE, '')

    def compaction_tick(self, hold) -> dict:
        """The hub's part of the compaction: a step of the stop or the resume, or, with no hold, what the last one left."""
        return pane_compaction.tick(self, hold)


class PaneHandle:
    """A run's pane as the runtime polls it. It reads the ledger only: `inspect` is what moves a row."""

    def __init__(self, service, spec):
        self.service, self.spec = service, spec

    def poll(self) -> dict | None:
        row = ops_panes.by_run(self.service.conn, self.spec['run_id'])
        if row is None or row['state'] == 'retired':
            return dict(status='pane-gone', usage={})
        return dict(status='launch-failed', usage={}) if row['state'] == 'failed' else None


class PaneDirectorAdapter:
    capabilities = dict(resident=True, live_offer=True, verified_resume=True, fresh_start=True, enforced_token_cap=False,
                        authenticated_processing=True, secure_handoff=True)
    session_ttl = SESSION_TTL

    def __init__(self, service):
        self.service = service

    def ready(self, role_id=None, launch=None) -> bool:
        """Whether a launch may be tried, the role's own if given. No costs nothing: Herdr may be closed, a workspace or a
        checkout missing, and no launch may stand in a compaction's way.
        """
        service = self.service
        if compaction_hold.active():
            return False
        if not service.herdr.available():
            service.herdr_down()
            return False
        service.herdr_up()
        if launch is None:
            return True
        cause = pane_launch.blocked(service.port, service.herdr, role_id, launch, cfg=service.cfg,
                                    resuming=service.orphan(role_id) is not None)
        if cause == pane_launch.HERDR_CLOSED:
            service.herdr_down()
            return False
        service.blocked(role_id, cause)
        return cause is None

    def overdue(self, spec):
        return self.service.overdue(spec)

    def revivable(self, spec):
        return self.service.revivable(spec)

    def _resume(self, spec, credential, guard):
        """The pane a lapsed run left, taken up again. False sends the launch to a fresh pane, past `start`'s own read."""
        service = self.service
        row = service.orphan(spec['role_id'])
        if row is None:
            return False
        guard()
        try:
            pane_launch.resume(service.port, service.herdr, row, spec, cfg=service.cfg, credential=credential)
        except AdmissionHeld as held:
            seen = service.inspect(row)
            if seen == UNKNOWN:         # only a clear read may end a row: this one proved nothing
                raise AdmissionHeld(str(held) if service.herdr.available() else pane_launch.HERDR_CLOSED) from None
            if seen != GONE:
                pane_launch.expire(service.port, service.herdr, row, f'resume refused: {held}',
                                   outcome='A fresh agent starts in a new pane once this one shows none.')
            return False
        return True

    def _rotate(self, spec, credential, guard):
        """The pane a rotated agent left, for its fresh session. False sends the launch to a fresh pane, flagged if this one refused."""
        service = self.service
        row = service.rotated(spec['role_id']) if spec.get('handoff') else None
        if row is None:
            return False
        guard()
        try:
            pane_launch.rotate(service.port, service.herdr, row, spec, cfg=service.cfg, credential=credential)
        except AdmissionHeld as held:
            if str(held) == pane_launch.HERDR_CLOSED:
                raise
            pane_launch.publish(service.port, f"pane-rotation-moved:{row['id']}", 'A rotated director moved to a new pane',
                                f"Seat {row['seat']} ({row['placement']}), pane {row['pane_id']}: {held}. The new session starts in a "
                                'fresh pane, and this one is left for you to close.')
            return False
        return True

    def launch(self, spec, credential, guard):
        service = self.service
        try:
            if not (self._resume if spec.get('mode') == 'resume' else self._rotate)(spec, credential, guard):
                pane_launch.start(service.port, service.herdr, spec, credential, guard, cfg=service.cfg)
        except AdmissionHeld as held:
            if str(held) == 'herdr-unavailable':
                service.herdr_down()
            raise
        service.herdr_up()
        return PaneHandle(service, spec)

    def verify_resume(self, spec):
        """True while a `live` row outlives its run: that pane is where the role's next run belongs.

        A pane that cannot be read raises, so the runtime holds the role before it spends a launch.
        """
        row = self.service.orphan(spec['role_id'])
        seen = row and self.service.inspect(row)
        if seen == UNKNOWN:
            raise AdmissionHeld('pane-unread')
        return seen not in (None, GONE)

    def probe(self, spec):
        return self.service.probe(spec)

    def adopt(self, spec):
        return PaneHandle(self.service, spec)

    def stop(self, spec):
        """One interrupt, as a request. A refusal is no request, so the runtime asks again on a later tick.

        Ctrl+c ends a turn and not the CLI, so the agent stays until it exits, and the owner hears that either way.
        """
        service = self.service
        row = ops_panes.by_run(service.conn, spec['run_id'])
        if row is None:
            return False
        pane_launch.publish(service.port, f"pane-stopped:{row['id']}", 'A director was told to stop',
                            f"Seat {row['seat']} ({row['placement']}), pane {row['pane_id']}: it gets no wakes now. Drover resumes "
                            'it here once it has exited and work may go on; closing the pane frees the role.')
        try:
            pane_send.interrupt(service.port, service.herdr, row)
        except PaneRefused:
            return False
        return True

    def maintain(self, run, guard):
        self.service.maintain(run, guard)

    def sweep(self):
        self.service.sweep()
