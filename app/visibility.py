"""The visibility rule, enforced in one place.

A viewer can never see a gift that has their own Person among its recipients,
directly or through a group. Every read or write of gifts and photos goes
through GiftRepo, which applies that rule to the query itself. Routes,
dashboards, exports and photo serving must not query Gift or Photo directly
(tests/test_visibility.py checks this).

Hidden gifts behave as if they don't exist: get() returns None (so routes
answer 404, not 403), and counts and totals leave them out.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ColumnElement, and_, exists, func, not_, or_, select
from sqlalchemy.orm import Session

from app.models import Gift, Photo, gift_photos, group_members


def _viewer_group_ids(viewer_id: int):
    return select(group_members.c.group_id).where(group_members.c.person_id == viewer_id)


def gift_hidden_from(viewer_id: int) -> ColumnElement[bool]:
    # Each branch is NULL-guarded: a bare `recipient_person_id = :viewer` is NULL
    # for group gifts, and NOT(NULL) would wrongly drop those gifts too.
    return or_(
        and_(Gift.recipient_person_id.is_not(None), Gift.recipient_person_id == viewer_id),
        and_(Gift.recipient_group_id.is_not(None),
             Gift.recipient_group_id.in_(_viewer_group_ids(viewer_id))),
    )


def gift_visible_to(viewer_id: int) -> ColumnElement[bool]:
    return not_(gift_hidden_from(viewer_id))


class GiftRepo:
    def __init__(self, session: Session, viewer_id: int):
        self.session = session
        self.viewer_id = viewer_id

    # --- reads -----------------------------------------------------------

    def query(self):
        """Base SELECT of visible gifts. Add filters and ordering to it."""
        return select(Gift).where(gift_visible_to(self.viewer_id))

    def list(self, *where) -> list[Gift]:
        stmt = self.query().where(*where).order_by(Gift.created_at.desc(), Gift.id.desc())
        return list(self.session.scalars(stmt))

    def get(self, gift_id: int) -> Gift | None:
        return self.session.scalar(self.query().where(Gift.id == gift_id))

    def search(self, text: str) -> list[Gift]:
        pattern = f"%{text}%"
        return self.list(or_(Gift.title.ilike(pattern), Gift.notes.ilike(pattern),
                             Gift.source.ilike(pattern)))

    def count(self, *where) -> int:
        stmt = select(func.count()).select_from(Gift).where(
            gift_visible_to(self.viewer_id), *where)
        return self.session.scalar(stmt)

    def total_cost_cents(self, *where) -> int:
        stmt = select(func.coalesce(func.sum(Gift.cost_cents), 0)).where(
            gift_visible_to(self.viewer_id), *where)
        return self.session.scalar(stmt)

    def last_created(self) -> Gift | None:
        """The viewer's most recent visible gift, for sticky form defaults."""
        stmt = (self.query().where(Gift.created_by_id == self.viewer_id)
                .order_by(Gift.created_at.desc(), Gift.id.desc()).limit(1))
        return self.session.scalar(stmt)

    def recent_recipients(self, limit: int = 60) -> list[tuple[int | None, int | None]]:
        """(person_id, group_id) of the viewer's recent visible gifts, newest first.

        Only visible gifts count, so chip order can't hint at hidden gifts.
        """
        stmt = (select(Gift.recipient_person_id, Gift.recipient_group_id)
                .where(gift_visible_to(self.viewer_id), Gift.created_by_id == self.viewer_id)
                .order_by(Gift.created_at.desc(), Gift.id.desc()).limit(limit))
        return [tuple(row) for row in self.session.execute(stmt)]

    # --- writes ----------------------------------------------------------

    def create(self, **fields) -> Gift:
        gift = Gift(created_by_id=self.viewer_id, **fields)
        self.session.add(gift)
        self.session.flush()
        return gift

    def update(self, gift_id: int, **fields) -> Gift | None:
        gift = self.get(gift_id)
        if gift is None:
            return None
        for name, value in fields.items():
            setattr(gift, name, value)
        self.session.flush()
        return gift

    def delete(self, gift_id: int) -> bool:
        gift = self.get(gift_id)
        if gift is None:
            return False
        self.session.delete(gift)
        self.session.flush()
        return True

    # --- photos ----------------------------------------------------------

    def detach_photo(self, gift_id: int, photo_id: str) -> bool:
        """Remove a photo from a visible gift. The photo itself is deleted later
        by delete_orphan_photos if no other gift uses it."""
        if self.get(gift_id) is None:
            return False
        self.session.execute(gift_photos.delete().where(
            gift_photos.c.gift_id == gift_id, gift_photos.c.photo_id == photo_id))
        self.session.flush()
        return True

    def add_photo(self, photo_id: str) -> Photo:
        """Record a newly uploaded photo, not yet attached to any gift."""
        photo = Photo(id=photo_id, uploaded_by_id=self.viewer_id)
        self.session.add(photo)
        self.session.flush()
        return photo

    def attach_photo(self, gift_id: int, photo_id: str) -> bool:
        """Attach a photo the viewer can see to a gift the viewer can see."""
        gift = self.get(gift_id)
        if gift is None or not self.photo_visible(photo_id):
            return False
        if photo_id not in {p.id for p in gift.photos}:
            position = len(gift.photos)
            self.session.execute(gift_photos.insert().values(
                gift_id=gift_id, photo_id=photo_id, position=position))
            self.session.expire(gift, ["photos"])
        self.session.flush()
        return True

    def photo_visible(self, photo_id: str) -> bool:
        """A photo is visible only if every gift using it is visible.

        A photo not yet attached to any gift (e.g. just shared in) is visible
        only to whoever uploaded it.
        """
        photo = self.session.get(Photo, photo_id)
        if photo is None:
            return False
        linked = select(gift_photos.c.gift_id).where(gift_photos.c.photo_id == photo_id)
        if not self.session.scalar(select(exists(linked))):
            return photo.uploaded_by_id == self.viewer_id
        hidden_link = (select(Gift.id)
                       .where(Gift.id.in_(linked), gift_hidden_from(self.viewer_id)))
        return not self.session.scalar(select(exists(hidden_link)))


def delete_orphan_photos(session: Session, store, older_than: datetime) -> int:
    """Remove uploads never attached to a gift (e.g. an abandoned add form).

    System housekeeping, not viewer-scoped: it only deletes, never returns gifts.
    """
    attached = select(gift_photos.c.photo_id)
    stale = list(session.scalars(
        select(Photo.id).where(Photo.id.not_in(attached), Photo.created_at < older_than)))
    for photo_id in stale:
        session.delete(session.get(Photo, photo_id))
        store.delete(photo_id)
    session.commit()
    return len(stale)

