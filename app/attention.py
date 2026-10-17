"""Things that need attention: gifts and people missing details.

Gifts come through GiftRepo, so nothing addressed to the viewer is ever listed or
counted. Ideas are allowed to be vague: an idea with no cost or occasion is fine
("someday, about $20"), but every gift needs a title.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Gift, Person
from app.visibility import GiftRepo


@dataclass
class Issue:
    item: Gift | Person
    missing: list[str]


def gift_issues(repo: GiftRepo) -> list[Issue]:
    issues = []
    for gift in repo.list():
        missing = [] if gift.title else ["title"]
        if gift.status != "idea":
            if gift.occasion_id is None:
                missing.append("occasion")
            if gift.cost_cents is None:
                missing.append("cost")
        if missing:
            issues.append(Issue(gift, missing))
    return issues


def person_issues(session: Session, viewer_id: int) -> list[Issue]:
    """Active people (other than the viewer) with no relation or no birthday."""
    issues = []
    for person in session.scalars(select(Person).where(Person.active).order_by(Person.name)):
        if person.id == viewer_id:
            continue
        missing = [] if person.relation_id else ["relation"]
        if person.birthday_month is None:
            missing.append("birthday")
        if missing:
            issues.append(Issue(person, missing))
    return issues


def count(session: Session, repo: GiftRepo) -> int:
    return len(gift_issues(repo)) + len(person_issues(session, repo.viewer_id))
