"""The Welcome checklist (see app/welcome.py)."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app import welcome
from app.deps import db_session
from app.templating import templates

router = APIRouter()


@router.get("/welcome", response_class=HTMLResponse)
def welcome_page(request: Request, session: Session = Depends(db_session)):
    steps = welcome.steps(session, request.app.state.settings.data_dir)
    resp = templates.TemplateResponse(request, "welcome.html", {
        "steps": steps, "done_count": sum(s.done for s in steps),
        "dismissed": welcome.dismissed(session)})
    resp.set_cookie(welcome.SEEN_COOKIE, "1", httponly=True, samesite="lax")  # this launch only
    return resp


@router.post("/welcome/dismiss")
def dismiss(session: Session = Depends(db_session)):
    welcome.dismiss(session)
    return RedirectResponse("/", status_code=303)
