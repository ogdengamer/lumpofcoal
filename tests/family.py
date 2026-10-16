"""A made-up household for tests, on top of the app's first-run defaults.

The tests' app (conftest) seeds this instead of the bare defaults, so every test
starts with people, groups, budgets and this year's Christmas and Birthday.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AppSetting, BudgetRule, Group, Occasion, OccasionType, Person, Relation
from app.seed import seed

# Abel and Jules are each other's Partner; each sees themselves as "Self".
PEOPLE = {
    "Abel": "Partner", "Jules": "Partner",
    "Emma": "Child", "Elsa": "Child", "Shay": "Child", "Astrid": "Child",
    "Cal": "Grandchild", "Evan": "Grandchild", "Gina": "Grandchild",
    "Nell": "Parent", "Pop": "Parent",
    "Sven": "Sibling", "Kira": "Sibling-in-law",
    "Hazel": "Niece/Nephew", "Sage": "Niece/Nephew",
    "Nils": "Friend", "Stacy": "Friend",
}

GROUPS = {
    "Nils & Stacy": ["Nils", "Stacy"],
    "Grandkids": ["Evan", "Cal", "Gina"],
    "Girls": ["Cal", "Gina"],
}

OCCASIONS = [("Christmas", 2026), ("Birthday", 2026)]

# (target kind, target name, occasion type, dollars, basis)
RULES = [
    ("relation", "Child", "Christmas", 200, "per_member"),
    ("relation", "Child", "Birthday", 100, "per_member"),
    ("relation", "Grandchild", "Birthday", 50, "per_member"),
    ("group", "Nils & Stacy", "Christmas", 30, "group_total"),
    ("person", "Nils", "Christmas", 15, "per_member"),
]


def seed_family(session: Session) -> bool:
    if not seed(session):
        return False
    relations = {r.name: r for r in session.scalars(select(Relation))}
    types = {t.name: t for t in session.scalars(select(OccasionType))}
    people = {name: Person(name=name, relation=relations[rel]) for name, rel in PEOPLE.items()}
    groups = {name: Group(name=name, members=[people[m] for m in members])
              for name, members in GROUPS.items()}
    session.add_all([*people.values(), *groups.values()])
    session.add_all(Occasion(type=types[t], year=y) for t, y in OCCASIONS)
    session.add(AppSetting(key="welcome_dismissed", value="1"))  # an established household
    targets = {"relation": relations, "person": people, "group": groups}
    for kind, target, occasion_type, dollars, basis in RULES:
        session.add(BudgetRule(occasion_type=types[occasion_type], amount_cents=dollars * 100,
                               basis=basis, **{kind: targets[kind][target]}))
    session.commit()
    return True
