"""The core rule: a user never sees a gift addressed to them, directly or via a group.

All gift titles here are made up.
"""

import re
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import Gift, Group, Photo, gift_photos
from app.visibility import GiftRepo


@pytest.fixture
def family(session, people):
    """A group that includes Abel."""
    group = Group(name="Family", members=[people["Abel"], people["Emma"]])
    session.add(group)
    session.flush()
    return group


@pytest.fixture
def gifts(session, people, groups, family):
    jules = GiftRepo(session, people["Jules"].id)
    abel = GiftRepo(session, people["Abel"].id)
    made = {
        "to_abel": jules.create(title="Telescope", recipient_person_id=people["Abel"].id,
                                 cost_cents=5000),
        "to_family": jules.create(title="Board game", recipient_group_id=family.id,
                                  cost_cents=3000),
        "to_jules": abel.create(title="Scarf", recipient_person_id=people["Jules"].id,
                                 cost_cents=2500),
        "to_emma": abel.create(title="Sketchbook", recipient_person_id=people["Emma"].id,
                                 cost_cents=1500),
        "to_grandkids": jules.create(title="Kite", recipient_group_id=groups["Grandkids"].id,
                                     cost_cents=1000),
    }
    session.commit()
    return made


def titles(gift_list):
    return sorted(g.title for g in gift_list)


def test_list_hides_direct_and_group_gifts(session, people, gifts):
    repo = GiftRepo(session, people["Abel"].id)
    assert titles(repo.list()) == ["Kite", "Scarf", "Sketchbook"]


def test_group_gifts_without_the_viewer_stay_visible(session, people, gifts):
    # Guards the SQL NULL trap: NOT(recipient_person_id = :viewer) is NULL for
    # group gifts and would silently drop them.
    repo = GiftRepo(session, people["Abel"].id)
    assert repo.get(gifts["to_grandkids"].id) is not None


def test_rule_is_symmetric_for_the_other_user(session, people, gifts):
    repo = GiftRepo(session, people["Jules"].id)
    assert titles(repo.list()) == ["Board game", "Kite", "Sketchbook", "Telescope"]


def test_get_returns_none_for_hidden_gifts(session, people, gifts):
    repo = GiftRepo(session, people["Abel"].id)
    assert repo.get(gifts["to_abel"].id) is None
    assert repo.get(gifts["to_family"].id) is None
    assert repo.get(gifts["to_emma"].id).title == "Sketchbook"


def test_counts_and_totals_exclude_hidden_gifts(session, people, gifts):
    repo = GiftRepo(session, people["Abel"].id)
    assert repo.count() == 3
    assert repo.total_cost_cents() == 2500 + 1500 + 1000


def test_filters_cannot_reach_hidden_gifts(session, people, gifts):
    repo = GiftRepo(session, people["Abel"].id)
    assert repo.list(Gift.recipient_person_id == people["Abel"].id) == []
    assert repo.count(Gift.title == "Telescope") == 0
    assert repo.total_cost_cents(Gift.title == "Telescope") == 0


def test_search_does_not_find_hidden_gifts(session, people, gifts):
    repo = GiftRepo(session, people["Abel"].id)
    assert repo.search("Telescope") == []
    assert repo.search("board") == []
    assert titles(repo.search("sketch")) == ["Sketchbook"]


def test_hidden_gifts_cannot_be_updated_or_deleted(session, people, gifts):
    repo = GiftRepo(session, people["Abel"].id)
    assert repo.update(gifts["to_abel"].id, title="Changed") is None
    assert repo.delete(gifts["to_family"].id) is False
    session.commit()
    assert session.get(Gift, gifts["to_abel"].id).title == "Telescope"
    assert session.get(Gift, gifts["to_family"].id) is not None


def test_visible_gifts_can_be_updated_and_deleted(session, people, gifts):
    repo = GiftRepo(session, people["Abel"].id)
    assert repo.update(gifts["to_emma"].id, status="wrapped").status == "wrapped"
    assert repo.delete(gifts["to_grandkids"].id) is True
    assert repo.count() == 2


def test_gift_you_create_for_yourself_is_hidden_from_you(session, people):
    repo = GiftRepo(session, people["Abel"].id)
    gift = repo.create(title="Self-gift", recipient_person_id=people["Abel"].id)
    assert repo.get(gift.id) is None


def test_membership_changes_apply_immediately(session, people, groups, gifts):
    repo = GiftRepo(session, people["Abel"].id)
    grandkids = groups["Grandkids"]

    grandkids.members.append(people["Abel"])
    session.commit()
    assert repo.get(gifts["to_grandkids"].id) is None

    grandkids.members.remove(people["Abel"])
    session.commit()
    assert repo.get(gifts["to_grandkids"].id) is not None


# --- photos --------------------------------------------------------------

def add_photo(session, uploader, *gift_list):
    photo = Photo(id=f"{len(session.scalars(select(Photo)).all()):032x}",
                  uploaded_by_id=uploader.id)
    session.add(photo)
    session.flush()
    for gift in gift_list:
        session.execute(gift_photos.insert().values(gift_id=gift.id, photo_id=photo.id))
    session.commit()
    return photo.id


def test_photo_of_visible_gift_is_visible(session, people, gifts):
    photo = add_photo(session, people["Abel"], gifts["to_emma"])
    assert GiftRepo(session, people["Abel"].id).photo_visible(photo)


def test_photo_of_hidden_gift_is_not_visible(session, people, gifts):
    photo = add_photo(session, people["Jules"], gifts["to_family"])
    assert not GiftRepo(session, people["Abel"].id).photo_visible(photo)
    assert GiftRepo(session, people["Jules"].id).photo_visible(photo)


def test_shared_photo_is_hidden_if_any_gift_using_it_is_hidden(session, people, gifts):
    # Like the Hazel/Sage books: one photo, two gifts.
    photo = add_photo(session, people["Jules"], gifts["to_emma"], gifts["to_abel"])
    assert not GiftRepo(session, people["Abel"].id).photo_visible(photo)


def test_unattached_photo_is_visible_only_to_its_uploader(session, people):
    photo = add_photo(session, people["Jules"])
    assert GiftRepo(session, people["Jules"].id).photo_visible(photo)
    assert not GiftRepo(session, people["Abel"].id).photo_visible(photo)


def test_unknown_photo_is_not_visible(session, people):
    assert not GiftRepo(session, people["Abel"].id).photo_visible("0" * 32)


# --- structural guard ----------------------------------------------------

DIRECT_QUERY = re.compile(
    r"(select|query|get)\(\s*(Gift|Photo|gift_photos)\b"
    r"|\b(Gift|Photo)\s*\)\s*\.where"
    r"|from_statement|text\(\s*[\"'].*\b(gifts|photos)\b",
    re.IGNORECASE,
)


def test_no_module_queries_gifts_or_photos_directly():
    """Only app/visibility.py may build gift or photo queries."""
    app_dir = Path(__file__).parent.parent / "app"
    offenders = []
    for path in app_dir.rglob("*.py"):
        if path.name == "visibility.py" or "migrations" in path.parts:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if DIRECT_QUERY.search(line):
                offenders.append(f"{path.relative_to(app_dir)}:{lineno}: {line.strip()}")
    assert offenders == [], "Use GiftRepo instead:\n" + "\n".join(offenders)
