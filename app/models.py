"""Database models.

Do not query Gift or Photo directly outside app/visibility.py; go through
GiftRepo so the "never see your own gifts" rule cannot be skipped.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, String, Table, Text,
    UniqueConstraint, text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

STATUSES = ("idea", "bought", "wrapped", "given")
BASES = ("per_member", "group_total")
# How an occasion type's date is determined each year.
DATE_RULES = ("fixed", "easter", "birthday", "none")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


group_members = Table(
    "group_members",
    Base.metadata,
    Column("group_id", ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True),
    Column("person_id", ForeignKey("people.id", ondelete="CASCADE"), primary_key=True),
)

gift_photos = Table(
    "gift_photos",
    Base.metadata,
    Column("gift_id", ForeignKey("gifts.id", ondelete="CASCADE"), primary_key=True),
    Column("photo_id", ForeignKey("photos.id", ondelete="CASCADE"), primary_key=True),
    Column("position", Integer, nullable=False, default=0),
)


class Relation(Base):
    __tablename__ = "relations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50), unique=True)


class Person(Base):
    __tablename__ = "people"
    __table_args__ = (
        CheckConstraint("birthday_month IS NULL OR birthday_month BETWEEN 1 AND 12"),
        CheckConstraint("birthday_day IS NULL OR birthday_day BETWEEN 1 AND 31"),
        CheckConstraint("(birthday_month IS NULL) = (birthday_day IS NULL)"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    relation_id: Mapped[int | None] = mapped_column(ForeignKey("relations.id"))
    birthday_month: Mapped[int | None]
    birthday_day: Mapped[int | None]
    # Set only for app users (Settings > App users). Their sign-in in both auth modes.
    login_email: Mapped[str | None] = mapped_column(String(254), unique=True)
    # AUTH_MODE=password only: see app/passwords.py. None until an invite is used.
    password_hash: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(default=True)

    relation: Mapped[Relation | None] = relationship()
    groups: Mapped[list["Group"]] = relationship(
        secondary=group_members, back_populates="members")

    def relation_label(self, viewer_id: int) -> str:
        """Users see themselves as "Self"; otherwise the household relation."""
        if self.id == viewer_id:
            return "Self"
        return self.relation.name if self.relation else ""


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    # Made on the fly by multi-selecting recipients; hidden from group lists
    # until someone names it.
    ad_hoc: Mapped[bool] = mapped_column(default=False, server_default="0")

    members: Mapped[list[Person]] = relationship(
        secondary=group_members, back_populates="groups", order_by=Person.name)


class OccasionType(Base):
    __tablename__ = "occasion_types"
    __table_args__ = (
        CheckConstraint(f"date_rule IN {DATE_RULES}"),
        CheckConstraint("(date_rule = 'fixed') = (month IS NOT NULL AND day IS NOT NULL)"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50), unique=True)
    date_rule: Mapped[str] = mapped_column(String(10), default="none")
    month: Mapped[int | None]
    day: Mapped[int | None]


class Occasion(Base):
    __tablename__ = "occasions"
    __table_args__ = (UniqueConstraint("type_id", "year"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    type_id: Mapped[int] = mapped_column(ForeignKey("occasion_types.id"))
    year: Mapped[int]

    type: Mapped[OccasionType] = relationship()

    @property
    def name(self) -> str:
        return f"{self.type.name} {self.year}"


class Photo(Base):
    __tablename__ = "photos"

    # Random UUID hex; also the on-disk file name, so URLs are unguessable.
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    uploaded_by_id: Mapped[int] = mapped_column(ForeignKey("people.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Gift(Base):
    __tablename__ = "gifts"
    __table_args__ = (
        # Exactly one recipient: a Person or a Group.
        CheckConstraint("(recipient_person_id IS NULL) != (recipient_group_id IS NULL)",
                        name="one_recipient"),
        CheckConstraint(f"status IN {STATUSES}", name="valid_status"),
        CheckConstraint("cost_cents IS NULL OR cost_cents >= 0", name="non_negative_cost"),
        Index("uq_gifts_import_key", "import_key", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str | None] = mapped_column(String(200))
    recipient_person_id: Mapped[int | None] = mapped_column(ForeignKey("people.id"), index=True)
    recipient_group_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id"), index=True)
    occasion_id: Mapped[int | None] = mapped_column(ForeignKey("occasions.id"), index=True)
    cost_cents: Mapped[int | None]
    source: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(10), default="bought")
    notes: Mapped[str | None] = mapped_column(Text)
    # Set by a one-time import from an earlier notes app (2026-10); unused since.
    import_key: Mapped[str | None] = mapped_column(String(200))
    created_by_id: Mapped[int] = mapped_column(ForeignKey("people.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    recipient_person: Mapped[Person | None] = relationship(foreign_keys=[recipient_person_id])
    recipient_group: Mapped[Group | None] = relationship()
    occasion: Mapped[Occasion | None] = relationship()
    created_by: Mapped[Person] = relationship(foreign_keys=[created_by_id])
    photos: Mapped[list[Photo]] = relationship(
        secondary=gift_photos, order_by=gift_photos.c.position)

    @property
    def recipients(self) -> list[Person]:
        if self.recipient_person:
            return [self.recipient_person]
        return list(self.recipient_group.members) if self.recipient_group else []


class BudgetRule(Base):
    __tablename__ = "budget_rules"
    __table_args__ = (
        CheckConstraint(
            "(relation_id IS NOT NULL) + (person_id IS NOT NULL) + (group_id IS NOT NULL) = 1",
            name="one_target"),
        CheckConstraint(f"basis IN {BASES}", name="valid_basis"),
        CheckConstraint("amount_cents >= 0", name="non_negative_amount"),
        # One rule per (occasion type, target). Partial indexes, because NULLs
        # never collide in a plain unique constraint.
        *(Index(f"uq_rule_{col}", "occasion_type_id", col, unique=True,
                sqlite_where=text(f"{col} IS NOT NULL"))
          for col in ("relation_id", "person_id", "group_id")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    occasion_type_id: Mapped[int] = mapped_column(ForeignKey("occasion_types.id"))
    relation_id: Mapped[int | None] = mapped_column(ForeignKey("relations.id"))
    person_id: Mapped[int | None] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"))
    group_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"))
    amount_cents: Mapped[int]
    # Only meaningful for group targets; relation and person rules are per person.
    basis: Mapped[str] = mapped_column(String(12), default="per_member")

    occasion_type: Mapped[OccasionType] = relationship()
    relation: Mapped[Relation | None] = relationship()
    person: Mapped[Person | None] = relationship()
    group: Mapped[Group | None] = relationship()


class SavedFilter(Base):
    """A named gallery view ("Christmas 2026", "Not given yet"), shared by both users.

    Stores only the filter's query string; results are always computed for the
    current viewer, so a saved filter can't expose hidden gifts."""

    __tablename__ = "saved_filters"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60), unique=True)
    query: Mapped[str] = mapped_column(String(500))
    position: Mapped[int] = mapped_column(default=0)


class LoginSession(Base):
    """A signed-in device (AUTH_MODE=password). The cookie holds a random token; only
    its SHA-256 is stored, so a copy of the database can't be used to sign in."""

    __tablename__ = "login_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    person: Mapped[Person] = relationship()


class Invite(Base):
    """A one-time link to choose a password (a new login, or a forgotten password)."""

    __tablename__ = "invites"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"), index=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("people.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    person: Mapped[Person] = relationship(foreign_keys=[person_id])


class AppSetting(Base):
    """Small household-wide flags, e.g. "welcome_dismissed"."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[str] = mapped_column(String(200))
