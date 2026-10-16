"""Gallery filters. Each filter becomes a WHERE clause passed to GiftRepo, which
adds the visibility rule; nothing here can widen what a viewer sees."""

from collections.abc import Mapping
from dataclasses import dataclass, fields
from urllib.parse import urlencode

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.orm import Session

from app.models import STATUSES, Gift, Group, Occasion, OccasionType, Person, Relation, group_members

NOT_GIVEN = "not_given"


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def involving(person_ids) -> ColumnElement[bool]:
    """Gifts to any of these people, directly or through a group they're in."""
    their_groups = select(group_members.c.group_id).where(group_members.c.person_id.in_(person_ids))
    return or_(Gift.recipient_person_id.in_(person_ids), Gift.recipient_group_id.in_(their_groups))


@dataclass(frozen=True)
class GalleryFilters:
    q: str = ""
    recipient: int | None = None   # a person, including group gifts they're part of
    relation: int | None = None    # anyone with this relation, likewise
    group: int | None = None       # the group, or any member individually
    occasion: int | None = None
    status: str = ""               # a status, or "not_given"
    source: str = ""
    needs: str = ""                # "details": no title yet

    @classmethod
    def from_params(cls, params: Mapping) -> "GalleryFilters":
        status = params.get("status") or ""
        return cls(
            q=(params.get("q") or "").strip(),
            recipient=_int(params.get("recipient")),
            relation=_int(params.get("relation")),
            group=_int(params.get("group")),
            occasion=_int(params.get("occasion")),
            status=status if status in (*STATUSES, NOT_GIVEN) else "",
            source=(params.get("source") or "").strip(),
            needs="details" if params.get("needs") == "details" else "",
        )

    def query_string(self) -> str:
        """Canonical form, so a saved filter matches however its URL was built."""
        return urlencode([(f.name, getattr(self, f.name)) for f in fields(self)
                          if getattr(self, f.name) not in (None, "")])

    @property
    def active(self) -> bool:
        return bool(self.query_string())

    def clauses(self) -> list[ColumnElement[bool]]:
        where = []
        if self.q:
            pattern = f"%{self.q}%"
            where.append(or_(Gift.title.ilike(pattern), Gift.notes.ilike(pattern),
                             Gift.source.ilike(pattern)))
        if self.recipient is not None:
            where.append(involving([self.recipient]))
        if self.relation is not None:
            where.append(involving(select(Person.id).where(Person.relation_id == self.relation)))
        if self.group is not None:
            members = select(group_members.c.person_id).where(group_members.c.group_id == self.group)
            where.append(or_(Gift.recipient_group_id == self.group,
                             Gift.recipient_person_id.in_(members)))
        if self.occasion is not None:
            where.append(Gift.occasion_id == self.occasion)
        if self.status == NOT_GIVEN:
            where.append(Gift.status != "given")
        elif self.status:
            where.append(Gift.status == self.status)
        if self.source:
            where.append(Gift.source == self.source)
        if self.needs:
            where.append(Gift.title.is_(None))
        return where


def filter_options(session: Session, viewer_id: int) -> dict:
    """Choices for the filter dropdowns. The viewer isn't offered as a recipient:
    every gift for them is hidden anyway."""
    return {
        "people": [p for p in session.scalars(select(Person).order_by(Person.name))
                   if p.id != viewer_id],
        "relations": list(session.scalars(select(Relation).order_by(Relation.name))),
        "groups": list(session.scalars(select(Group).where(~Group.ad_hoc).order_by(Group.name))),
        "occasions": list(session.scalars(select(Occasion).join(OccasionType)
                                          .order_by(Occasion.year.desc(), OccasionType.name))),
        "statuses": STATUSES,
    }
