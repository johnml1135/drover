"""Owner API for recording an external landing."""
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from . import auth
from .api import guard, owner_only

router = APIRouter(prefix='/api/v1', dependencies=[Depends(auth.require_api)])


class ExternalLandingIn(BaseModel):
    sha: str
    base_ref: str
    gate: str
    review: str


@router.post('/tasks/{tid}/external-landing')
def record_external_landing(tid: str, body: ExternalLandingIn, request: Request,
                            who: str = Depends(auth.require_api)):
    owner_only(who)
    return guard(request.app.state.store.record_external_landing, tid, **body.model_dump())
