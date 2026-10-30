"""Authenticated HTTP adapter; ledger services own durable state and authority."""
import asyncio
import json
import time
from collections import OrderedDict
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from fastapi.sse import format_sse_event
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from drover.ledger import ops, tx
from drover.notifications import Conflict, Notifications
from hub import auth
from hub.api import TaskIn

router = APIRouter(prefix='/api/v1/notifications')
Identity = Annotated[str, Field(min_length=1, max_length=256)]
RECEIVE_RECHECK_S = 30.0


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')

    @model_validator(mode='after')
    def bounded(self):
        try:
            size = len(json.dumps(self.model_dump(), allow_nan=False).encode())
        except ValueError:
            raise ValueError('finite JSON required')
        if size > 65536:
            raise ValueError('request exceeds 65536 bytes')
        return self


class Preferences(Input):
    preset: Literal['quiet', 'normal', 'verbose'] = 'normal'
    mode: Literal['profile', 'immediate', 'digest', 'muted'] = 'profile'
    class_modes: dict[str, Literal['immediate', 'digest', 'muted']] = Field(default_factory=dict)
    digest_interval: float = Field(default=900, ge=1, le=604800, strict=True)
    digest_wake: StrictBool = False
    detail_limit: int = Field(default=50, ge=1, le=100, strict=True)
    minimum_severity: Literal['info', 'warning', 'critical'] = 'info'
    enabled: StrictBool = True
    expires_at: float | None = Field(default=None, strict=True)
    stop_on_completion: StrictBool = False


class FollowPreferences(Preferences):
    replay_after: int | None = Field(default=None, ge=0, strict=True)


class TunePreferences(Preferences):
    selectors: dict[str, Any] | None = None


class Policy(Input):
    notifications: Preferences = Field(default_factory=Preferences)
    max_pending_routine: int = Field(default=1000, ge=1, le=100000, strict=True)
    max_pending_actionable: int = Field(default=10000, ge=1, le=100000, strict=True)
    broadcast_summary: StrictBool = False
    action_allowlist: list[Literal['task.retry', 'task.comment', 'issue.resolve']] = Field(default_factory=list)
    allowed_models: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(default_factory=list)
    recovery_attempt_limit: int = Field(default=2, ge=0, le=2, strict=True)
    replacement_limit: int = Field(default=1, ge=0, le=1, strict=True)
    wake_limit_per_hour: int = Field(default=4, ge=0, le=1000, strict=True)
    token_budget: int = Field(default=0, ge=0, le=10**12, strict=True)


class Role(Input):
    role_id: Identity
    policy: Policy = Field(default_factory=Policy)


class Session(Input):
    session_id: Identity
    ttl: float = Field(default=3600, gt=0, le=86400)
    capabilities: list[Literal['notifications.read', 'notifications.process', 'notifications.tune']] | None = None


class Follow(Input):
    selectors: dict[str, Any]
    preferences: FollowPreferences = Field(default_factory=FollowPreferences)


class Tune(Input):
    expected_revision: int = Field(ge=1, strict=True)
    preferences: TunePreferences


class Lease(Input):
    ttl: float = Field(default=60, gt=0, le=86400)


class Commit(Input):
    lease_token: Identity
    checkpoint: dict[str, Any]
    delivery_ids: list[Identity] = Field(default_factory=list, max_length=100)
    delivery_versions: dict[str, Annotated[int, Field(gt=0, strict=True)]] = Field(default_factory=dict)
    actions: list[dict[str, Any]] = Field(default_factory=list, max_length=32)
    request_key: Identity
    release: StrictBool = False

    @model_validator(mode='after')
    def versions(self):
        if set(self.delivery_ids) != set(self.delivery_versions):
            raise ValueError('echo message_version for every delivery_id')
        return self


class Transfer(Input):
    task_id: Identity
    role_id: Identity
    expected_revision: int | None = Field(default=None, ge=0, strict=True)
    event_class: str = '*'


class Group(Input):
    group_id: Identity
    parent_id: Identity | None = None
    mode: Literal['synthesis'] = 'synthesis'


class OwnerGroup(Group):
    role_id: Identity


class Revision(Input):
    expected_revision: int = Field(ge=1, strict=True)
    request_key: Identity


class Enroll(Revision):
    child_id: Identity


class Notice(Input):
    targets: Literal['all'] | list[Identity]
    body: str | dict[str, Any]
    urgency: Literal['summary', 'urgent'] = 'summary'
    request_key: Identity


class Control(Input):
    request_key: Identity


class Resume(Control):
    expected_epoch: int = Field(ge=0, strict=True)


class Skill(Control):
    skill: Identity
    digest: Identity
    targets: Literal['all'] | list[Identity]


class Apply(Input):
    lease_token: Identity
    digest: Identity | None = None


class Issue(Lease):
    lease_token: Identity
    max_attempts: int = Field(default=2, ge=1, le=2, strict=True)


class Hold(Input):
    lease_token: Identity
    reason: str = Field(min_length=1, max_length=2048)


class Submit(TaskIn, Input):
    title: str = Field(min_length=1, max_length=4096)
    parent_id: Identity | None = None
    group_id: Identity | None = None


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
        data = body.model_dump(exclude={'title', 'repo', 'description'})
        return ops.create_task(s.conn, body.title, body.repo, goal=body.description,
                               initiating_role=role, actor='director:' + role, **data)
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
    try:
        first = replay(cursor, 50)
    except Exception:
        bus.unsubscribe(queue)
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
            bus.unsubscribe(queue)

    return StreamingResponse(stream(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache'})


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
