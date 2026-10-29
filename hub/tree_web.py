"""Routes for the task tree and navigation jump."""
from urllib.parse import urlencode
import re
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from . import web
from .tree import tree_data

router = APIRouter()


@router.get("/tree", response_class=HTMLResponse)
def tree(request: Request, q: str = "", store=Depends(web.get_store)):
    return web._tab(request, "tree", store, epics=tree_data(store.c), q=q)


@router.get("/jump")
def jump(q: str = "", store=Depends(web.get_store)):
    q = q.strip()
    if re.fullmatch(r"(?i)T-[a-z0-9]+", q):
        row = store.c.execute("SELECT id FROM tasks WHERE id = ? COLLATE NOCASE LIMIT 1", (q,)).fetchone()
        if row:
            return RedirectResponse("/tasks/" + row["id"], status_code=303)
    return RedirectResponse("/tree?" + urlencode({"q": q}), status_code=303)
