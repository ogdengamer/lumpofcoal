"""The budget dashboard: budget vs. spent per person or group, for one occasion.

Only gifts the viewer can see are counted (GiftRepo), so a viewer's totals never
include, or hint at, gifts for them. Their own row, and groups they're in, are
left out too, as are budgets aimed at them (see app/manage.py).

Each gift's cost is split evenly across its recipients. Each person's share goes to
the first budget that covers them for this occasion type:
  1. a group budget for a group they're in (so a gift for just Nils counts
     toward Nils & Stacy),
  2. their own person budget,
  3. their relation's budget,
  4. otherwise "No budget set".
"""

from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import BudgetRule, Gift, Occasion, OccasionType, Person
from app.visibility import GiftRepo

AMBER_FROM = 0.9  # within 10% of the budget


def easter(year: int) -> date:
    """Western Easter Sunday (anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def occasion_date(occasion: Occasion) -> date | None:
    """The day an occasion falls on, for types with one date (not birthdays)."""
    kind = occasion.type
    if kind.date_rule == "fixed":
        day = 28 if (kind.month, kind.day) == (2, 29) and not _leap(occasion.year) else kind.day
        return date(occasion.year, kind.month, day)
    if kind.date_rule == "easter":
        return easter(occasion.year)
    return None


def next_occasion(session: Session, today: date) -> Occasion | None:
    """The next dated occasion on or after today (Christmas, Easter, ...)."""
    upcoming = [(d, o) for o in session.scalars(select(Occasion))
                if (d := occasion_date(o)) is not None and d >= today]
    return min(upcoming, key=lambda pair: pair[0])[1] if upcoming else None


def next_birthday(person: Person, today: date) -> date | None:
    if person.birthday_month is None:
        return None
    for year in (today.year, today.year + 1):
        day = person.birthday_day
        if (person.birthday_month, day) == (2, 29) and not _leap(year):
            day = 28
        when = date(year, person.birthday_month, day)
        if when >= today:
            return when
    return None


def _leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


# --- rows ------------------------------------------------------------------------------

@dataclass
class Row:
    kind: str                 # "person" or "group"
    id: int
    name: str
    detail: str               # relation name, or "group"
    budget_cents: int | None  # None: no budget set
    spent_cents: int = 0
    planned_cents: int = 0
    gift_ids: set[int] = field(default_factory=set)
    birthday: date | None = None

    @property
    def left_cents(self) -> int | None:
        return None if self.budget_cents is None else self.budget_cents - self.spent_cents

    @property
    def gift_count(self) -> int:
        return len(self.gift_ids)

    @property
    def status(self) -> str:
        """green / amber (within 10%) / red (over), by money actually spent."""
        if self.budget_cents is None:
            return "none"
        if self.spent_cents > self.budget_cents:
            return "red"
        if self.budget_cents and self.spent_cents >= AMBER_FROM * self.budget_cents:
            return "amber"
        return "green"

    @property
    def no_gifts(self) -> bool:
        return self.budget_cents is not None and not self.gift_ids


@dataclass
class Dashboard:
    occasion: Occasion
    rows: list[Row]            # with a budget
    unbudgeted: list[Row]      # gifts, but no budget covers them

    @property
    def budget_cents(self) -> int:
        return sum(r.budget_cents for r in self.rows)

    @property
    def spent_cents(self) -> int:
        return sum(r.spent_cents for r in self.rows)

    @property
    def planned_cents(self) -> int:
        return sum(r.planned_cents for r in self.rows)

    @property
    def left_cents(self) -> int:
        return self.budget_cents - self.spent_cents

    @property
    def unbudgeted_spent_cents(self) -> int:
        return sum(r.spent_cents for r in self.unbudgeted)

    @property
    def gift_count(self) -> int:
        return len(set().union(*(r.gift_ids for r in self.rows + self.unbudgeted)))


def split(cents: int, ways: int) -> list[int]:
    """Even shares that add back up exactly: $10 three ways is 334, 333, 333."""
    base, extra = divmod(cents, ways)
    return [base + (1 if i < extra else 0) for i in range(ways)]


def build(session: Session, repo: GiftRepo, occasion: Occasion, today: date) -> Dashboard:
    viewer = repo.viewer_id
    rules = list(session.scalars(select(BudgetRule).where(
        BudgetRule.occasion_type_id == occasion.type_id)))
    by_relation = {r.relation_id: r for r in rules if r.relation_id}
    by_person = {r.person_id: r for r in rules if r.person_id}
    # Budgets for groups the viewer is in are hidden from them; their other members'
    # shares fall through to their own person or relation budget.
    group_rules = sorted((r for r in rules if r.group_id
                          and viewer not in {m.id for m in r.group.members}),
                         key=lambda r: r.group.name)
    group_of: dict[int, BudgetRule] = {}  # person -> their budgeted group (at most one)
    for rule in group_rules:
        for member in rule.group.members:
            group_of.setdefault(member.id, rule)

    rows: dict[tuple[str, int], Row] = {}
    unbudgeted: dict[int, Row] = {}

    def person_row(person: Person) -> Row:
        """The row a person's share lands in."""
        if person.id in group_of:
            rule = group_of[person.id]
            return rows[("group", rule.group_id)]
        rule = by_person.get(person.id) or by_relation.get(person.relation_id)
        if rule is None:
            if person.id not in unbudgeted:
                unbudgeted[person.id] = Row("person", person.id, person.name,
                                            person.relation.name if person.relation else "", None)
            return unbudgeted[person.id]
        key = ("person", person.id)
        if key not in rows:
            rows[key] = Row("person", person.id, person.name,
                            person.relation.name if person.relation else "", rule.amount_cents,
                            birthday=next_birthday(person, today))
        return rows[key]

    # Every budget gets a row, even before any gifts ("no gifts yet").
    for rule in group_rules:
        members = rule.group.members
        amount = rule.amount_cents * (len(members) if rule.basis == "per_member" else 1)
        rows[("group", rule.group_id)] = Row("group", rule.group_id, rule.group.name, "group", amount)
    for person in session.scalars(select(Person).where(Person.active)):
        if person.id != viewer and (person.id in by_person or person.relation_id in by_relation):
            person_row(person)

    for gift in repo.list(Gift.occasion_id == occasion.id):
        recipients = [p for p in gift.recipients if p.id != viewer]
        if not recipients:
            continue
        for person, share in zip(recipients, split(gift.cost_cents or 0, len(recipients))):
            row = person_row(person)
            row.gift_ids.add(gift.id)
            if gift.status == "idea":
                row.planned_cents += share
            else:
                row.spent_cents += share

    budgeted = list(rows.values())
    if occasion.type.date_rule == "birthday":
        far = today + timedelta(days=400)
        budgeted.sort(key=lambda r: (r.birthday or far, r.name))
    else:
        closeness = {p.id: (p.relation_id or 10_000) for p in session.scalars(select(Person))}
        budgeted.sort(key=lambda r: (r.kind == "group", closeness.get(r.id, 0)
                                     if r.kind == "person" else 0, r.name))
    return Dashboard(occasion, budgeted, sorted(unbudgeted.values(), key=lambda r: r.name))


# --- upcoming birthdays ----------------------------------------------------------------------

@dataclass
class Upcoming:
    person: Person
    when: date
    budget_cents: int | None
    spent_cents: int


def upcoming_birthdays(session: Session, repo: GiftRepo, today: date,
                       days: int = 60) -> list[Upcoming]:
    birthday_type = session.scalar(select(OccasionType).where(OccasionType.date_rule == "birthday"))
    if birthday_type is None:
        return []
    soon = []
    for person in session.scalars(select(Person).where(Person.active)):
        when = next_birthday(person, today)
        if person.id != repo.viewer_id and when and when <= today + timedelta(days=days):
            soon.append((when, person))
    boards: dict[int, Dashboard] = {}
    out = []
    for when, person in sorted(soon, key=lambda pair: (pair[0], pair[1].name)):
        occasion = session.scalar(select(Occasion).where(Occasion.type_id == birthday_type.id,
                                                         Occasion.year == when.year))
        if occasion is None:
            continue
        if when.year not in boards:
            boards[when.year] = build(session, repo, occasion, today)
        board = boards[when.year]
        row = next((r for r in board.rows + board.unbudgeted
                    if r.kind == "person" and r.id == person.id), None)
        out.append(Upcoming(person, when, row.budget_cents if row else None,
                            row.spent_cents if row else 0))
    return out


def gifts_without_occasion(repo: GiftRepo) -> list[Gift]:
    return repo.list(Gift.occasion_id.is_(None))
