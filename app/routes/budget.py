"""The budget dashboard (see app/budget.py)."""

from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import budget
from app.deps import db_session, gift_repo
from app.models import Occasion, OccasionType
from app.routes.gifts import add_form_context
from app.templating import templates
from app.visibility import GiftRepo

router = APIRouter()


@router.get("/budget", response_class=HTMLResponse)
def dashboard(request: Request, occasion: int | None = None,
              session: Session = Depends(db_session), repo: GiftRepo = Depends(gift_repo)):
    today = date.today()
    chosen = session.get(Occasion, occasion) if occasion else None
    chosen = chosen or budget.next_occasion(session, today)
    occasions = list(session.scalars(select(Occasion).join(OccasionType)
                                     .order_by(Occasion.year, OccasionType.name)))
    context = add_form_context(session, repo)
    context.update(
        occasion=chosen,
        occasions=occasions,
        board=budget.build(session, repo, chosen, today) if chosen else None,
        upcoming=budget.upcoming_birthdays(session, repo, today),
        loose_gifts=budget.gifts_without_occasion(repo),
    )
    return templates.TemplateResponse(request, "budget.html", context)
