"""A director as a resident run in a pane: what the runtime asks of the pane, answered from Herdr and the ledger.

A read decides a row's fate, and only a clear read may. Herdr unreachable, or any refusal but the missing-pane code,
changes no row: that is `unknown`, and unknown never spends a replacement. `gone` needs Herdr up and the pane missing or
its terminal changed. A `live` agent has left when Herdr has no agent there and the shell stands alone; the row retires.

A `starting` row outlives its launch only if the hub died, so past its deadline it ends `failed`. A `failed` row frees
its role only when its pane is gone or, once any late launch line has had time to land, holds no agent; such a pane
retires. A role whose failed pane holds an agent stays held for the owner.

A run that has ended is never resumed here: a fresh agent starts from the repo, the ledger and the brief. Only the
compaction stop resumes a session, on the run it interrupted (Task 13).
"""
import logging
import time
from datetime import datetime

from . import compaction_hold, config, pane_close, pane_handoff, pane_launch, pane_send, pane_wake, placement
from .herdr_client import HerdrRefused, HerdrUnavailable
from .launch import AdmissionHeld
from .ledger import disk_notices, ops_panes
from .notifications import Notifications, roles
from .pane_send import PaneRefused

ALIVE, GONE, UNKNOWN = 'alive', 'gone', 'unknown'
CHANGED, EMPTY = 'changed', 'empty'
MISSING_PANE = 'pane_not_found'
SESSION_TTL = 86400
RENEW_BELOW_S = SESSION_TTL / 2
OUTAGE = 'pane_herdr_outage'            # the open outage's notice id, so a hub restart does not publish it again
CLOSE_ENABLED = 'pane_close_enabled'    # the owner's switch: without it `pane_close` only reports
TRUTHY = ('true', '1', 'yes', 'on')
log = logging.getLogger(__name__)


def _age(stamp, now):
    return now - datetime.fromisoformat(stamp).timestamp()


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

    def maintain(self, run) -> None:
        """Keep one run. Nothing leaves this: a step that fails is logged, without its text, and the rest still run."""
        self._step(self.sweep)
        self._step(self._keep, run)

    def _step(self, action, *args):
        try:
            return action(*args)
        except Exception as error:      # no exception text: it may quote a path or a credential
            log.warning('pane maintenance step failed: %s', type(error).__name__)
            return None

    def _keep(self, run):
        row = ops_panes.by_run(self.conn, run['run_id'])
        if row is None or row['state'] not in pane_launch.RUNNING or run.get('stop_action_id') or self._stale(run):
            return
        seen = self.inspect(row)
        if seen in (ALIVE, UNKNOWN):
            self._step(self._renew, run)
        if seen == ALIVE and row['state'] == 'live':
            self._step(pane_wake.deliver, self, run)
            self._step(pane_handoff.maintain, self, run)

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
        if not compaction_hold.active():    # a checkout on the WSL side must not wake it
            pane_close.tick(self.port, self.herdr, self.now(), apply=self._close_enabled(), cfg=self._cfg)

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
        return {CHANGED: ALIVE, EMPTY: GONE}.get(seen, seen)

    def inspect(self, row):
        """What one read proves about a row: ALIVE, GONE, UNKNOWN, CHANGED (not the recorded agent) or EMPTY (a failed
        row whose pane holds no agent). The moves a read proves are made here, except EMPTY's, which `sweep` makes after
        the run has seen how its row ended.
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
            if _age(row['created'], self.now()) < pane_launch.START_DEADLINE_S:
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
        if _age(row['updated'], self.now()) < pane_launch.STARTUP_S:
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
        if not alone:
            return ALIVE
        ops_panes.update_agent(self.conn, row['id'], state='retired')
        self._event('pane-retired', row, 'agent-left')
        return GONE

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
        """The hub's part of the compaction stop. Task 13 fills it; until then no pane has anything to settle."""
        return dict(id=hold.get('id'), phase=hold.get('phase'), settled=True, agents=[])


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

    def launch(self, spec, credential, guard):
        service = self.service
        try:
            pane_launch.start(service.port, service.herdr, spec, credential, guard, cfg=service.cfg)
        except AdmissionHeld as held:
            if str(held) == 'herdr-unavailable':
                service.herdr_down()
            raise
        service.herdr_up()
        return PaneHandle(service, spec)

    def verify_resume(self, spec):
        """Never for an ended run: its session has no credential now, so the module's rule stands."""
        return False

    def probe(self, spec):
        return self.service.probe(spec)

    def adopt(self, spec):
        return PaneHandle(self.service, spec)

    def stop(self, spec):
        """One interrupt, as a request. A refusal is no request, so the runtime asks again on a later tick."""
        service = self.service
        row = ops_panes.by_run(service.conn, spec['run_id'])
        if row is None:
            return False
        try:
            pane_send.interrupt(service.port, service.herdr, row)
        except PaneRefused:
            return False
        return True

    def maintain(self, run):
        self.service.maintain(run)

    def sweep(self):
        self.service.sweep()
