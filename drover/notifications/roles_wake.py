"""Wake reconciliation and director admission budgets."""
import json

from ..ledger import ops
from ..ledger.db import tx
from .core_scope import Conflict, _role, recipient_owner
from .core_evidence import coalesce_issue, issue_key
from .roles_base import _artifact, _runtime, canonical, hashed
from .roles_auth import admission

def signal(conn, role_id, event_id):
    """Called in the source transaction; an active consumer never clears arrivals."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='director_runtime'").fetchone():
        return
    _runtime(conn, role_id)
    conn.execute('UPDATE director_runtime SET wake_position=MAX(wake_position,?),pending_wake=1 WHERE role_id=?',
                 (event_id, role_id))

def refresh_wake(service, role_id):
    """Reconcile pending work while holding the same lock as checkpoint/release."""
    conn = service.conn
    runtime = _runtime(conn, role_id)
    # Reconcile the state projection will produce, including already processed
    # issues whose identical observations must not become new work on release.
    deliveries = {}
    for row in conn.execute('SELECT * FROM notification_inbox WHERE role_id=? AND resolved=0 '
                             'AND (actionable=1 OR acknowledged_at IS NULL)', (role_id,)):
        envelope = json.loads(row['envelope'])
        key = ('issue', row['issue_key']) if row['actionable'] else ('event', row['event_id'])
        deliveries[key] = envelope, row['acknowledged_at'] is None
    for row in conn.execute('SELECT i.* FROM notification_intents i LEFT JOIN notification_inbox n ON n.event_id=i.event_id '
                             'WHERE i.role_id=? AND i.resolved=0 AND i.projected=0 AND (i.actionable=1 OR n.id IS NULL) '
                             'ORDER BY i.event_id', (role_id,)):
        envelope = json.loads(row['envelope'])
        if not row['actionable'] and envelope['mode'] == 'muted':
            continue
        key = ('issue', issue_key(conn, envelope)) if row['actionable'] else ('event', row['event_id'])
        previous = deliveries.get(key)
        if previous is not None:
            if not row['actionable']:
                continue  # Replay cannot change an existing immutable delivery.
            updated = coalesce_issue(previous[0], envelope)
            if updated is None:
                continue
            envelope, changed = updated
            unread = previous[1] or changed
        else:
            unread = True
        deliveries[key] = envelope, unread
    for row in conn.execute('SELECT t.* FROM notification_targets t LEFT JOIN notification_inbox n '
                             'ON n.event_id=t.event_id AND n.role_id=t.role_id WHERE t.role_id=? AND t.projected=0 AND n.id IS NULL', (role_id,)):
        envelope = json.loads(row['envelope'])
        deliveries[('event', row['event_id'])] = envelope, True
    positions = []
    for envelope, unread in deliveries.values():
        owner = recipient_owner(conn, envelope)
        if unread and envelope['mode'] == 'immediate' and owner and owner['role_id'] == role_id:
            positions.append(envelope.get('latest_event_id', envelope['event_id']))
    for row in conn.execute('SELECT body FROM notification_digests WHERE role_id=? AND acknowledged_at IS NULL', (role_id,)):
        body = json.loads(row['body'])
        if body.get('wake') and any((owner := recipient_owner(conn, entry)) and
                                    owner['role_id'] == role_id for entry in body['entries']):
            positions.append(body['last_event'])
    continuation = bool(json.loads(runtime['checkpoint']).get('continuation', False))
    conn.execute('UPDATE director_runtime SET pending_wake=?,wake_position=MAX(wake_position,?) WHERE role_id=?',
                 (bool(positions) or continuation, max(positions, default=0), role_id))

def validate_policy(policy):
    from ..ledger.actions import KINDS
    if 'broadcast_summary' in policy and not isinstance(policy['broadcast_summary'], bool):
        raise ValueError('broadcast_summary must be a boolean')
    allowlist = policy.get('action_allowlist', [])
    if not isinstance(allowlist, list) or any(not isinstance(kind, str) or kind not in KINDS for kind in allowlist):
        raise ValueError('action_allowlist must contain only supported safe actions')
    models = policy.get('allowed_models', [])
    if not isinstance(models, list) or any(not isinstance(model, str) or not model or len(model) > 128 for model in models):
        raise ValueError('allowed_models must name explicit models')
    for key, maximum in (('recovery_attempt_limit', 2), ('replacement_limit', 1),
                         ('wake_limit_per_hour', 1000), ('token_budget', 10**12)):
        if key in policy and (isinstance(policy[key], bool) or not isinstance(policy[key], int) or not 0 <= policy[key] <= maximum):
            raise ValueError(f'{key} must be a nonnegative integer no greater than {maximum}')

def _hold_role(service, role_id, reason):
    runtime = _runtime(service.conn, role_id)
    question = runtime['fallback_question']
    if question is None:
        question = service.conn.execute('INSERT INTO questions(target_kind,target_id,title,body,asked) VALUES(?,?,?,?,?)',
                                         ('director', role_id, f'Director needs attention: {role_id}', reason, ops.stamp())).lastrowid
        ops.event(service.conn, 'director.held', 'system', dict(role_id=role_id, reason=reason, question=question))
    service.conn.execute('UPDATE director_runtime SET held_reason=?,fallback_question=? WHERE role_id=?',
                         (reason, question, role_id))
    return dict(role_id=role_id, accepted=False, held=True, reason=reason, question_id=question)

def _clear_role_hold(service, role_id):
    runtime = _runtime(service.conn, role_id)
    if runtime['fallback_question'] is not None:
        service.conn.execute("UPDATE questions SET status='answered',answer_text=?,answered_at=? WHERE id=? AND status='open'",
                             ('Director admission hold cleared.', ops.stamp(), runtime['fallback_question']))
        ops.event(service.conn, 'director.hold-cleared', 'system', dict(role_id=role_id, question=runtime['fallback_question']))
    service.conn.execute('UPDATE director_runtime SET held_reason=NULL,fallback_question=NULL WHERE role_id=?', (role_id,))

def reserve_wake(service, role_id, request_key, tokens=0, model=None):
    """Reserve an invocation's maximum token use, never a model call or launch.

    Reservations are conservative lifetime charges. Runtime adapters must cap a
    run at this reservation; restarting the hub never restores consumed budget.
    """
    if not isinstance(request_key, str) or not 0 < len(request_key) <= 256:
        raise ValueError('bounded wake request_key required')
    if isinstance(tokens, bool) or not isinstance(tokens, int) or not 0 <= tokens <= 10**12:
        raise ValueError('tokens must be a nonnegative bounded integer')
    if model is not None and (not isinstance(model, str) or not 0 < len(model) <= 128):
        raise ValueError('bounded model name required')
    conn = service.conn
    request_hash = hashed(canonical(dict(tokens=tokens, model=model)))
    with tx(conn):
        _artifact(conn, {'request_key': request_key, 'model': model})
        prior = conn.execute('SELECT * FROM director_budget_entries WHERE role_id=? AND request_key=?', (role_id, request_key)).fetchone()
        if prior:
            if prior['request_hash'] != request_hash:
                raise Conflict('wake request key reused with different content')
            return json.loads(prior['receipt'])
        policy = json.loads(_role(conn, role_id)['policy'])
        _runtime(conn, role_id)
        reason = None
        try:
            admission(service, role_id)
        except Conflict as error:
            reason = str(error)
        live = conn.execute('SELECT 1 FROM director_leases l JOIN director_roles r USING(role_id) '
                             'WHERE l.role_id=? AND l.generation=r.generation AND expires>?', (role_id, service.now())).fetchone()
        if reason is None and live:
            receipt = dict(role_id=role_id, accepted=False, coalesced=True, reason='director-already-active')
            conn.execute('INSERT INTO director_budget_entries VALUES(?,?,?,?,0,0,?,?)',
                         (role_id, request_key, request_hash, service.now(), model, canonical(receipt)))
            return receipt
        if reason is None and (model is not None or tokens) and model not in policy.get('allowed_models', []):
            reason = 'model-not-authorized'
        wakes = conn.execute('SELECT COALESCE(SUM(wakes),0) FROM director_budget_entries WHERE role_id=? AND created>?',
                              (role_id, service.now() - 3600)).fetchone()[0]
        total = conn.execute('SELECT COALESCE(SUM(tokens),0) FROM director_budget_entries WHERE role_id=?', (role_id,)).fetchone()[0]
        if reason is None and wakes >= policy.get('wake_limit_per_hour', 4):
            reason = 'wake-budget-exhausted'
        if reason is None and total + tokens > policy.get('token_budget', 0):
            reason = 'token-budget-exhausted'
        if reason:
            receipt = _hold_role(service, role_id, reason)
        else:
            _clear_role_hold(service, role_id)
            receipt = dict(role_id=role_id, accepted=True, tokens=tokens, model=model,
                           generation=_role(conn, role_id)['generation'],
                           wake_position=_runtime(conn, role_id)['wake_position'])
        conn.execute('INSERT INTO director_budget_entries VALUES(?,?,?,?,?,?,?,?)',
                     (role_id, request_key, request_hash, service.now(), int(reason is None),
                      tokens if reason is None else 0, model, canonical(receipt)))
        return receipt

def replace(service, role_id, expected_generation, *, confirmed_dead=False, reason='consumer-confirmed-gone'):
    """One automatic replacement admission; caller supplies verified supervision evidence."""
    if not isinstance(confirmed_dead, bool) or not isinstance(reason, str) or not 0 < len(reason) <= 1024:
        raise ValueError('confirmed_dead boolean and bounded reason required')
    conn = service.conn
    with tx(conn):
        role = _role(conn, role_id)
        if expected_generation != role['generation']:
            raise Conflict('director generation changed')
        runtime = _runtime(conn, role_id)
        try:
            admission(service, role_id)
        except Conflict as error:
            return _hold_role(service, role_id, str(error))
        if not confirmed_dead:
            return _hold_role(service, role_id, 'consumer-liveness-uncertain')
        if runtime['replacements_used'] >= min(1, json.loads(role['policy']).get('replacement_limit', 1)):
            return _hold_role(service, role_id, 'replacement-budget-exhausted')
        _artifact(conn, {'reason': reason})
        _clear_role_hold(service, role_id)
        generation = role['generation'] + 1
        conn.execute('UPDATE director_roles SET generation=? WHERE role_id=?', (generation, role_id))
        conn.execute('UPDATE director_runtime SET session_id=NULL,replacements_used=replacements_used+1,held_reason=NULL WHERE role_id=?', (role_id,))
        conn.execute('DELETE FROM director_leases WHERE role_id=?', (role_id,))
        conn.execute('DELETE FROM director_sessions WHERE role_id=?', (role_id,))
        ops.event(conn, 'director.replaced', 'system', dict(role_id=role_id, generation=generation, reason=reason))
        return dict(role_id=role_id, generation=generation, accepted=True,
                    checkpoint=json.loads(runtime['checkpoint']), checkpoint_revision=runtime['checkpoint_revision'])
