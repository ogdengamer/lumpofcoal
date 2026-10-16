"""The Welcome checklist for a new copy: a few setup steps that tick themselves off.

It opens on its own (once per app launch) until someone hides it; it's shared by
the household, so hiding it hides it for everyone. Settings links back to it.
"""

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import backup, logins
from app.models import AppSetting, BudgetRule, Person

DISMISSED = "welcome_dismissed"
SEEN_COOKIE = "loc_welcome_seen"  # this launch already showed it


@dataclass
class Step:
    title: str
    detail: str
    href: str | None
    done: bool


def dismissed(session: Session) -> bool:
    return session.get(AppSetting, DISMISSED) is not None


def dismiss(session: Session) -> None:
    if not dismissed(session):
        session.add(AppSetting(key=DISMISSED, value="1"))
    session.commit()


def steps(session: Session, data_dir: str) -> list[Step]:
    users = logins.app_users(session)
    others = list(session.scalars(select(Person).where(Person.active,
                                                       Person.login_email.is_(None))))
    status = backup.read_status(Path(data_dir))
    return [
        Step("Add your partner, and give them a login",
             "Add them on the People tab, then give them a login on App users. "
             "Each of you sees every gift except your own. Just you? Skip this one.",
             "/settings/users", len(users) >= 2),
        Step("Add the people you buy for",
             "With their relation (Child, Friend…) and birthday, for the budget page and "
             "upcoming birthdays. Groups (\"the grandkids\") are on the Groups tab.",
             "/settings/people", bool(others) and all(p.relation_id for p in others)),
        Step("Set a budget or two",
             "For example $100 per Child for Christmas. You can set budgets by relation, "
             "for one person, or for a group.",
             "/settings/budgets",
             bool(session.scalar(select(func.count()).select_from(BudgetRule)))),
        Step("Set up backups",
             "Turn on the backup service (see Backups in the README), then check it on "
             "Settings > Backups. This ticks itself off after the first good backup.",
             "/settings/backups", bool(backup.last_good(status))),
    ]
