"""Director invocations supervised by the dispatcher, using the shared ledger/outbox.

Adapters are trusted execution integrations, not role-supplied commands. A compatible
adapter must enforce max_tokens, authenticate processing, and securely hand off the
credential (returned once, valid until expiry/revocation). Its probe must verify the
launch nonce; unknown never authorizes a replacement. No paid adapter is enabled by
default. Notification projection, digests and local action effects need no adapter.
"""
import json
import time
import uuid

from . import stats
from .launch import AdmissionHeld, RunnerLauncher
from .ledger import actions, ops
from .ledger.db import tx
from .notifications import Conflict, Notifications
from .notifications import roles


def service_tick(port, now=None):
    """Bounded deterministic work, independent of paid execution or dispatch pause."""
    service = Notifications(port.conn, now=now)
    projected = service.project(limit=100)
    digests = []
    for row in port.conn.execute("SELECT role_id FROM director_roles WHERE state<>'retired' ORDER BY role_id"):
        digest = service.digest(row['role_id'])
        if digest:
            digests.append(digest)
    effects = [actions.execute(port.conn, action['action_id'], now=now)
               for action in actions.pending(port.conn, limit=32)]
    return dict(projected=projected, digests=digests, actions=effects)


def stop_intent(port, spec, epoch, stop, kind='run.stop'):
    """Nonce-addressed stop requests are safe to retry; receipt means requested only."""
    conn = port.conn
    with tx(conn):
        row = conn.execute('SELECT action_id FROM action_outbox WHERE kind=? AND '
                           "json_extract(payload,'$.run_id')=? ORDER BY event_id DESC LIMIT 1",
                           (kind, spec['run_id'])).fetchone()
        if row:
            action = actions._action(conn, row['action_id'])
        else:
            payload = dict(run_id=spec['run_id'], nonce=spec.get('nonce'), stop_epoch=epoch)
            event = ops.event(conn, kind + '-requested', 'dispatcher', payload)
            action = actions.enqueue(conn, event, 0, kind, payload, supervised=True)
    if action['state'] == 'pending':
        try:
            requested = bool(spec.get('nonce') and stop(spec))
        except Exception:
            requested = False
        # Failed I/O may have issued the request. Keep pending and retry the same
        # nonce; never translate an exception or request receipt into cessation.
        if requested:
            action = actions.complete(conn, action['action_id'], dict(requested=True, termination='unknown'))
        with tx(conn):
            if not conn.execute("SELECT 1 FROM task_events WHERE kind='run.stop-held' AND "
                                "json_extract(data,'$.action_id')=?", (action['action_id'],)).fetchone():
                ops.event(conn, 'run.stop-held', 'dispatcher', dict(run=spec['run_id'],
                          action_id=action['action_id'], reason='termination-uncertain', requested=requested))
    return action


class RunnerDirectorAdapter:
    """Existing runner launch/adoption/nonce-stop machinery with an optional executor.

    An executor declares capabilities and implements launch(launcher, spec,
    credential, guard). It must enforce the token cap and use a private file/pipe
    or equivalent credential handoff, never argv, audit, or spec payloads. It calls
    guard at its last external boundary and preserves the spec's read-only sandbox.
    This is an integration seam, not a claim that existing CLI token flags do so.
    """
    def __init__(self, launcher=None, executor=None):
        self.launcher = launcher or RunnerLauncher()
        self.executor = executor
        self.capabilities = dict(live_offer=False, verified_resume=False, fresh_start=False,
                                 enforced_token_cap=False, authenticated_processing=False, secure_handoff=False)
        if executor is not None:
            self.capabilities.update(executor.capabilities)

    def launch(self, spec, credential, guard):
        if self.executor is None:
            raise AdmissionHeld('unsupported:enforced-token-cap,authenticated-processing,secure-handoff')
        return self.executor.launch(self.launcher, dict(spec, _admission_check=guard), credential, guard)

    def verify_resume(self, spec):
        verify = getattr(self.executor, 'verify_resume', None)
        return bool(verify and verify(self.launcher, spec))

    def probe(self, spec):
        from . import runner
        try:
            status = runner.read_status(self.launcher.runs_dir, spec['run_id'])
            if not status or status.get('nonce') != spec['nonce']:
                return 'unknown'
            if status.get('state') in ('exited', 'killed', 'timeout'):
                return 'gone'
            found = self.launcher.scan(spec['placement'] == 'wsl')
            if spec['run_id'] in found:
                return 'alive'
            # A missing supervisor does not prove its model child has ceased.
            # Require terminal nonce-matched evidence, not a clean empty scan.
            return 'unknown'
        except Exception:
            return 'unknown'

    def adopt(self, spec):
        return self.launcher.adopt(spec)

    def stop(self, spec):
        return self.launcher.cancel(spec['run_id'], spec['nonce'])


class DirectorRuntime:
    def __init__(self, port, adapter=None, now=None):
        self.port, self.conn = port, port.conn
        self.now = now or time.time
        self.service = Notifications(self.conn, now=self.now)
        self.adapter = adapter or RunnerDirectorAdapter()
        self.live = {}

    def active_runs(self):
        return [dict(row, spec=json.loads(row['spec'])) for row in self.conn.execute(
            "SELECT * FROM director_runs WHERE state<>'finished' ORDER BY run_id")]

    def _hold(self, role_id, reason, run_id=None):
        with tx(self.conn):
            held = roles._hold_role(self.service, role_id, reason)
            if run_id is not None:
                self.conn.execute('UPDATE director_runs SET held_reason=? WHERE run_id=?', (reason, run_id))
            return held

    def _guard(self, spec):
        control = self.service.admission(role_id=spec['role_id'], generation=spec['generation'], epoch=spec['control_epoch'])
        if not control['allowed']:
            raise AdmissionHeld(control['reason'])
        try:
            roles.admission(self.service, spec['role_id'])
        except Conflict as error:
            raise AdmissionHeld(str(error)) from error
        row = self.conn.execute('SELECT * FROM director_sessions WHERE role_id=? AND session_id=? '
                                'AND generation=? AND expires>?', (spec['role_id'], spec['session_id'],
                                spec['generation'], self.now())).fetchone()
        if not row:
            raise AdmissionHeld('director-session-expired')

    def _config(self, role_id):
        configured = json.loads(self.port.get_setting('director_execution', '{}')).get(role_id)
        if not isinstance(configured, dict):
            raise ValueError('execution-not-configured')
        required = {'backend', 'model', 'seat', 'max_tokens', 'cwd', 'placement'}
        if set(configured) != required or any(not isinstance(configured[k], str) or not configured[k]
                                             for k in required - {'max_tokens'}):
            raise ValueError('invalid-director-execution-config')
        if configured['backend'] not in ('claude', 'codex', 'local') or configured['placement'] not in ('windows', 'wsl'):
            raise ValueError('invalid-director-execution-config')
        tokens = configured['max_tokens']
        if isinstance(tokens, bool) or not isinstance(tokens, int) or not 0 < tokens <= 10**12:
            raise ValueError('invalid-director-token-cap')
        return dict(configured)

    def _anchor(self):
        row = self.conn.execute('SELECT id FROM tasks WHERE internal=1 LIMIT 1').fetchone()
        if row:
            return row['id']
        task_id, stamp = ops._next_id(self.conn, 'T'), ops.stamp()
        self.conn.execute('INSERT INTO tasks(id,repo,title,rank,status,created,updated,archived,internal,"write") '
                          'VALUES(?,?,?,?,?,?,?,1,1,0)',
                          (task_id, '__director_accounting__', 'Director run accounting', 0, 'draft', stamp, stamp))
        ops.event(self.conn, 'director.accounting-created', 'dispatcher', dict(task_id=task_id))
        return task_id

    def _start(self, role_id, config, epoch, global_epoch, mode='fresh', previous=None):
        with tx(self.conn):
            # Recheck inside the same transaction as budget, identity, and intent.
            roles.admission(self.service, role_id)
            global_control = self.service.admission(epoch=global_epoch)
            if not global_control['allowed']:
                raise AdmissionHeld(global_control['reason'])
            control = self.service.admission(role_id=role_id, epoch=epoch)
            if not control['allowed']:
                raise AdmissionHeld(control['reason'])
            if self.conn.execute("SELECT 1 FROM director_runs WHERE role_id=? AND state<>'finished'", (role_id,)).fetchone():
                return None
            state = self.service.role_state(role_id)
            nonce = uuid.uuid4().hex
            reserved = self.service.reserve_wake(role_id, 'launch:' + nonce, tokens=config['max_tokens'], model=config['model'])
            if not reserved['accepted']:
                return reserved
            session = self.service.issue_session(role_id, 'director-run:' + nonce)
            task_id = self._anchor()
            run_id = self.conn.execute('INSERT INTO runs(task_id,role,backend,model,started) VALUES(?,?,?,?,?)',
                                      (task_id, 'director', config['backend'], config['model'], ops.stamp())).lastrowid
            spec = dict(config, role='director', role_id=role_id, run_id=run_id, nonce=nonce,
                        session_id=session['session_id'], generation=session['generation'], control_epoch=epoch,
                        checkpoint=state['checkpoint'], checkpoint_revision=state['checkpoint_revision'],
                        write=False, sandbox='read-only', read_dirs=[], system_append='',
                        mode=mode, effort=None, wall_s=600, idle_s=120,
                        user_prompt='Process the authenticated director inbox and save a checkpoint before exiting.',
                        task={'id': task_id, 'title': 'Director ' + role_id})
            if mode == 'resume':
                spec['resume_from'] = dict(run_id=previous['run_id'], nonce=previous['nonce'], session_id=previous['session_id'])
            source = ops.event(self.conn, 'director.launch-intent', 'dispatcher', dict(role_id=role_id, run_id=run_id,
                               nonce=nonce, generation=session['generation'], control_epoch=epoch))
            action = actions.enqueue(self.conn, source, 0, 'director.launch', dict(spec), supervised=True)
            self.conn.execute('INSERT INTO director_runs(run_id,role_id,generation,checkpoint_revision,control_epoch,'
                              'nonce,session_id,spec,launch_action_id) VALUES(?,?,?,?,?,?,?,?,?)',
                              (run_id, role_id, session['generation'], state['checkpoint_revision'], epoch,
                               nonce, session['session_id'], roles.canonical(spec), action['action_id']))
            stats.initial_run(self.conn, run_id)
            stats.update_run(self.conn, run_id, seat=config['seat'], placement=config['placement'])
        guard = lambda: self._guard(spec)
        try:
            guard()
            handle = self.adapter.launch(dict(spec), session['credential'], guard)
        except AdmissionHeld as error:
            # Adapter contract: this exception is only legal before issuing I/O.
            self.finish(run_id, dict(status='admission-held', usage={}))
            actions.complete(self.conn, action['action_id'], dict(issued=False, reason=str(error)))
            return self._hold(role_id, str(error))
        except Exception:
            # No exception text: backends can accidentally include credentials.
            return self._hold(role_id, 'launch-effect-unknown', run_id)
        self.live[run_id] = handle
        with tx(self.conn):
            self.conn.execute("UPDATE director_runs SET state='running',held_reason=NULL WHERE run_id=?", (run_id,))
            actions.complete(self.conn, action['action_id'], dict(run_id=run_id, nonce=nonce, issued=True))
        # STOP can race an already-issued launch. Preserve its receipt and stop it.
        if not self.service.admission(role_id=role_id, generation=spec['generation'], epoch=epoch)['allowed']:
            self.stop_active(self.service.admission()['epoch'])
        return dict(run_id=run_id, launched=True)

    def recover(self):
        out = dict(adopted=[], finished=[], held=[])
        for run in self.active_runs():
            spec, run_id = run['spec'], run['run_id']
            handle = self.live.get(run_id)
            if handle is not None:
                result = handle.poll()
                if result is not None:
                    self.finish(run_id, result)
                    out['finished'].append(run_id)
                    continue
            try:
                state = self.adapter.probe(spec)
            except Exception:
                state = 'unknown'
            if state not in ('alive', 'gone'):
                out['held'].append(self._hold(run['role_id'], 'termination-uncertain' if run['stop_action_id'] else
                                             'consumer-liveness-uncertain', run_id))
                continue
            if state == 'gone':
                # Recover a terminal receipt when available; no receipt means a
                # failed director, never permission to accept child completion.
                if handle is None:
                    try:
                        handle = self.adapter.adopt(spec)
                    except Exception:
                        handle = None
                result = handle.poll() if handle is not None else None
                self.finish(run_id, result or dict(status='stopped' if run['stop_action_id'] else 'director-gone', usage={}))
                if actions._action(self.conn, run['launch_action_id'])['state'] == 'pending':
                    actions.complete(self.conn, run['launch_action_id'], dict(run_id=run_id, reconciled='gone'))
                out['finished'].append(run_id)
            else:
                if handle is None:
                    try:
                        self.live[run_id] = self.adapter.adopt(spec)
                    except Exception:
                        out['held'].append(self._hold(run['role_id'], 'adoption-uncertain', run_id))
                        continue
                    out['adopted'].append(run_id)
                try:
                    self._guard(spec)
                except AdmissionHeld:
                    out['held'].append(self._stop_one(run, self.service.admission(role_id=run['role_id'])['epoch']))
                    continue
                with tx(self.conn):
                    self.conn.execute("UPDATE director_runs SET state='running',held_reason=NULL WHERE run_id=?", (run_id,))
                    roles._clear_role_hold(self.service, run['role_id'])
                    if actions._action(self.conn, run['launch_action_id'])['state'] == 'pending':
                        actions.complete(self.conn, run['launch_action_id'], dict(run_id=run_id, nonce=run['nonce'], issued=True))
        return out

    def finish(self, run_id, result):
        """Accounting only: checkpoint/acknowledgement is the authenticated API's job."""
        with tx(self.conn):
            run = self.conn.execute('SELECT d.*,r.ended,r.started FROM director_runs d JOIN runs r ON r.id=d.run_id '
                                    'WHERE d.run_id=?', (run_id,)).fetchone()
            if run is None:
                raise ops.NotFound(f'director run {run_id}')
            if run['ended'] is not None:
                return dict(run_id=run_id, finished=True)
            outcome = result.get('status', 'error')
            if not isinstance(outcome, str) or not 0 < len(outcome) <= 100:
                raise ValueError('bounded director result status required')
            usage = result.get('usage') or {}
            values = [usage.get(name) for name in ('tokens_in', 'tokens_out', 'tokens_cached')]
            if any(v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 0) for v in values):
                raise ValueError('director usage must be nonnegative integers')
            ended = ops.stamp()
            wall = (ops._parse(ended) - ops._parse(run['started'])).total_seconds()
            self.conn.execute('UPDATE runs SET ended=?,outcome=?,wall_s=?,tokens_in=?,tokens_out=?,tokens_cached=? WHERE id=?',
                              (ended, outcome, wall, *values, run_id))
            self.conn.execute("UPDATE director_runs SET state='finished' WHERE run_id=?", (run_id,))
            stats.finish_run(self.conn, run_id)
            self.conn.execute('DELETE FROM director_leases WHERE role_id=? AND generation=? AND session_id=?',
                              (run['role_id'], run['generation'], run['session_id']))
            self.conn.execute('DELETE FROM director_sessions WHERE role_id=? AND generation=? AND session_id=?',
                              (run['role_id'], run['generation'], run['session_id']))
            roles.refresh_wake(self.service, run['role_id'])
            ops.event(self.conn, 'director.run-finished', 'dispatcher', dict(run_id=run_id,
                      role_id=run['role_id'], generation=run['generation'], outcome=outcome))
        self.live.pop(run_id, None)
        return dict(run_id=run_id, finished=True)

    def stop_active(self, epoch):
        out = dict(requested=[], held=[])
        for run in self.active_runs():
            out['requested'].append(run['run_id'])
            out['held'].append(self._stop_one(run, epoch))
        return out

    def _stop_one(self, run, epoch):
        # A prior issued stop remains authoritative across resume/restart. Do not
        # clear its uncertain termination just because control was later lifted.
        if run['stop_action_id']:
            epoch = actions._action(self.conn, run['stop_action_id'])['payload']['stop_epoch']
        action = stop_intent(self.port, run['spec'], epoch, self.adapter.stop, 'director.stop')
        with tx(self.conn):
            self.conn.execute('UPDATE director_runs SET stop_action_id=? WHERE run_id=?', (action['action_id'], run['run_id']))
        return self._hold(run['role_id'], 'termination-uncertain', run['run_id'])

    def tick(self, admission, capacity):
        out = dict(service=service_tick(self.port, self.now), recovery=self.recover(), launched=[], held=[])
        current = self.service.admission(epoch=admission['epoch'])
        if not admission['allowed']:
            current.update(allowed=False, reason=admission['reason'])
        if not current['allowed']:
            out['stopped'] = self.stop_active(current['epoch'])
        active = {run['role_id'] for run in self.active_runs()}
        slots = capacity.get('slots', 0)
        slots = slots() if callable(slots) else slots
        enabled = str(self.port.get_setting('director_execution_enabled', 'false')).lower() in ('true', '1', 'yes', 'on')
        capabilities = getattr(self.adapter, 'capabilities', {})
        for row in self.conn.execute("SELECT role_id FROM director_roles WHERE state<>'retired' ORDER BY role_id").fetchall():
            role_id = row['role_id']
            state = self.service.role_state(role_id)
            role_epoch = self.service.admission(role_id=role_id)['epoch']
            if role_id in active or not state['pending_wake']:
                continue
            reason = current['reason'] if not current['allowed'] else None
            if reason is None and not enabled:
                reason = 'execution-disabled'
            required = ('enforced_token_cap', 'authenticated_processing', 'secure_handoff')
            missing = [key.replace('_', '-') for key in required if capabilities.get(key) is not True]
            if reason is None and missing:
                reason = 'unsupported:' + ','.join(missing)
            if reason is None and slots <= 0:
                reason = 'capacity-unavailable'
            try:
                config = self._config(role_id) if reason is None else None
                if reason is None:
                    # This callback shares the dispatcher's seat, allowance,
                    # platform and resource checks, including open durable runs.
                    reason = capacity['admit'](config)
                if reason is None:
                    previous = self.conn.execute('SELECT d.*,r.outcome FROM director_runs d JOIN runs r ON r.id=d.run_id '
                                                 'WHERE role_id=? ORDER BY run_id DESC LIMIT 1', (role_id,)).fetchone()
                    mode = 'fresh'
                    if previous and capabilities.get('verified_resume') is True:
                        verify = getattr(self.adapter, 'verify_resume', None)
                        try:
                            if verify and verify(json.loads(previous['spec'])) is True:
                                mode = 'resume'
                        except Exception:
                            reason = 'resume-verification-uncertain'
                    if mode == 'fresh' and capabilities.get('fresh_start') is not True:
                        reason = 'unsupported:fresh-start,verified-resume-unavailable'
                    failed = previous and previous['outcome'] not in ('done', 'stopped', 'admission-held')
                    if failed and previous['generation'] == state['generation']:
                        if reason is None:
                            replaced = self.service.replace(role_id, state['generation'], confirmed_dead=True)
                            if not replaced['accepted']:
                                reason = replaced['reason']
                    if reason is None:
                        started = self._start(role_id, config, role_epoch, current['epoch'], mode, previous)
                        if started and started.get('launched'):
                            out['launched'].append(started['run_id'])
                            slots -= 1
                        elif started:
                            out['held'].append(started)
            except (ValueError, PermissionError, Conflict, AdmissionHeld) as error:
                reason = str(error)
            if reason:
                out['held'].append(self._hold(role_id, reason))
        return out
