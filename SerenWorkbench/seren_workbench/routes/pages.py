"""
The pages at the door, for the boxes that pull them (seren_workbench.pages).

  GET /pages                    what is held: each page's seq, when, by, size
  GET /pages/{page}?after=N     the newest version if its seq is past N, else
                                204. A carabiner's bookmark hook asks this at
                                session start with the seq it last pulled.

Behind the bearer like every other route: a box that pulls already holds the
Workbench's token. There is no write route - a page is written through MCP,
by the model, and only a piton decides whether a box takes it.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request, Response

from ..pages import PAGES

router = APIRouter(tags=["pages"])


def _pages(request: Request):
    pages = getattr(request.app.state, "pages", None)
    if pages is None:
        raise HTTPException(503, "the page store is not up yet")
    return pages


@router.get("/pages")
async def pages_summary(request: Request):
    return _pages(request).store.summary()


@router.get("/pages/{page}")
async def page_after(request: Request, page: str, after: int = Query(0, ge=0)):
    if page not in PAGES:
        raise HTTPException(404, f"there is no page called {page!r}; the pages are {', '.join(PAGES)}")
    store = _pages(request).store
    v = store.after(page, after)
    if v is None:
        if store.problem:
            raise HTTPException(503, store.problem)
        return Response(status_code=204)
    return v
