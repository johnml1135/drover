"""Authenticated HTTP adapter; ledger services own durable state and authority."""
import asyncio
import json
import time
from collections import OrderedDict

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from fastapi.sse import format_sse_event

from drover.ledger import ops, tx
from drover.notifications import Conflict, Notifications
from hub import auth
from hub.notification_models import (Apply, Commit, Control, Enroll, Follow, Group, Hold, Issue, Lease, Notice, OwnerGroup,
                                     Policy, Resume, Revision, Role, Session, Skill, Submit, Transfer, Tune)

# Policy is re-exported: callers and tests import it from here.
router = APIRouter(prefix='/api/v1/notifications')
RECEIVE_RECHECK_S = 30.0


class _CleanupStreamingResponse(StreamingResponse):
    def __init__(self, content, *, cleanup, **kwargs):
        super().__init__(content, **kwargs)
        self._cleanup = cleanup

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._cleanup()


def call(request, operation, credential=None, capability=None):
    """Identity, capability and service mutation share one BEGIN IMMEDIATE."""
    service = Notifications(request.app.state.store.conn)
    before = service.conn.total_changes
    try:
        with tx(service.conn):
            principal = None
            if credential is not None:
                try:
                    principal = service.authenticate(credential)
                except PermissionError:
                    raise HTTPException(401, 'invalid or expired director credential')
                if capability and capability not in principal['capabilities']:
                    raise HTTPException(403, 'director capability required')
            result = operation(service, principal)
    except Conflict as error:
        raise HTTPException(409, str(error))
    except ops.NotFound as error:
        raise HTTPException(404, str(error))
    except PermissionError:
        raise HTTPException(403, 'director scope refused')
    except ValueError as error:
        raise HTTPException(400, str(error))
    if service.conn.total_changes != before:
        # Publish only after tx() committed. The receiver treats this as a hint
        # and reads the durable, role-scoped replay before offering anything.
        request.app.state.bus.publish('notification.receive-wake', {})
    return result


def scoped(request, credential, capability, operation):
    return call(request, lambda s, p: operation(s, p['role_id'], p), credential, capability)


@router.post('/roles', status_code=201, dependencies=[Depends(auth.require_owner)])
def register(body: Role, request: Request):
    return call(request, lambda s, p: s.register(body.role_id, **body.policy.model_dump(exclude_unset=True)))


@router.post('/roles/{role_id}/sessions', status_code=201, dependencies=[Depends(auth.require_owner)])
def session(role_id: str, body: Session, request: Request):
    return call(request, lambda s, p: s.issue_session(role_id, **body.model_dump()))


@router.post('/ownership', dependencies=[Depends(auth.require_owner)])
def transfer(body: Transfer, request: Request):
    return call(request, lambda s, p: s.assign(**body.model_dump()))


@router.get('/owner/health', dependencies=[Depends(auth.require_owner)])
def owner_health(request: Request):
    return call(request, lambda s, p: s.health())


@router.get('/health')
def health(request: Request, credential=Depends(auth.require_director)):
    return scoped(request, credential, 'notifications.read', lambda s, role, p: s.health(role))


@router.post('/follow')
def follow(body: Follow, request: Request, credential=Depends(auth.require_director)):
    return scoped(request, credential, 'notifications.tune', lambda s, role, p: s.follow(role, body.selectors, **body.preferences.model_dump(exclude_unset=True)))


@router.patch('/subscriptions/{subscription_id}')
def tune(subscription_id: str, body: Tune, request: Request, credential=Depends(auth.require_director)):
    def apply(s, role, p):
        s.subscription(subscription_id, role)
        return s.tune(subscription_id, body.expected_revision, **body.preferences.model_dump(exclude_unset=True))
    return scoped(request, credential, 'notifications.tune', apply)


@router.post('/tasks', status_code=201)
def submit(body: Submit, request: Request, credential=Depends(auth.require_director)):
    def apply(s, role, p):
        if body.parent_id:
            s.authorize_task(role, body.parent_id)
        if body.group_id:
            s.authorize_group(role, body.group_id)
        data = body.model_dump(exclude={'title', 'repo', 'description', 'requires_explicit_approval'})
        task = ops.create_task(s.conn, body.title, body.repo, goal=body.description,
                               initiating_role=role, actor='director:' + role, **data)
        request.app.state.store.set_explicit_approval(task['id'], body.requires_explicit_approval)
        return task
    return scoped(request, credential, 'notifications.process', apply)


@router.get('/inbox')
async def inbox(request: Request, limit: int = Query(50, ge=1, le=100), wait: float = Query(0, ge=0, le=30),
                credential=Depends(auth.require_director)):
    deadline = time.monotonic() + wait
    bus = request.app.state.bus
    queue = bus.subscribe()
    try:
        while True:
            def read(s, role, p):
                s.project(1000)
                return s.receive(role, limit)
            result = scoped(request, credential, 'notifications.read', read)
            if result or time.monotonic() >= deadline or await request.is_disconnected():
                return result
            # Every wake and timeout starts a fresh authenticated transaction.
            # The durable receive is never held open across this await.
            await bus.wait(queue, min(RECEIVE_RECHECK_S, max(0, deadline - time.monotonic())))
    finally:
        bus.unsubscribe(queue)


@router.get('/replay')
def replay(request: Request, after: str | None = None, limit: int = Query(50, ge=1, le=100),
           credential=Depends(auth.require_director)):
    return scoped(request, credential, 'notifications.read', lambda s, role, p: s.replay(role, after, limit))


@router.get('/events', response_class=StreamingResponse)
async def events(request: Request, after: str | None = None, limit: int | None = Query(None, ge=1, le=100),
                 credential=Depends(auth.require_director)):
    cursor = after if after is not None else request.headers.get('last-event-id')
    bus = request.app.state.bus

    def replay(saved_cursor, batch_limit):
        return scoped(request, credential, 'notifications.read', lambda s, role, p:
                      s.replay(role, saved_cursor, batch_limit))

    if limit is not None:
        batch = replay(cursor, limit)

        async def finite():
            if batch['resync_required']:
                yield format_sse_event(event='resync', id=batch['cursor'], data_str=json.dumps(batch))
            else:
                for item in batch['items']:
                    yield format_sse_event(event='notification', id=item['cursor'], data_str=json.dumps(item['message']))
                yield format_sse_event(event='checkpoint', id=batch['cursor'], data_str=json.dumps({'more': batch['more']}))

        return StreamingResponse(finite(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache'})

    # Subscribe before replay so a commit racing the initial read remains a
    # hint for the next durable pass. Empty receives stay open; reconnects use
    # the durable cursor and need no in-memory hint history.
    queue = bus.subscribe()
    released = False

    def unsubscribe():
        nonlocal released
        if not released:
            released = True
            bus.unsubscribe(queue)

    try:
        first = replay(cursor, 50)
    except Exception:
        unsubscribe()
        raise

    async def stream():
        current = first
        saved_cursor = cursor
        offered = OrderedDict()
        try:
            while True:
                batch = current
                current = None
                if batch['resync_required']:
                    offered.clear()
                    saved_cursor = batch['cursor']
                    yield format_sse_event(event='resync', id=batch['cursor'], data_str=json.dumps(batch))
                    return  # the client must apply the snapshot before receiving more events
                else:
                    sent = False
                    previous_cursor = saved_cursor
                    for item in batch['items']:
                        message = item['message']
                        key = (message['delivery_id'], message['message_version'], message.get('ownership_revision'))
                        if key in offered:
                            offered.move_to_end(key)
                            continue
                        offered[key] = None
                        while len(offered) > 1000:
                            offered.popitem(last=False)
                        yield format_sse_event(event='notification', id=item['cursor'], data_str=json.dumps(message))
                        sent = True
                    saved_cursor = batch['cursor']
                    # A missing client cursor means the stream starts at the
                    # durable origin. Do not report v1:0:0 as progress: an
                    # empty checkpoint would end the client's initial receive
                    # before it can wait for a notification.
                    advanced = previous_cursor is not None and saved_cursor != previous_cursor
                    if sent or advanced:
                        yield format_sse_event(event='checkpoint', id=saved_cursor,
                                               data_str=json.dumps({'more': batch['more']}))

                # Replay pages are drained while they add new offers. An
                # unacknowledged page repeats by design, so wait for a commit
                # hint instead of spinning when the page has already been sent.
                if batch['more'] and (sent or advanced):
                    current = replay(saved_cursor, 50)
                    continue
                await bus.wait(queue, RECEIVE_RECHECK_S)
                current = replay(saved_cursor, 50)
        except HTTPException as error:
            if error.status_code not in (401, 403):
                raise
            # Headers are already sent. Close the stream; the client's next
            # request receives the normal 401/403 response.
        finally:
            unsubscribe()

    return _CleanupStreamingResponse(stream(), cleanup=unsubscribe, media_type='text/event-stream',
                                     headers={'Cache-Control': 'no-cache'})


@router.post('/lease')
def acquire(body: Lease, request: Request, credential=Depends(auth.require_director)):
    return scoped(request, credential, 'notifications.process', lambda s, role, p: s.acquire(role, p['session_id'], body.ttl))


@router.post('/commit')
def commit(body: Commit, request: Request, credential=Depends(auth.require_director)):
    return scoped(request, credential, 'notifications.process', lambda s, role, p: s.commit(role, **body.model_dump()))


@router.get('/checkpoint')
def checkpoint(request: Request, revision: int | None = Query(None, ge=1), credential=Depends(auth.require_director)):
    return scoped(request, credential, 'notifications.read', lambda s, role, p: s.get_checkpoint(role, revision))


@router.post('/issues/{issue_id}/claim')
def claim(issue_id: str, body: Issue, request: Request, credential=Depends(auth.require_director)):
    return scoped(request, credential, 'notifications.process', lambda s, role, p: s.claim_issue(role, issue_id, **body.model_dump()))


@router.post('/issues/{issue_id}/hold')
def hold(issue_id: str, body: Hold, request: Request, credential=Depends(auth.require_director)):
    return scoped(request, credential, 'notifications.process', lambda s, role, p: s.hold_issue(role, issue_id, **body.model_dump()))


@router.post('/groups', status_code=201)
def group(body: Group, request: Request, credential=Depends(auth.require_director)):
    return scoped(request, credential, 'notifications.process', lambda s, role, p: s.create_group(role_id=role, **body.model_dump()))


@router.post('/owner/groups', status_code=201, dependencies=[Depends(auth.require_owner)])
def owner_group(body: OwnerGroup, request: Request):
    return call(request, lambda s, p: s.create_group(**body.model_dump()))


@router.get('/groups/{group_id}')
def get_group(group_id: str, request: Request, credential=Depends(auth.require_director)):
    return scoped(request, credential, 'notifications.read', lambda s, role, p: s.authorize_group(role, group_id))


@router.post('/groups/{group_id}/enroll')
def enroll(group_id: str, body: Enroll, request: Request, credential=Depends(auth.require_director)):
    def apply(s, role, p):
        s.authorize_group(role, group_id)
        s.authorize_task(role, body.child_id)
        return s.enroll(group_id, **body.model_dump())
    return scoped(request, credential, 'notifications.process', apply)


@router.post('/owner/groups/{group_id}/enroll', dependencies=[Depends(auth.require_owner)])
def owner_enroll(group_id: str, body: Enroll, request: Request):
    return call(request, lambda s, p: s.enroll(group_id, **body.model_dump()))


@router.post('/groups/{group_id}/seal')
def seal(group_id: str, body: Revision, request: Request, credential=Depends(auth.require_director)):
    def apply(s, role, p):
        s.authorize_group(role, group_id)
        return s.seal(group_id, **body.model_dump())
    return scoped(request, credential, 'notifications.process', apply)


@router.post('/broadcast')
def broadcast(body: Notice, request: Request, credential=Depends(auth.require_director)):
    return scoped(request, credential, 'notifications.process', lambda s, role, p:
                  s.broadcast({'kind': 'director', 'role_id': role}, **body.model_dump()))


@router.post('/owner/broadcast', dependencies=[Depends(auth.require_owner)])
def owner_broadcast(body: Notice, request: Request):
    return call(request, lambda s, p: s.broadcast('owner', **body.model_dump()))


@router.get('/controls')
def controls(request: Request, credential=Depends(auth.require_director)):
    return scoped(request, credential, 'notifications.read', lambda s, role, p: s.control_state(role))


@router.post('/controls/stop', dependencies=[Depends(auth.require_owner)])
def stop(body: Control, request: Request):
    return call(request, lambda s, p: s.stop('owner', body.request_key))


@router.post('/controls/resume', dependencies=[Depends(auth.require_owner)])
def resume(body: Resume, request: Request):
    return call(request, lambda s, p: s.resume('owner', **body.model_dump()))


@router.post('/controls/skill', dependencies=[Depends(auth.require_owner)])
def skill(body: Skill, request: Request):
    return call(request, lambda s, p: s.require_skill('owner', **body.model_dump()))


@router.post('/controls/{control_id}/apply')
def ack_control(control_id: str, body: Apply, request: Request, credential=Depends(auth.require_director)):
    return scoped(request, credential, 'notifications.process', lambda s, role, p: s.ack_control(role, control_id=control_id, **body.model_dump()))
