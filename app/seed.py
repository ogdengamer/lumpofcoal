"""First-run defaults: relations and occasion types, nothing else.

The seed runs only on an empty database, so later edits in the app are never
overwritten. People, groups and budgets are added in the app (see /welcome);
logins in Settings > App users or logins.bootstrap_users().
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OccasionType, Relation

# Closest first: the add-gift chips pad with the closest relations (gifts.py).
RELATIONS = ["Partner", "Child", "Grandchild", "Parent", "Sibling", "Sibling-in-law",
             "Niece/Nephew", "Friend"]

# name: (date_rule, month, day)
OCCASION_TYPES = {
    "Christmas": ("fixed", 12, 25),
    "Birthday": ("birthday", None, None),
    "Easter": ("easter", None, None),
    "Just Because": ("none", None, None),
}


def seed(session: Session) -> bool:
    """Populate an empty database. Returns True if it seeded."""
    if session.scalar(select(Relation.id).limit(1)) is not None:
        return False
    session.add_all(Relation(name=name) for name in RELATIONS)
    session.add_all(OccasionType(name=name, date_rule=rule, month=m, day=d)
                    for name, (rule, m, d) in OCCASION_TYPES.items())
    session.commit()
    return True
