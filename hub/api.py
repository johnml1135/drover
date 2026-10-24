"""JSON API under /api/v1 (bearer token, or an authorised UI request)."""
import re
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, StrictBool, field_validator

from drover import task_context

from hub import auth
from hub.store import BadRequest, Forbidden, NotFound

VERSION = "0.1.0"

TaskPriority = Literal["P0", "P1", "P2", "P3", "P4"]
TaskSize = Literal["S", "M", "L"]
TaskRisk = Literal["low", "high"]
NewTaskStatus = Literal["proposed", "draft"]
TaskStatus = Literal["proposed", "draft", "ready", "claimed", "running", "gated", "reviewed",
                     "landed", "blocked", "failed", "needs-owner", "cancelled"]

public = APIRouter(prefix="/api/v1")
router = APIRouter(prefix="/api/v1", dependencies=[Depends(auth.require_api)])


def _store(request: Request):
    return request.app.state.store


class Loose(BaseModel):
    model_config = ConfigDict(extra="allow")


TaskKind = Literal["research", "investigation", "implementation", "fix", "review", "docs", "mechanical"]


class Dependency(BaseModel):
    depends_on: str
    kind: Literal["blocks", "waits-for"] = "blocks"


class AccessInput(BaseModel):
    @field_validator("read_access", check_fields=False)
    @classmethod
    def valid_reads(cls, value):
        return task_context.read_access(value, "") if value is not None else value


class TaskIn(AccessInput):
    title: str
    repo: str = "default"
    status: NewTaskStatus = "proposed"  # ready needs owner approval
    epic_id: str | None = None
    repo_path: str | None = None
    description: str = Field(default="", validation_alias=AliasChoices("description", "goal"))
    kind: TaskKind = "implementation"
    write: StrictBool | None = None
    read_access: list[str] | None = None
    branch: str | None = None
    acceptance: list[str] = []
    scope_files: list[str] = []
    priority: TaskPriority = "P2"
    size: TaskSize = "M"
    risk: TaskRisk = "low"
    deps: list[str | Dependency] = []
    related: list[str] = []


class TaskPatch(AccessInput):
    title: str | None = None
    description: str | None = Field(default=None, validation_alias=AliasChoices("description", "goal"))
    kind: TaskKind | None = None
    write: StrictBool | None = None
    read_access: list[str] | None = None
    deps: list[str | Dependency] | None = None
    related: list[str] | None = None
    acceptance: list[str] | None = None
    scope_files: list[str] | None = None
    priority: TaskPriority | None = None
    size: TaskSize | None = None
    risk: TaskRisk | None = None
    repo: str | None = None
    epic_id: str | None = None
    handoff: str | None = None
    branch: str | None = None
    worktree: str | None = None
    model_pin: dict[str, str] | None = None


class StatusIn(BaseModel):
    status: TaskStatus
    reason: str | None = None


class ClaimIn(BaseModel):
    owner_kind: str | None = None
    model: str | None = None
    branch: str | None = None
    worktree: str | None = None
    repo: str | None = None
    repo_path: str | None = None
    policy_version: str | None = None


class EpicIn(BaseModel):
    repo: str
    title: str
    plan_md: str = ""


class RunStart(BaseModel):
    task_id: str
    role: str = "worker"
    backend: str
    model: str | None = None
    effort: str | None = None


class RunFinish(BaseModel):
    outcome: str
    tokens_in: int | None = None
    tokens_out: int | None = None
    tokens_cached: int | None = None
    allowance_used: Any = None
    gate_receipt: str | None = None
    review_verdict: str | None = None
    note: str | None = None
    stats: dict[str, Any] | None = None


class EventIn(BaseModel):
    kind: str
    data: dict[str, Any] = {}
    task_id: str | None = None
    epic_id: str | None = None


class CommentIn(BaseModel):
    target_kind: str = "task"
    target_id: str
    body: str
    author: str = "dispatcher"
    urgent: bool = False
    replan: bool = False


class QuestionIn(BaseModel):
    target_kind: str = "task"
    target_id: str
    title: str
    body: str = ""
    options: list[dict[str, Any]] = []
    allow_text: bool = True
    needs_owner: bool = False


class AnswerIn(BaseModel):
    choice: str | None = None
    text: str | None = None


class DecisionIn(BaseModel):
    question_name: str
    criteria_version: str
    model: str
    state_hash: str
    answer: Any
    probabilities: Any = None
    confidence: float | None = None
    shadow: bool = False
    rule_answer: Any = None


class OutcomeIn(BaseModel):
    outcome: Any


class ReorderIn(BaseModel):
    kind: str
    item_id: str
    before_id: str | None = None
    after_id: str | None = None


class ImportIn(BaseModel):
    tasks: list[dict[str, Any]]
    dry_run: bool = True


class HeartbeatIn(Loose):
    resources: dict[str, Any] = {}
    allowance: dict[str, Any] = {}
    workers: list[Any] = []


def is_owner(who: str) -> bool:
    """Only UI requests (Tailscale identity or loopback) act as the owner."""
    return who.startswith("ui:")


def actor(who: str) -> str:
    return "owner" if is_owner(who) else "dispatcher"


def owner_only(who: str):
    if not is_owner(who):
        raise HTTPException(403, "owner action: the API token cannot do this")


def guard(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except NotFound as e:
        raise HTTPException(404, str(e))
    except Forbidden as e:
        raise HTTPException(403, str(e))
    except BadRequest as e:
        raise HTTPException(400, str(e))


@public.get("/health")
def health():
    return {"ok": True, "version": VERSION}


@router.post("/heartbeat")
def post_heartbeat(body: HeartbeatIn, request: Request):
    return _store(request).put_heartbeat(body.model_dump())


@router.get("/heartbeat")
def get_heartbeat(request: Request):
    return _store(request).get_heartbeat() or {}


@router.post("/epics", status_code=201)
def create_epic(body: EpicIn, request: Request, who: str = Depends(auth.require_api)):
    return guard(_store(request).create_epic, body.repo, body.title, body.plan_md,
                 actor(who))


@router.get("/epics")
def list_epics(request: Request):
    return _store(request).list_epics()


@router.post("/tasks", status_code=201)
def create_task(body: TaskIn, request: Request, who: str = Depends(auth.require_api)):
    d = body.model_dump()
    return guard(_store(request).create_task, d.pop("title"), actor=actor(who), **d)


@router.get("/tasks")
def list_tasks(request: Request, status: str | None = None, epic_id: str | None = None,
               repo: str | None = None, q: str | None = None, limit: int = 500, include_archived: bool = False):
    return _store(request).list_tasks(status, epic_id, repo, q, limit, include_archived)


@router.post("/tasks/claim")
def claim(body: ClaimIn, request: Request):
    worker = {k: v for k, v in body.model_dump().items() if v is not None}
    worker["actor"] = "dispatcher"
    return {"task": guard(_store(request).claim_next, worker)}


@router.post("/tasks/reorder")
def reorder(body: ReorderIn, request: Request, who: str = Depends(auth.require_api)):
    owner_only(who)
    return guard(_store(request).reorder, body.kind, body.item_id, body.before_id,
                 body.after_id)


@router.get("/tasks/{tid}")
def get_task(tid: str, request: Request):
    s = _store(request)
    t = guard(s.get_task, tid)
    t["runs"] = s.list_runs(tid)
    return t


@router.patch("/tasks/{tid}")
def patch_task(tid: str, body: TaskPatch, request: Request,
               who: str = Depends(auth.require_api)):
    fields = body.model_dump()
    if 'model_pin' in body.model_fields_set and body.model_pin is None:
        fields['model_pin'] = {}  # preserve an explicit clear through the store's omitted-value filter
    return guard(_store(request).update_task, tid, actor(who), **fields)


@router.post("/tasks/{tid}/status")
def set_status(tid: str, body: StatusIn, request: Request,
               who: str = Depends(auth.require_api)):
    # the ledger refuses proposed/draft -> ready unless the actor is "owner"
    return guard(_store(request).set_status, tid, body.status, actor(who), body.reason)


@router.post("/tasks/{tid}/approve")
def approve(tid: str, request: Request, who: str = Depends(auth.require_api)):
    owner_only(who)
    return guard(_store(request).approve, tid)


@router.post("/runs", status_code=201)
def start_run(body: RunStart, request: Request, who: str = Depends(auth.require_api)):
    if body.role == "director" or body.role.startswith("director:"):
        raise HTTPException(403, "director lifecycle requires supervised runtime")
    return guard(_store(request).start_run, body.task_id, body.role, body.backend,
                 body.model, body.effort, actor(who))


@router.post("/runs/{rid}/finish")
def finish_run(rid: int, body: RunFinish, request: Request,
               who: str = Depends(auth.require_api)):
    role = guard(_store(request).run_role, rid)
    if role == "director" or role.startswith("director:"):
        raise HTTPException(403, "director lifecycle requires supervised runtime")
    d = body.model_dump(exclude={"outcome"})
    d = {k: v for k, v in d.items() if v is not None}
    return guard(_store(request).finish_run, rid, body.outcome, actor(who), **d)


OWNER_EVENT_KINDS = ("garden.", "dispatch.", "import-batch", "import-task", "tuner.", "director-borrow", "director.", "notification.", "group.", "waiting.", "task.result-accepted", "task.landed", "result.accepted")  # the executor trusts these


@router.post("/events", status_code=201)
def add_event(body: EventIn, request: Request, who: str = Depends(auth.require_api)):
    if not is_owner(who) and (body.kind == 'status' or body.kind.startswith(OWNER_EVENT_KINDS)):
        raise HTTPException(403, f"owner-reserved event kind: {body.kind}")
    return guard(_store(request).add_event, body.kind, actor(who), body.data, body.task_id,
                 body.epic_id)


@router.get("/events")
def list_events(request: Request, task_id: str | None = None, epic_id: str | None = None,
                kind: str | None = None, since_id: int = 0, limit: int = 200):
    return _store(request).list_events(task_id, epic_id, kind, since_id, limit)


@router.post("/comments", status_code=201)
def add_comment(body: CommentIn, request: Request, who: str = Depends(auth.require_api)):
    if body.target_kind in ("director", "group"):
        owner_only(who)
    # an owner comment requeues needs-owner tasks, so only the UI may use that name
    author = "owner" if is_owner(who) else (
        "dispatcher" if body.author == "owner" else body.author)
    return guard(_store(request).add_comment, body.target_kind, body.target_id, author,
                 body.body, body.urgent, body.replan)


@router.get("/comments")
def list_comments(target_kind: str, target_id: str, request: Request):
    return guard(_store(request).list_comments, target_kind, target_id)


@router.post("/questions", status_code=201)
def ask(body: QuestionIn, request: Request, who: str = Depends(auth.require_api)):
    if body.target_kind in ("director", "group"):
        owner_only(who)
    return guard(_store(request).ask_question, body.target_kind, body.target_id, body.title,
                 body.body, body.options, body.allow_text, body.needs_owner, actor(who))


@router.get("/questions")
def list_questions(request: Request, open: bool = False):
    return _store(request).list_questions(open)


@router.post("/questions/{qid}/answer")
def answer(qid: int, body: AnswerIn, request: Request, who: str = Depends(auth.require_api)):
    owner_only(who)
    return guard(_store(request).answer_question, qid, body.choice, body.text)


@router.post("/decisions", status_code=201)
def record_decision(body: DecisionIn, request: Request):
    return guard(_store(request).record_decision, **body.model_dump())


@router.post("/decisions/{did}/outcome")
def decision_outcome(did: int, body: OutcomeIn, request: Request):
    return guard(_store(request).decision_outcome, did, body.outcome)


@router.get("/decisions")
def list_decisions(request: Request):
    return _store(request).list_decisions()


@router.get("/board", response_class=PlainTextResponse)
def board(request: Request):
    return _store(request).board_text()


@router.get("/settings")
def get_settings(request: Request):
    return _store(request).get_settings()


# director_ controls admitted model execution; dispatch_ is the pause switch;
# gate_cmd_ names an owner-provided repo test script. Workers cannot set these.
OWNER_SETTING_PREFIXES = ("wsl_", "dispatch_", "director_", "garden_", "tuner_", "gate_cmd_", "gate_slots", "gate_reserved", "gate_infra_retries", "ui_", "watchdog_", "auth_", "token",
                          "hub_", "tailscale")
SAFE_NAME = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._-]*$")


@router.put("/settings")
def put_settings(body: dict[str, str], request: Request, who: str = Depends(auth.require_api)):
    guarded = [k for k in body if k.startswith(OWNER_SETTING_PREFIXES)]
    if guarded and not is_owner(who):
        raise HTTPException(403, f"owner-only settings: {', '.join(guarded)}")
    for k in ("wsl_distro", "wsl_user"):
        if k in body and not SAFE_NAME.match(body[k]):
            raise HTTPException(400, f"{k} must match [A-Za-z0-9._-]+ and not start with '-'")
    return guard(_store(request).update_settings, body, author=actor(who))


class DispatchIn(Loose):
    enabled: bool


@router.get("/dispatch")
def get_dispatch(request: Request):
    return {"enabled": _store(request).dispatch_enabled()}


@router.post("/dispatch")
def set_dispatch(body: DispatchIn, request: Request, who: str = Depends(auth.require_api)):
    owner_only(who)
    return {"enabled": _store(request).set_dispatch_enabled(body.enabled, actor="owner")}


@router.post("/epics/{eid}/approve-proposed")
def approve_epic(eid: str, request: Request, who: str = Depends(auth.require_api)):
    owner_only(who)
    return {"approved": guard(_store(request).approve_epic, eid)}


@router.post("/import")
def import_tasks(body: ImportIn, request: Request, who: str = Depends(auth.require_api)):
    owner_only(who)
    return guard(_store(request).import_tasks, body.tasks, dry_run=body.dry_run,
                 actor=actor(who))


class UsageIn(BaseModel):
    seat: str
    window: str
    pct: float
    resets: str


class ArchiveIn(BaseModel):
    archived: bool = True


class DeleteIn(BaseModel):
    confirm: bool = False


@router.get("/stats")
def get_stats(request: Request, since: str | None = None, task: str | None = None):
    return guard(_store(request).stats, since, task)


@router.get("/usage/order")
def get_seats(request: Request):
    return _store(request).seat_order()


@router.post("/usage")
def manual_usage(body: UsageIn, request: Request):
    return guard(_store(request).set_usage, body.seat, body.window, body.pct, body.resets)


@router.post("/tasks/{tid}/archive")
def archive_task(tid: str, body: ArchiveIn, request: Request, who: str = Depends(auth.require_api)):
    owner_only(who)
    return guard(_store(request).archive_task, tid, body.archived, actor(who))


@router.delete("/tasks/{tid}")
def delete_task(tid: str, body: DeleteIn, request: Request, who: str = Depends(auth.require_api)):
    owner_only(who)
    return guard(_store(request).delete_task, tid, body.confirm, actor(who))
