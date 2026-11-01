"""Owner API for who dispatches: the pause switch, the default and each epic's setting, and a director's pick."""
from typing import Literal

from fastapi import APIRouter, Depends, Request

from . import auth
from .api import Loose, _store, actor, guard, owner_only

router = APIRouter(prefix="/api/v1", dependencies=[Depends(auth.require_api)])


class DispatchIn(Loose):
    enabled: bool


class DispatchModeIn(Loose):
    mode: Literal["drover", "director"]


@router.get("/dispatch")
def get_dispatch(request: Request):
    return {"enabled": _store(request).dispatch_enabled()}


@router.post("/dispatch")
def set_dispatch(body: DispatchIn, request: Request, who: str = Depends(auth.require_api)):
    owner_only(who)
    return {"enabled": _store(request).set_dispatch_enabled(body.enabled, actor="owner")}


@router.get("/dispatch/default")
def get_dispatch_default(request: Request):
    return {"mode": _store(request).get_default_dispatch()}


@router.post("/dispatch/default")
def set_dispatch_default(body: DispatchModeIn, request: Request, who: str = Depends(auth.require_api)):
    owner_only(who)
    return {"mode": guard(_store(request).set_default_dispatch, body.mode)}


@router.post("/epics/{eid}/dispatch")
def set_epic_dispatch(eid: str, body: DispatchModeIn, request: Request, who: str = Depends(auth.require_api)):
    owner_only(who)
    return guard(_store(request).set_epic_dispatch, eid, body.mode)


@router.post("/tasks/{tid}/pick")
def pick_task(tid: str, request: Request, who: str = Depends(auth.require_api)):
    return guard(_store(request).pick, tid, actor(who))
