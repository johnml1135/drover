"""Owner API for the Bypass: yank a task, then abandon it or hand it back."""
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from . import auth
from .api import guard, owner_only

router = APIRouter(prefix='/api/v1', dependencies=[Depends(auth.require_api)])


class YankIn(BaseModel):
    reason: str = ''


@router.post('/tasks/{tid}/yank')
def yank_task(tid: str, request: Request, body: YankIn | None = None, who: str = Depends(auth.require_api)):
    owner_only(who)
    return guard(request.app.state.store.yank_task, tid, body.reason if body else '')


@router.post('/tasks/{tid}/abandon')
def abandon_task(tid: str, request: Request, who: str = Depends(auth.require_api)):
    owner_only(who)
    return guard(request.app.state.store.abandon_task, tid)


@router.post('/tasks/{tid}/handback')
def handback_task(tid: str, request: Request, who: str = Depends(auth.require_api)):
    owner_only(who)
    return guard(request.app.state.store.handback_task, tid)
