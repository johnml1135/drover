"""Authenticated HTTP adapter; ledger services own durable state and authority."""
import asyncio
import json
import time
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from drover.ledger import ops, tx
from drover.notifications import Conflict, Notifications
from hub import auth
from hub.api import TaskIn

router = APIRouter(prefix='/api/v1/notifications')
Identity = Annotated[str, Field(min_length=1, max_length=256)]


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


class Role(Input):
    role_id: Identity
    policy: dict[str, Any] = Field(default_factory=dict)


class Session(Input):
    session_id: Identity
    ttl: float = Field(default=3600, gt=0, le=86400)
    capabilities: list[Literal['notifications.read', 'notifications.process', 'notifications.tune']] | None = None


class Follow(Input):
    selectors: dict[str, Any]
    preferences: dict[str, Any] = Field(default_factory=dict)


class Tune(Input):
    expected_revision: int = Field(ge=1, strict=True)
    preferences: dict[str, Any]


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
            return operation(service, principal)
    except Conflict as error:
        raise HTTPException(409, str(error))
    except ops.NotFound as error:
        raise HTTPException(404, str(error))
    except PermissionError:
        raise HTTPException(403, 'director scope refused')
    except ValueError as error:
        raise HTTPException(400, str(error))


def scoped(request, credential, capability, operation):
    return call(request, lambda s, p: operation(s, p['role_id'], p), credential, capability)


@router.post('/roles', status_code=201, dependencies=[Depends(auth.require_owner)])
def register(body: Role, request: Request):
    return call(request, lambda s, p: s.register(body.role_id, **body.policy))


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
    return scoped(request, credential, 'notifications.tune', lambda s, role, p: s.follow(role, body.selectors, **body.preferences))


@router.patch('/subscriptions/{subscription_id}')
def tune(subscription_id: str, body: Tune, request: Request, credential=Depends(auth.require_director)):
    def apply(s, role, p):
        s.subscription(subscription_id, role)
        return s.tune(subscription_id, body.expected_revision, **body.preferences)
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
    while True:
        def read(s, role, p):
            s.project(1000)
            return s.receive(role, limit)
        result = scoped(request, credential, 'notifications.read', read)
        if result or time.monotonic() >= deadline or await request.is_disconnected():
            return result
        # No transaction or connection is held across this await.
        await asyncio.sleep(min(.25, max(0, deadline - time.monotonic())))


@router.get('/replay')
def replay(request: Request, after: str | None = None, limit: int = Query(50, ge=1, le=100),
           credential=Depends(auth.require_director)):
    return scoped(request, credential, 'notifications.read', lambda s, role, p: s.replay(role, after, limit))


@router.get('/events', response_class=EventSourceResponse)
async def events(request: Request, after: str | None = None, limit: int = Query(50, ge=1, le=100),
                 credential=Depends(auth.require_director)):
    batch = scoped(request, credential, 'notifications.read', lambda s, role, p:
                   s.replay(role, after or request.headers.get('last-event-id'), limit))
    if batch['resync_required']:
        yield ServerSentEvent(event='resync', id=batch['cursor'], data=batch)
    else:
        for item in batch['items']:
            yield ServerSentEvent(event='notification', id=item['cursor'], data=item['message'])
        yield ServerSentEvent(event='checkpoint', id=batch['cursor'], data={'more': batch['more']})


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
