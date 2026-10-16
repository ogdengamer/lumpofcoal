"""Add-gift form logic: prices, recipient chips, sticky defaults, ad hoc groups."""

import re
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import BudgetRule, Group, Occasion, OccasionType, Person
from app.visibility import GiftRepo

TOP_CHIPS = 6


class FormError(ValueError):
    pass


def parse_price(raw: str | None) -> int | None:
    """"$12.50", "12", "12.5", "1,200" -> cents. Blank -> None."""
    text = re.sub(r"[\s$,]", "", raw or "")
    if not text:
        return None
    if not re.fullmatch(r"\d*\.?\d*", text):  # Decimal alone would accept "1e3", "NaN"
        raise FormError("Cost should be a number, like 12.50")
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise FormError("Cost should be a number, like 12.50")
    if value < 0 or value != value.quantize(Decimal("0.01")) or value > 100_000:
        raise FormError("Cost should be a number, like 12.50")
    return int(value * 100)


def format_price(cents: int | None) -> str:
    if cents is None:
        return ""
    return f"{cents / 100:.2f}".removesuffix(".00")


# --- recipient chips -------------------------------------------------------

@dataclass(frozen=True)
class Chip:
    kind: str  # "person" or "group"
    id: int
    label: str

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.id}"


def recipient_chips(session: Session, repo: GiftRepo) -> tuple[list[Chip], list[Chip]]:
    """(top chips, the rest). Ranked by how often, then how recently, the viewer
    used each recipient, then by closest relation. The viewer, and groups
    containing them, are left out: a gift to either would vanish from the
    viewer's own view.

    "Closest" is relation order: the seed adds relations closest first (Partner,
    Child, Grandchild, ...), so a lower id is closer."""
    viewer = repo.viewer_id
    by_closeness = (select(Person).where(Person.active)
                    .order_by(Person.relation_id.is_(None), Person.relation_id, Person.name))
    people = [p for p in session.scalars(by_closeness) if p.id != viewer]
    groups = [g for g in session.scalars(select(Group).where(~Group.ad_hoc).order_by(Group.name))
              if viewer not in {m.id for m in g.members}]
    chips = {("person", p.id): Chip("person", p.id, p.name) for p in people}
    chips |= {("group", g.id): Chip("group", g.id, g.name) for g in groups}

    used = [("person", pid) if pid else ("group", gid) for pid, gid in repo.recent_recipients()]
    counts = Counter(used)
    recency = {key: i for i, key in reversed(list(enumerate(used)))}
    ranked = sorted((k for k in counts if k in chips), key=lambda k: (-counts[k], recency[k]))

    top = [chips[k] for k in ranked[:TOP_CHIPS]]
    if len(top) < TOP_CHIPS:  # pad with the closest people, e.g. for a new user
        top += [c for k, c in chips.items() if c not in top and c.kind == "person"][
            :TOP_CHIPS - len(top)]
    rest = [c for c in chips.values() if c not in top]  # people by closeness, then groups
    return top, rest


def group_budget_hints(session: Session, viewer_id: int) -> dict[int, list[dict]]:
    """person_id -> groups with a budget rule that the person belongs to.

    Lets the form say "Nils is in the Nils & Stacy Christmas budget"
    without a server round trip. Budgets for groups the viewer is in are hidden
    from them, like their gifts."""
    hints: dict[int, list[dict]] = {}
    rules = session.scalars(select(BudgetRule).where(BudgetRule.group_id.is_not(None)))
    for rule in rules:
        if viewer_id in {m.id for m in rule.group.members}:
            continue
        for member in rule.group.members:
            hints.setdefault(member.id, []).append({
                "group_id": rule.group.id,
                "group": rule.group.name,
                "occasion_type_id": rule.occasion_type_id,
                "amount": format_price(rule.amount_cents),
                "basis": rule.basis,
            })
    return hints


# --- people ----------------------------------------------------------------

NEW_PERSON = "new"  # the recipient key for a person typed into the gift form


def find_or_add_person(session: Session, name: str, viewer_id: int) -> Person:
    """The person with this name (any capitals), else a new one with no relation yet.

    Someone inactive is made active again: you're giving them a gift."""
    name = " ".join((name or "").split())
    if not name:
        raise FormError("Type the new person's name")
    if len(name) > 100:
        raise FormError("That name is too long")
    person = session.scalar(select(Person).where(func.lower(Person.name) == name.lower()))
    if person is None:
        person = Person(name=name)
        session.add(person)
        session.flush()
    elif person.id == viewer_id:
        raise FormError("That's you: gifts for you are hidden from you, so they can't be added here")
    person.active = True
    return person


# --- groups ----------------------------------------------------------------

def find_or_create_group(session: Session, member_ids: set[int],
                         name: str | None = None) -> Group:
    """The group with exactly these members, created (ad hoc) if needed.

    Naming it saves it as a regular group."""
    if len(member_ids) < 2:
        raise FormError("A group needs at least two people")
    members = list(session.scalars(select(Person).where(Person.id.in_(member_ids))))
    if len(members) != len(member_ids):
        raise FormError("Unknown recipient")
    name = (name or "").strip() or None

    for group in session.scalars(select(Group)):
        if {m.id for m in group.members} == member_ids:
            if name and group.ad_hoc:
                group.name, group.ad_hoc = _unique_group_name(session, name, group), False
            return group

    label = name or " & ".join(sorted(m.name for m in members))
    group = Group(name=_unique_group_name(session, label), members=members, ad_hoc=name is None)
    session.add(group)
    session.flush()
    return group


def _unique_group_name(session: Session, name: str, current: Group | None = None) -> str:
    taken = {g.name for g in session.scalars(select(Group)) if g is not current}
    candidate, n = name, 2
    while candidate in taken:
        candidate, n = f"{name} ({n})", n + 1
    return candidate


# --- occasions -------------------------------------------------------------

def ensure_occasions(session: Session, today: date) -> None:
    """Every occasion type has an occasion for this year and next."""
    existing = {(o.type_id, o.year) for o in session.scalars(select(Occasion))}
    for occasion_type in session.scalars(select(OccasionType)):
        for year in (today.year, today.year + 1):
            if (occasion_type.id, year) not in existing:
                session.add(Occasion(type=occasion_type, year=year))
    session.commit()


def default_occasion(session: Session, repo: GiftRepo, today: date) -> Occasion | None:
    """The occasion of the viewer's last gift, else the next Christmas."""
    last = repo.last_created()
    if last and last.occasion:
        return last.occasion
    year = today.year if (today.month, today.day) <= (12, 25) else today.year + 1
    return session.scalar(select(Occasion).join(OccasionType)
                          .where(OccasionType.name == "Christmas", Occasion.year == year))


def occasion_choices(session: Session, today: date) -> list[Occasion]:
    """Occasions from last year on, soonest year first."""
    stmt = (select(Occasion).join(OccasionType).where(Occasion.year >= today.year - 1)
            .order_by(Occasion.year, OccasionType.name))
    return list(session.scalars(stmt))


def known_sources(session: Session, repo: GiftRepo) -> list[str]:
    """Sources on gifts the viewer can see, most recently used first."""
    seen: dict[str, None] = {}
    for gift in repo.list():
        if gift.source:
            seen.setdefault(gift.source, None)
    return list(seen)
