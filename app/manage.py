"""Rules for the Settings pages: budget rules, people, groups, relations, occasion types.

Budgets aimed at the viewer (a person rule for them, or a group rule for a group
they're in) are hidden from them, like their gifts. Relation rules stay visible to
both users: they're the shared "what do we usually spend" reference.
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.gifts import FormError, parse_price
from app.models import BASES, DATE_RULES, BudgetRule, Group, OccasionType, Person, Relation

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]
DAYS_IN_MONTH = [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]  # Feb 29 birthdays exist


# --- budget rules ----------------------------------------------------------------

def rule_visible_to(rule: BudgetRule, viewer_id: int) -> bool:
    if rule.person_id is not None:
        return rule.person_id != viewer_id
    if rule.group is not None:
        return viewer_id not in {m.id for m in rule.group.members}
    return True


def visible_rules(session: Session, viewer_id: int) -> list[BudgetRule]:
    return [r for r in session.scalars(select(BudgetRule)) if rule_visible_to(r, viewer_id)]


def visible_rule(session: Session, viewer_id: int, rule_id: int) -> BudgetRule | None:
    rule = session.get(BudgetRule, rule_id)
    return rule if rule is not None and rule_visible_to(rule, viewer_id) else None


def rule_target_label(rule: BudgetRule) -> str:
    if rule.relation:
        return rule.relation.name
    if rule.person:
        return rule.person.name
    return rule.group.name


def rule_sort_key(rule: BudgetRule) -> tuple:
    """Relations (closest first), then people, then groups."""
    if rule.relation:
        return (0, rule.relation.position, "")
    if rule.person:
        return (1, 0, rule.person.name)
    return (2, 0, rule.group.name)


def parse_target(session: Session, key: str, viewer_id: int) -> dict:
    """"relation:3" / "person:5" / "group:2" -> the rule's target column."""
    kind, _, raw_id = (key or "").partition(":")
    model = {"relation": Relation, "person": Person, "group": Group}.get(kind)
    target = session.get(model, int(raw_id)) if model and raw_id.isdigit() else None
    if target is None:
        raise FormError("Pick who the budget is for")
    if kind == "person" and target.id == viewer_id:
        raise FormError("You can't set a budget for yourself")
    if kind == "group" and viewer_id in {m.id for m in target.members}:
        raise FormError("You can't set a budget for a group you're in")
    return {"relation_id": None, "person_id": None, "group_id": None, f"{kind}_id": target.id}


def parse_amount(raw: str) -> int:
    cents = parse_price(raw)
    if cents is None:
        raise FormError("Enter a budget amount")
    return cents


def check_rule(session: Session, *, occasion_type: OccasionType, relation_id=None,
               person_id=None, group_id=None, basis: str = "per_member",
               rule_id: int | None = None) -> None:
    """Raise FormError if this rule (new, or rule_id edited) breaks a budget rule."""
    if basis not in BASES:
        raise FormError("Unknown budget basis")
    if group_id is not None and occasion_type.date_rule == "birthday":
        raise FormError("Birthday budgets are per person: set them for a person or a relation, "
                        "not a group")
    clash = session.scalar(select(BudgetRule).where(
        BudgetRule.occasion_type_id == occasion_type.id,
        BudgetRule.relation_id.is_(relation_id) if relation_id is None
        else BudgetRule.relation_id == relation_id,
        BudgetRule.person_id.is_(person_id) if person_id is None
        else BudgetRule.person_id == person_id,
        BudgetRule.group_id.is_(group_id) if group_id is None
        else BudgetRule.group_id == group_id,
        BudgetRule.id != (rule_id or 0)))
    if clash is not None:
        raise FormError(f"There's already a {occasion_type.name} budget for "
                        f"{rule_target_label(clash)}")
    if group_id is not None:
        group = session.get(Group, group_id)
        check_one_group_budget(session, group, {m.id for m in group.members},
                               only_type=occasion_type)


def check_one_group_budget(session: Session, group: Group, member_ids: set[int],
                           only_type: OccasionType | None = None) -> None:
    """A person may be in at most one budgeted group per occasion type.

    Checked when a group rule is saved and when a budgeted group's members change."""
    types = [only_type] if only_type else [
        r.occasion_type for r in session.scalars(
            select(BudgetRule).where(BudgetRule.group_id == group.id))]
    for occasion_type in types:
        others = session.scalars(select(BudgetRule).where(
            BudgetRule.occasion_type_id == occasion_type.id,
            BudgetRule.group_id.is_not(None), BudgetRule.group_id != group.id))
        for other in others:
            shared = sorted(m.name for m in other.group.members if m.id in member_ids)
            if shared:
                raise FormError(
                    f"{' and '.join(shared)} {'is' if len(shared) == 1 else 'are'} already in the "
                    f"{other.group.name} {occasion_type.name} budget. A person can be in only "
                    f"one group budget per occasion.")


# --- people ----------------------------------------------------------------------

def parse_birthday(month: str | None, day: str | None,
                   what: str = "A birthday") -> tuple[int | None, int | None]:
    month, day = (month or "").strip(), (day or "").strip()
    if not month and not day:
        return None, None
    if not (month.isdigit() and day.isdigit()):
        raise FormError(f"{what} needs both a month and a day")
    m, d = int(month), int(day)
    if not 1 <= m <= 12 or not 1 <= d <= DAYS_IN_MONTH[m - 1]:
        raise FormError(f"{MONTHS[m - 1] if 1 <= m <= 12 else 'That month'} doesn't have a day {d}")
    return m, d


def person_fields(session: Session, form, person_id: int | None = None) -> dict:
    name = (form.get("name") or "").strip()
    if not name:
        raise FormError("A person needs a name")
    taken = session.scalar(select(Person).where(Person.name == name, Person.id != (person_id or 0)))
    if taken:
        raise FormError(f"There's already someone called {name}")
    relation_id = form.get("relation_id") or None
    if relation_id == "new":  # "+ New relation…", named in the box beside it
        label = (form.get("new_relation") or "").strip()
        if not label:
            raise FormError("Type the new relation's name")
        relation = session.scalar(select(Relation).where(Relation.name == label))  # already there
        if relation is None:
            relation = new_relation(session, label)
            session.add(relation)
            session.flush()
        relation_id = relation.id
    elif relation_id and session.get(Relation, int(relation_id)) is None:
        raise FormError("Unknown relation")
    month, day = parse_birthday(form.get("birthday_month"), form.get("birthday_day"))
    return {
        "name": name,
        "relation_id": int(relation_id) if relation_id else None,
        "birthday_month": month,
        "birthday_day": day,
        "notes": (form.get("notes") or "").strip() or None,
        "active": form.get("active") is not None,
    }


# --- groups ----------------------------------------------------------------------

def group_fields(session: Session, form, group: Group | None = None) -> tuple[str, list[Person]]:
    name = (form.get("name") or "").strip()
    if not name:
        raise FormError("A group needs a name")
    taken = session.scalar(select(Group).where(Group.name == name,
                                               Group.id != (group.id if group else 0)))
    if taken:
        raise FormError(f"There's already a group called {name}")
    ids = {int(v) for v in form.getlist("members") if v.isdigit()}
    members = [p for p in session.scalars(select(Person).where(Person.id.in_(ids)))]
    if len(members) < 2:
        raise FormError("A group needs at least two people")
    return name, members


# --- relations and occasion types ------------------------------------------------------

def relations(session: Session) -> list[Relation]:
    """Every relation, closest first: the one order used everywhere."""
    return list(session.scalars(select(Relation).order_by(Relation.position, Relation.id)))


def new_relation(session: Session, name: str) -> Relation:
    last = session.scalar(select(func.max(Relation.position))) or 0
    return Relation(name=name, position=last + 1)


def move_relation(session: Session, relation: Relation, step: int) -> None:
    """Swap with its neighbour above (step -1) or below (+1); no-op at either end."""
    order = relations(session)
    i = order.index(relation)
    j = i + step
    if 0 <= j < len(order):
        order[i], order[j] = order[j], order[i]
    for position, r in enumerate(order, 1):  # also tidies ties and gaps
        r.position = position


def relation_name(session: Session, form, relation_id: int | None = None) -> str:
    name = (form.get("name") or "").strip()
    if not name:
        raise FormError("A relation needs a name")
    if session.scalar(select(Relation).where(Relation.name == name,
                                             Relation.id != (relation_id or 0))):
        raise FormError(f"There's already a relation called {name}")
    return name


def relation_in_use(session: Session, relation: Relation) -> bool:
    return (session.scalar(select(Person.id).where(Person.relation_id == relation.id).limit(1))
            is not None or
            session.scalar(select(BudgetRule.id).where(BudgetRule.relation_id == relation.id)
                           .limit(1)) is not None)


def occasion_type_fields(session: Session, form, type_id: int | None = None) -> dict:
    name = (form.get("name") or "").strip()
    if not name:
        raise FormError("An occasion needs a name")
    if session.scalar(select(OccasionType).where(OccasionType.name == name,
                                                 OccasionType.id != (type_id or 0))):
        raise FormError(f"There's already an occasion called {name}")
    date_rule = form.get("date_rule") or "none"
    if date_rule not in DATE_RULES:
        raise FormError("Unknown date rule")
    month = day = None
    if date_rule == "fixed":
        month, day = parse_birthday(form.get("month"), form.get("day"), what="A fixed date")
        if month is None:
            raise FormError("A fixed date needs a month and a day")
    return {"name": name, "date_rule": date_rule, "month": month, "day": day}
