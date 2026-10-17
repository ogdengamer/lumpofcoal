"""The gallery: filtered card grid, saved quick filters, and the needs-attention list."""

from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import attention, welcome
from app.deps import db_session, gift_repo
from app.gallery import GalleryFilters, filter_options
from app.gifts import known_sources
from app.models import Gift, SavedFilter
from app.routes.gifts import add_form_context
from app.templating import templates
from app.visibility import GiftRepo

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def gallery(request: Request, saved: int | None = None, edited: bool = False,
            deleted: bool = False,
            session: Session = Depends(db_session), repo: GiftRepo = Depends(gift_repo)):
    # A new copy opens on the Welcome checklist, once per launch, until it's hidden.
    if (not request.query_params and not request.cookies.get(welcome.SEEN_COOKIE)
            and not welcome.dismissed(session)):
        return RedirectResponse("/welcome", status_code=303)
    filters = GalleryFilters.from_params(request.query_params)
    gifts = repo.list(*filters.clauses())
    saved_filters = list(session.scalars(select(SavedFilter).order_by(SavedFilter.position,
                                                                      SavedFilter.name)))
    context = add_form_context(session, repo)
    context.update(
        gifts=gifts,
        total_cents=sum(g.cost_cents or 0 for g in gifts),
        filters=filters,
        query=filters.query_string(),
        options=filter_options(session, repo.viewer_id),
        filter_sources=known_sources(session, repo),
        saved_filters=saved_filters,
        attention_count=attention.count(session, repo),
        on_the_way=repo.count(Gift.status == "ordered"),
        saved=repo.get(saved) if saved else None,
        edited=edited,
        deleted=deleted,
    )
    return templates.TemplateResponse(request, "gallery.html", context)


@router.post("/filters")
def save_filter(name: str = Form(...), query: str = Form(...),
                session: Session = Depends(db_session)):
    name = name.strip()[:60]
    # Re-canonicalise so the stored filter always parses the same way.
    query = GalleryFilters.from_params(dict(parse_qsl(query))).query_string()
    if not name or not query:
        raise HTTPException(422, "A saved filter needs a name and at least one filter")
    session.add(SavedFilter(name=name, query=query, position=100))
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(422, f"There's already a saved filter called {name!r}")
    return RedirectResponse(f"/?{query}", status_code=303)


@router.post("/filters/{filter_id}/delete")
def delete_filter(filter_id: int, session: Session = Depends(db_session)):
    saved = session.get(SavedFilter, filter_id)
    if saved is None:
        raise HTTPException(404)
    session.delete(saved)
    session.commit()
    return RedirectResponse("/", status_code=303)


@router.get("/attention", response_class=HTMLResponse)
def needs_attention(request: Request, saved: int | None = None, deleted: bool = False,
                    session: Session = Depends(db_session), repo: GiftRepo = Depends(gift_repo)):
    context = add_form_context(session, repo)
    context.update(
        gift_issues=attention.gift_issues(repo),
        person_issues=attention.person_issues(session, repo.viewer_id),
        saved=repo.get(saved) if saved else None, deleted=deleted,
    )
    return templates.TemplateResponse(request, "attention.html", context)
