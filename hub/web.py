"""Owner website: server-rendered tabs, htmx partials and form posts. Data comes only from hub.store."""
from __future__ import annotations

import importlib
import time
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from .task_views import markdown
from .api import TaskPatch
from pydantic import ValidationError

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
templates = Jinja2Templates(directory=str(HERE / "templates"))
templates.env.filters["markdown"] = markdown
router = APIRouter()

TABS = [("inbox", "Inbox"), ("plan", "Plan"), ("runs", "Runs"), ("landing", "Landing"), ("health", "Health"), ("stats", "Stats")]
GROUPS = ("epic", "state", "repo", "model")
FILTERS = ("state", "repo", "model", "low")
KINDS = {"owner", "import", "tuner", "garden", "task", "epic", "question", "run"}


def _kind(kind):
    if kind not in KINDS:
        raise HTTPException(400, "unknown item kind")


def get_store():
    """Dependency: the ledger adapter module. Tests override this with a fake."""
    return importlib.import_module("hub.store")


def _call(store, name, *args, default=None, **kw):
    fn = getattr(store, name, None)
    return fn(*args, **kw) if fn else default


async def _form(request: Request) -> dict[str, list[str]]:
    return parse_qs((await request.body()).decode("utf-8", "replace"), keep_blank_values=True)


def _one(form, key, default=""):
    return (form.get(key) or [default])[0]


def _render(request, name, store, **ctx):
    ctx.setdefault("updated", _call(store, "last_updated", default=time.strftime("%H:%M:%S")))
    ctx.setdefault("unread", inbox_count(store))
    ctx.setdefault("tabs", TABS)
    ctx.setdefault("dispatch", _call(store, "dispatch_state", default={"enabled": False, "label": "PAUSED"}))
    ctx.setdefault("usage_alerts", _call(store, "usage_alerts", default=[]))
    ctx.setdefault("partial", False)
    return templates.TemplateResponse(request, name, ctx)


def _tab(request, tab, store, **ctx):
    partial = request.headers.get("hx-request") == "true" and request.headers.get("hx-boosted") != "true"
    ctx.update(tab=tab, partial=partial)
    return _render(request, f"{tab}.html", store, **ctx)


def inbox_data(store):
    return {
        "questions": _call(store, "inbox_questions", default=[]),
        "owner_items": _call(store, "needs_owner_items", default=[]),
        "imports": _call(store, "import_proposals", default=[]),
        "tuner": _call(store, "tuner_proposals", default=[]),
        "garden": _call(store, "garden_batches", default=[]),
        "failed": _call(store, "failed_blocked_items", default=[]),
    }


def inbox_count(store) -> int:
    try:
        d = inbox_data(store)
    except Exception:
        return 0
    return sum(len(v) for v in d.values()) + len(_call(store, "usage_alerts", default=[]))


@router.get("/", include_in_schema=False)
def index():
    return RedirectResponse("/inbox", status_code=303)


@router.get("/static/{path:path}", include_in_schema=False)
def static(path: str):
    target = (STATIC / path).resolve()
    if STATIC.resolve() not in target.parents or not target.is_file():
        raise HTTPException(404)
    return FileResponse(target)


def import_groups(imports):
    """Import proposals grouped by epic, in first-seen order."""
    groups: dict = {}
    for it in imports:
        g = groups.setdefault(str(it.get("epic_id") or ""), {"epic_id": str(it.get("epic_id") or ""),
                                                             "epic": it.get("epic") or "", "items": []})
        g["items"].append(it)
    return list(groups.values())


def inbox_ctx(store, low=False, review=""):
    d = inbox_data(store)
    all_imports = d["imports"]
    shown = [i for i in all_imports if i.get("low_confidence")] if low else all_imports
    d.update(imports=shown, import_groups=import_groups(shown), low=low,
             low_count=sum(1 for i in all_imports if i.get("low_confidence")), review=review)
    d["review_batch"] = None
    if review:
        d["review_batch"] = next((b for b in d["garden"] if str(b.get("id")) == review), None)
    return d


@router.get("/inbox", response_class=HTMLResponse)
def inbox(request: Request, low: str = "", review: str = "", store=Depends(get_store)):
    return _tab(request, "inbox", store, **inbox_ctx(store, low == "1", review))


def plan_view(store, group="epic", filters=None):
    filters = {k: v for k, v in (filters or {}).items() if v}
    epics = _call(store, "plan_epics", default=[])
    tasks = [dict(t, epic_id=e["id"], epic_title=e.get("title", "")) for e in epics for t in e.get("tasks", [])]
    chips = {f: sorted({str(t.get(f)) for t in tasks if t.get(f)}) for f in FILTERS}
    shown = [t for t in tasks if all(str(t.get(k)) == v for k, v in filters.items())]
    ctx = {"group": group, "filters": filters, "chips": chips, "groups": GROUPS, "epics": [], "buckets": []}
    if group == "epic":
        for e in epics:
            keep = [t for t in e.get("tasks", []) if all(str(t.get(k)) == v for k, v in filters.items())]
            if keep or not filters:
                ctx["epics"].append(dict(e, tasks=keep))
    else:
        key = {"state": "state", "repo": "repo", "model": "model"}[group]
        buckets: dict[str, list] = {}
        for t in shown:
            buckets.setdefault(str(t.get(key) or "none"), []).append(t)
        ctx["buckets"] = sorted(buckets.items())
    return ctx


@router.get("/plan", response_class=HTMLResponse)
def plan(request: Request, group: str = "epic", store=Depends(get_store)):
    group = group if group in GROUPS else "epic"
    filters = {f: request.query_params.get(f, "") for f in FILTERS}
    return _tab(request, "plan", store, **plan_view(store, group, filters))


@router.get("/tasks/{tid}", response_class=HTMLResponse)
def task_page(tid: str, request: Request, view: str = "page", store=Depends(get_store)):
    return _tab(request, "task", store, view=view if view in ("page", "card") else "page",
                **store.task_detail(tid))


@router.post("/tasks/{tid}/edit", response_class=HTMLResponse)
async def edit_task(tid: str, request: Request, store=Depends(get_store)):
    form = await _form(request)
    fields = {k: _one(form, k) for k in ("title", "description", "kind", "repo", "branch", "priority", "size", "risk")
              if k in form}
    if "write" in form:
        fields["write"] = _one(form, "write") == "true"
    for k in ("acceptance", "scope_files", "read_access", "related"):
        if k in form:
            fields[k] = [line.strip() for line in _one(form, k).splitlines() if line.strip()]
    if "deps" in form:
        fields["deps"] = [dict(depends_on=line.strip().partition(":")[0], kind=line.strip().partition(":")[2] or "blocks")
                          for line in _one(form, "deps").splitlines() if line.strip()]
    if "read_access" in fields and fields["read_access"] == store.task_detail(tid)["task"]["read_access"]:
        # Full forms resubmit implicit defaults, which can name an unconfigured repo.
        del fields["read_access"]
    try:
        fields = TaskPatch.model_validate(fields).model_dump(exclude_unset=True)
        store.edit_task(tid, **fields)
    except (ValidationError, ValueError) as e:
        raise HTTPException(422, str(e))
    return task_page(tid, request, store=store)


@router.get("/runs", response_class=HTMLResponse)
def runs(request: Request, store=Depends(get_store)):
    return _tab(request, "runs", store, runs=_call(store, "runs", default=[]),
                decider=_call(store, "decider_accuracy", default=[]))


@router.get("/runs/{rid}", response_class=HTMLResponse)
def run_detail(rid: str, request: Request, store=Depends(get_store)):
    return _tab(request, "runs", store, run=store.run_detail(rid), tail=store.run_tail(rid))


@router.get("/runs/{rid}/tail", response_class=HTMLResponse)
def run_tail(rid: str, request: Request, store=Depends(get_store)):
    return _render(request, "_runtail.html", store, run=store.run_detail(rid), tail=store.run_tail(rid))


@router.get("/landing", response_class=HTMLResponse)
def landing(request: Request, store=Depends(get_store)):
    data = _call(store, "landing", default={}) or {}
    return _tab(request, "landing", store, queue=data.get("queue", []), batch=data.get("batch"),
                gate=data.get("gate") or {}, repos=data.get("repos") or [])


@router.post("/repos/{name}/push-main")
def push_main(name: str, store=Depends(get_store)):
    """Owner-only through the UI auth and CSRF middleware; never exposed on the API."""
    store.push_main(name)
    return RedirectResponse("/landing", status_code=303)


@router.get("/health", response_class=HTMLResponse)
def health(request: Request, store=Depends(get_store)):
    return _tab(request, "health", store, h=_call(store, "health", default={}) or {})


def _inbox_response(request, store):
    return _tab(request, "inbox", store, **inbox_ctx(store))


@router.post("/questions/{qid}/answer", response_class=HTMLResponse)
async def answer(qid: str, request: Request, store=Depends(get_store)):
    form = await _form(request)
    option, text = _one(form, "option"), _one(form, "text").strip()
    if not option and not text:
        raise HTTPException(422, "choose an option or write an answer")
    store.answer_question(qid, option=option or None, text=text or None)
    return _inbox_response(request, store)


@router.get("/items/{kind}/{iid}/comments", response_class=HTMLResponse)
def comments(kind: str, iid: str, request: Request, store=Depends(get_store)):
    _kind(kind)
    return _render(request, "_comments.html", store, kind=kind, iid=iid,
                   thread=_call(store, "comments", kind, iid, default=[]))


@router.post("/items/{kind}/{iid}/comments", response_class=HTMLResponse)
async def add_comment(kind: str, iid: str, request: Request, store=Depends(get_store)):
    _kind(kind)
    form = await _form(request)
    body = _one(form, "body").strip()
    if not body:
        raise HTTPException(422, "comment is empty")
    store.add_comment(kind, iid, body, urgent=_one(form, "urgent") == "on", replan=_one(form, "replan") == "on")
    return _render(request, "_comments.html", store, kind=kind, iid=iid,
                   thread=_call(store, "comments", kind, iid, default=[]))


@router.post("/items/{kind}/{iid}/{decision}", response_class=HTMLResponse)
async def decide(kind: str, iid: str, decision: str, request: Request, store=Depends(get_store)):
    _kind(kind)
    if decision not in ("approve", "reject"):
        raise HTTPException(404)
    store.decide_item(kind, iid, decision)
    return _inbox_response(request, store)


@router.post("/imports/approve", response_class=HTMLResponse)
async def approve_imports(request: Request, store=Depends(get_store)):
    ids = (await _form(request)).get("id", [])
    if not ids:
        raise HTTPException(422, "select at least one item")
    store.approve_imports(ids)
    return _inbox_response(request, store)


@router.post("/imports/approve-epic", response_class=HTMLResponse)
async def approve_epic(request: Request, store=Depends(get_store)):
    epic_id = _one(await _form(request), "epic_id")
    if not epic_id:
        raise HTTPException(422, "epic required")
    store.approve_epic(epic_id)
    return _inbox_response(request, store)


@router.post("/garden/{batch}/approve-rows", response_class=HTMLResponse)
async def approve_garden_rows(batch: str, request: Request, store=Depends(get_store)):
    branches = (await _form(request)).get("branch", [])
    if not branches:
        raise HTTPException(422, "select at least one row")
    store.approve_garden(batch, branches)
    return _inbox_response(request, store)


@router.get("/dispatch/confirm", response_class=HTMLResponse)
def dispatch_confirm(request: Request, store=Depends(get_store)):
    return _render(request, "dispatch_confirm.html", store, tab="health", partial=False)


@router.post("/dispatch/toggle")
async def dispatch_toggle(request: Request, store=Depends(get_store)):
    """Owner-only (the UI port only serves the owner). Pausing is immediate; turning dispatch on needs confirm=yes."""
    form = await _form(request)
    enable = _one(form, "enable") == "1"
    if enable and _one(form, "confirm") != "yes":
        raise HTTPException(422, "confirm before turning dispatch on")
    store.set_dispatch(enable)
    back = _one(form, "next")
    return RedirectResponse(back if back in {"/" + k for k, _ in TABS} else "/health", status_code=303)


def _siblings(store, kind, iid):
    """Ids in the same list as the item, in current order."""
    epics = _call(store, "plan_epics", default=[])
    if kind == "epic":
        return None, [str(e["id"]) for e in epics]
    for e in epics:
        ids = [str(t["id"]) for t in e.get("tasks", [])]
        if iid in ids:
            return str(e["id"]), ids
    raise HTTPException(404, "unknown task")


@router.post("/plan/reorder", response_class=HTMLResponse)
async def reorder(request: Request, store=Depends(get_store)):
    form = await _form(request)
    kind, iid = _one(form, "kind"), _one(form, "id")
    before, after, move = _one(form, "before"), _one(form, "after"), _one(form, "move")
    if kind not in ("epic", "task") or not iid:
        raise HTTPException(422, "kind and id required")
    epic_id, ids = _siblings(store, kind, iid)
    for sib in (before, after):
        if sib and (sib not in ids or sib == iid):
            raise HTTPException(400, "before/after must be a sibling")
    if move:
        if iid not in ids:
            raise HTTPException(404)
        i = ids.index(iid)
        if move == "up" and i > 0:
            before = ids[i - 1]
        elif move == "down" and i < len(ids) - 1:
            after = ids[i + 1]
        else:
            return _tab(request, "plan", store, **plan_view(store))
    if not (before or after):
        raise HTTPException(422, "before or after required")
    store.reorder(kind, iid, before=before or None, after=after or None)
    return _tab(request, "plan", store, **plan_view(store))


@router.get("/stats", response_class=HTMLResponse)
def stats_tab(request: Request, store=Depends(get_store)):
    return _tab(request, "stats", store, seats=_call(store, "seat_order", default=[]),
                archived=_call(store, "archived_tasks", default=[]))


@router.post("/usage")
async def manual_usage(request: Request, store=Depends(get_store)):
    form = await _form(request)
    try:
        store.set_usage(_one(form, "seat"), _one(form, "window"), float(_one(form, "pct")), _one(form, "resets"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return RedirectResponse("/stats", status_code=303)


@router.post("/tasks/{tid}/archive")
async def archive_task(tid: str, request: Request, store=Depends(get_store)):
    form = await _form(request)
    store.archive_task(tid, _one(form, "archived", "1") == "1")
    return RedirectResponse("/plan", status_code=303)


@router.get("/tasks/{tid}/delete", response_class=HTMLResponse)
def delete_confirmation(tid: str, request: Request, store=Depends(get_store)):
    return _render(request, "delete_task.html", store, tab="plan", task=store.task(tid))


@router.post("/tasks/{tid}/delete")
async def delete_task(tid: str, request: Request, store=Depends(get_store)):
    form = await _form(request)
    store.delete_task(tid, confirm=_one(form, "confirm") == "1")
    return RedirectResponse("/plan", status_code=303)
