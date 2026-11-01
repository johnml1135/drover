"""The hub's part of the overnight compaction: Drover-started agents in WSL panes are stopped, and come back after it.

This is the one exception to the pane-write rule. Windows panes and owner-started agents are never named here. All
input goes through `pane_send`, and the elevated job only ever reads the answer this module saves on each tick.

Stop: "stop now" once, and one interruption at ASK_S if the agent is still not idle. Both count from `hold['since']`, so
a hub restart does not start them over. What each agent is sent is a ledger event written before the send, so no restart
repeats it. A refusal is final: the agent gets no further input and its row waits for the job's kill, which comes at
ASK_S + INTERRUPT_S and not sooner. Only an idle read on a tick after the stop counts: on the tick that sent it the agent
has not seen it yet.

Resume: a fenced or STOPped run comes first and stays stopped: no launch, no text, one notice. Otherwise the recorded
session comes back on a new row in the same pane, and "restart complete, keep going" follows once that pane passes every
check. A failure is settled by a read of the pane. No agent there: the row and the run end as `compaction-fresh`, which
spends no replacement, the role is signalled, and as launches wait for the release its next tick starts a fresh agent in
a new pane from the repo, the ledger and the brief. An agent there: it keeps its session and its row stays `live`, flagged
as `resumed-untold`, and the wake route tells it. No read: nothing changes and the next tick reads again. The job's
report flags each with its seat and placement.

A hold that ends with a row still in `compaction` had no resume phase, as after a failed restart or a job that died, so
such a row is settled the same way with a notice of its own, once a clean read proves the hold is over.
"""
import json
import logging

from . import compaction_hold, config, pane_launch, pane_read, pane_screen, pane_send
from .directors import COMPACTION_FRESH, DirectorRuntime
from .launch import AdmissionHeld
from .ledger import ops_panes
from .ledger.db import tx
from .notifications import roles
from .pane_send import PaneRefused

STOP_TEXT = 'stop now'
RESUME_TEXT = 'restart complete, keep going'
EVENT = 'pane-compaction'
FRESH_EVENT = 'pane-compaction-fresh'
STOP, RESUME = 'stop', 'resume'
ASKED, INTERRUPTED, LEFT = 'stopped-asked', 'stopped-interrupted', 'left-for-kill'
RESUMED, RESUME_FAILED, UNTOLD, STAYED_STOPPED = 'resumed', 'resume-failed', 'resumed-untold', 'stayed-stopped'
STOPPED = (ASKED, INTERRUPTED)
OVER = (RESUMED, RESUME_FAILED, UNTOLD, STAYED_STOPPED)
MISSING_PANE = 'herdr-refused:pane_not_found'
STOP_STEPS = ('hold', 'stop')       # once the job has moved past these, nothing reaches a pane before the resume
PUBLIC = ('pane_id', 'seat', 'placement', 'session_id')
RECORDED = (*PUBLIC, 'run_id', 'role_id')
log = logging.getLogger(__name__)


class Night:
    """One tick's view of one hold: what each agent has been sent, read back from the ledger, by row id."""

    def __init__(self, service, hold):
        self.service, self.hold = service, hold
        self.port, self.herdr, self.conn = service.port, service.herdr, service.conn
        self.agents = {}
        for event in self.port.events(EVENT):
            if event['data'].get('hold') == hold['id']:
                self.agents.setdefault(event['data']['row'], {}).update(event['data'])

    def note(self, row_id, **facts):
        self.port.append_event(EVENT, dict(hold=self.hold['id'], row=row_id, **facts), actor='panes')
        self.agents.setdefault(row_id, {}).update(facts)

    def take(self, row):
        """Seat, placement and session go on record before any input."""
        ops_panes.update_agent(self.conn, row['id'], state='compaction')
        self.note(row['id'], **{name: row[name] for name in RECORDED})

    def stopped(self, row_id, outcome, reason=None):
        self.note(row_id, outcome=outcome, stopped=outcome, reason=reason)

    def left(self, row_id, reason):
        if reason == pane_launch.HERDR_CLOSED:
            self.service.herdr_down()
        self.stopped(row_id, LEFT, reason)

    def answer(self, phase, settled):
        agents = [{name: facts.get(name) for name in (*PUBLIC, 'outcome', 'reason', 'stopped')}
                  for _, facts in sorted(self.agents.items())]
        return dict(id=self.hold['id'], phase=phase, settled=settled, agents=agents)

    def save(self, answer):
        try:
            compaction_hold.save_panes(config.state_dir(self.service.cfg), answer)
        except OSError:
            pass                        # the job reads it again; the next tick writes it again
        return answer


def _owned(service):
    return [row for row in ops_panes.open_agents(service.conn, placement='wsl')
            if row['started_by'] == 'drover' and row['state'] in pane_launch.RESUMABLE]


def _guarded(action, *args):
    """One agent's trouble is that agent's alone. No exception text: it may quote a path or a credential."""
    try:
        action(*args)
    except Exception as error:
        log.warning('pane compaction step failed: %s', type(error).__name__)
        return True
    return False


def _idle(night, row):
    seen = pane_send.observe(night.herdr, row)
    return seen['agent'].get('agent_status') in pane_read.HERDR_READY and pane_screen.status(seen['screen'], row['kind']) == 'idle'


def _write(night, row, write):
    """One write. A refusal or an unproved write ends this agent's input: it goes to the kill."""
    try:
        written = write()
    except PaneRefused as refused:
        return night.left(row['id'], refused.reason)
    if written['state'] != 'sent':
        night.left(row['id'], written['reason'] or written['state'])


def _stop_one(night, row, elapsed, deadline):
    facts = night.agents[row['id']]
    if not facts.get('asked'):
        if deadline:
            return night.left(row['id'], 'no-time-left')
        night.note(row['id'], asked=True)
        return _write(night, row, lambda: pane_send.send_stop(night.port, night.herdr, row, STOP_TEXT, night.hold['id']))
    try:
        idle = _idle(night, row)
    except PaneRefused as refused:
        return night.left(row['id'], refused.reason)
    if idle:
        night.stopped(row['id'], INTERRUPTED if facts.get('interrupted') else ASKED)
    elif deadline:
        night.left(row['id'], 'still-working')
    elif elapsed >= compaction_hold.ASK_S and not facts.get('interrupted'):
        night.note(row['id'], interrupted=True)
        _write(night, row, lambda: pane_send.interrupt(night.port, night.herdr, row))


def _stop_tick(night):
    elapsed = night.service.now() - night.hold.get('since', night.service.now())
    deadline = elapsed >= compaction_hold.ASK_S + compaction_hold.INTERRUPT_S
    moved_on = night.hold.get('step') not in STOP_STEPS
    rows = _owned(night.service)
    for row in rows:
        if row['id'] not in night.agents:
            night.take(row)
        if night.agents[row['id']].get('outcome'):
            continue
        if moved_on:
            night.left(row['id'], 'no-time-left')
        elif _guarded(_stop_one, night, row, elapsed, deadline):
            night.left(row['id'], 'stop-failed')
    held = {row['id'] for row in rows}
    for row_id, facts in night.agents.items():
        if row_id not in held and not facts.get('outcome'):
            night.left(row_id, 'row-ended')
    return night.answer(STOP, deadline or moved_on or all(facts.get('outcome') in STOPPED for facts in night.agents.values()))


def _in_pane(service, row):
    """What one read proves: True an agent is there, False none is, None nothing. Only a read may end anything."""
    try:
        return not pane_send.shell_alone(service.herdr, row)
    except PaneRefused as refused:
        if refused.reason == MISSING_PANE:
            return False
        if refused.reason == pane_launch.HERDR_CLOSED:
            service.herdr_down()
        return None


def _fence(service, run_id):
    """Why this run may not go on, or None. A pause starts nothing new but ends no resident's session, so it is no fence."""
    run = service.conn.execute("SELECT spec,stop_action_id FROM director_runs WHERE run_id=? AND state<>'finished'", (run_id,)).fetchone()
    if run is None:
        return None
    if run['stop_action_id']:
        return 'stopped'
    try:
        DirectorRuntime(service.port, service, now=service.now)._guard(json.loads(run['spec']))
    except AdmissionHeld as held:
        return None if str(held) == roles.DISPATCH_PAUSED else str(held)
    return None


def _signal(service, row, reason):
    if row.get('role_id'):
        event = service.port.append_event(FRESH_EVENT, dict(role_id=row['role_id'], run_id=row.get('run_id'), reason=reason), actor='panes')
        with tx(service.conn):
            roles.signal(service.conn, row['role_id'], event)


def _end_row(service, row, reason, *, notice):
    if row['state'] in pane_launch.RUNNING:
        pane_launch.expire(service.port, service.herdr, row, f'resume failed: {reason}', notice=notice,
                           outcome='A fresh agent starts in a new pane once this one shows none.')


def _start_fresh(service, row, reason):
    """Only on a read showing no agent. The run ends clean, and the wake goes after `finish`, which works it out again."""
    if row.get('run_id'):
        DirectorRuntime(service.port, service, now=service.now).finish(row['run_id'], dict(status=COMPACTION_FRESH, usage={}))
    _signal(service, row, reason)


def _keep_row(service, row):
    """The agent is up, so its row is `live` again and nothing the hold wrote stands in the way of the next wake."""
    for write in ops_panes.open_writes(service.conn, row['id']):
        if write['kind'] in ('stop', 'interrupt', RESUME):
            ops_panes.finish_write(service.conn, write['id'], 'observed', 'agent-kept')
    if row['state'] == 'compaction':
        ops_panes.update_agent(service.conn, row['id'], state='live')


def _keep_agent(service, row, reason):
    """It keeps its session. The wake route tells it, when the pane lets it."""
    _keep_row(service, row)
    _signal(service, row, reason)


def _stay_stopped(service, row, why):
    """No launch and no text: the runtime's stop path has the run. False until a read says what the pane holds."""
    seen = _in_pane(service, row)
    if seen is None:
        return False
    if seen:
        _keep_row(service, row)
    elif row['state'] in pane_launch.RUNNING:
        ops_panes.update_agent(service.conn, row['id'], state='retired')
    pane_launch.publish(service.port, f"pane-compaction-stopped:{row['id']}", 'A stopped director was not resumed',
                        f"Seat {row['seat']} ({row['placement']}), pane {row['pane_id']}: {why}. It stays stopped after the compaction.")
    return True


def _newest(night, row_id):
    """The row that speaks for this pane now: a resume gives it a second one."""
    return ops_panes.by_pane(night.conn, night.agents[row_id]['pane_id']) or _row(night.conn, row_id)


def _failed(night, row_id, reason):
    """Every effect here may run twice, so the note comes last and a crash is only ever repeated."""
    row = _newest(night, row_id)
    seen = _in_pane(night.service, row)
    if seen is False and row['id'] != row_id and pane_launch.age(row['updated'], night.service.now()) < pane_launch.STARTUP_S:
        seen = None                 # a launch line that stalled may still land
    if seen is None:
        return night.note(row_id, outcome=None, reason=reason, failure=reason)
    if seen and row['state'] in pane_launch.RESUMABLE:
        _keep_agent(night.service, row, reason)
        return night.note(row_id, outcome=UNTOLD, reason=reason)
    if not seen:
        _end_row(night.service, row, reason, notice=False)
        _start_fresh(night.service, night.agents[row_id], reason)
    return night.note(row_id, outcome=RESUME_FAILED, reason=reason)


def _row(conn, row_id):
    row = conn.execute('SELECT * FROM pane_agents WHERE id=?', (row_id,)).fetchone()
    return dict(row) if row else None


def _spec(conn, run_id):
    row = conn.execute("SELECT spec FROM director_runs WHERE run_id=? AND state<>'finished'", (run_id,)).fetchone()
    return json.loads(row['spec']) if row else None


def _relaunch(night, row_id):
    """The session back on a new row: True when a live one holds it. One try, as a launch always is."""
    facts = night.agents[row_id]
    if facts.get('relaunched'):
        return _failed(night, row_id, facts.get('failure') or 'resume-cut-short')
    row, spec = _row(night.conn, row_id), _spec(night.conn, facts.get('run_id'))
    if spec is None:
        return _failed(night, row_id, 'no-open-run')
    night.note(row_id, relaunched=True)
    try:
        fresh = pane_launch.resume(night.port, night.herdr, row, spec, cfg=night.service.cfg)
    except AdmissionHeld as held:
        return _failed(night, row_id, str(held))
    if fresh['state'] != 'live':
        return _failed(night, row_id, 'agent-did-not-come-back')
    night.note(row_id, fresh=fresh['id'])
    return True


def _fenced(night, row_id):
    why = _fence(night.service, night.agents[row_id].get('run_id'))
    if why and _stay_stopped(night.service, _newest(night, row_id), why):
        night.note(row_id, outcome=STAYED_STOPPED, reason=why)
    return bool(why)


def _resume_one(night, row_id, expired):
    facts = night.agents[row_id]
    if 'fresh' not in facts:
        if _fenced(night, row_id):
            return None
        if expired:
            return _failed(night, row_id, facts.get('failure') or 'no-time-left')
        if not _relaunch(night, row_id):
            return None
    if _fenced(night, row_id):      # a launch takes its time, and a STOP can land in it
        return None
    try:
        written = pane_send.send(night.port, night.herdr, _row(night.conn, facts['fresh']), RESUME_TEXT, RESUME)
    except PaneRefused as refused:      # it may still be coming up, so only the deadline makes this a failure
        return _failed(night, row_id, refused.reason) if expired or refused.reason == pane_launch.HERDR_CLOSED else None
    if written['state'] != 'sent':
        return _failed(night, row_id, written['reason'] or written['state'])
    return night.note(row_id, outcome=RESUMED, reason=None)


def _resume_tick(night):
    expired = night.service.now() - night.hold['at'] >= compaction_hold.RESUME_S
    resumed = {facts.get('fresh') for facts in night.agents.values()}
    for row in _owned(night.service):           # a hub that was down for the stop still brings these back
        if row['id'] not in night.agents and row['id'] not in resumed:
            night.take(row)
            night.stopped(row['id'], LEFT, 'no-time-left')
    night.save(night.answer(RESUME, False))     # a launch takes its time, and the job is counting
    for row_id, facts in list(night.agents.items()):
        if facts.get('outcome') not in OVER and _guarded(_resume_one, night, row_id, expired):
            _guarded(_failed, night, row_id, 'resume-error')
    return night.answer(RESUME, expired or all(facts.get('outcome') in OVER for facts in night.agents.values()))


def _leftover(service, row):
    why = _fence(service, row['run_id'])
    if why:
        return _stay_stopped(service, row, why)
    seen = _in_pane(service, row)
    if seen:
        _keep_agent(service, row, 'hold-ended')
        return pane_launch.publish(service.port, f"pane-compaction-kept:{row['id']}", 'A director outlived a compaction',
                                   f"Seat {row['seat']} ({row['placement']}), pane {row['pane_id']}: the hold ended with the agent "
                                   'still in its pane. It keeps its session and hears by the next wake.')
    if seen is False:
        _end_row(service, row, 'hold-ended', notice=True)
        _start_fresh(service, row, 'hold-ended')
    return None


def _leftovers(service):
    """Rows a hold left in `compaction`, once a clean read proves it over: nothing else would ever settle them."""
    rows = [row for row in ops_panes.open_agents(service.conn) if row['state'] == 'compaction']
    if rows and compaction_hold.over(config.state_dir(service.cfg)):
        for row in rows:
            _guarded(_leftover, service, row)
    return dict(id=None, phase=None, settled=True, agents=[])


def tick(service, hold) -> dict:
    """One step of the stop or the resume, with the answer saved for the job. With no hold, settle what the last one left."""
    if not hold:
        return _leftovers(service)
    night = Night(service, hold)
    return night.save(_resume_tick(night) if hold.get('phase') == 'resuming' else _stop_tick(night))
