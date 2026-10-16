"""FastAPI dependencies shared by routes."""

from collections.abc import Iterator

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.auth import CurrentUser, current_user
from app.visibility import GiftRepo


def db_session(request: Request) -> Iterator[Session]:
    with request.app.state.sessionmaker() as session:
        yield session


def gift_repo(session: Session = Depends(db_session),
              user: CurrentUser = Depends(current_user)) -> GiftRepo:
    return GiftRepo(session, user.person_id)
