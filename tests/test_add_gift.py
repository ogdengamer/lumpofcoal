"""Adding gifts: form logic, the save route, the share target, PWA plumbing, CSRF.

All gift titles are made up.
"""

import html as html_lib
import io
import json
import re
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.gifts import (
    FormError, default_occasion, ensure_occasions, find_or_create_group, format_price,
    group_budget_hints, known_sources, parse_price, recipient_chips,
)
from app.main import create_app
from app.models import Gift, Group, Occasion, Person, Photo
from app.visibility import GiftRepo, delete_orphan_photos

# --- price -----------------------------------------------------------------

@pytest.mark.parametrize("raw, cents", [
    ("12", 1200), ("12.5", 1250), ("$12.50", 1250), (" 1,200 ", 120000), ("0", 0),
    (".99", 99), ("", None), (None, None), ("  ", None),
])
def test_parse_price(raw, cents):
    assert parse_price(raw) == cents


@pytest.mark.parametrize("raw", ["abc", "-5", "1.234", "12..5", "1e3", "NaN", "Infinity", "200000"])
def test_parse_price_rejects(raw):
    with pytest.raises(FormError):
        parse_price(raw)


def test_format_price():
    assert (format_price(1200), format_price(1250), format_price(None)) == ("12", "12.50", "")


# --- chips -------------------------------------------------------------------

def keys(chips):
    return [c.label for c in chips]


def test_chips_exclude_viewer_and_their_groups(session, people, groups):
    session.add(Group(name="Family", members=[people["Abel"], people["Emma"]]))
    session.commit()
    top, rest = recipient_chips(session, GiftRepo(session, people["Abel"].id))
    labels = keys(top) + keys(rest)
    assert "Abel" not in labels and "Family" not in labels
    assert "Jules" in labels and "Grandkids" in labels


def test_chips_rank_by_frequency_then_recency(session, people, groups):
    repo = GiftRepo(session, people["Abel"].id)
    for name in ["Elsa", "Elsa", "Pop", "Elsa"]:
        repo.create(title="x", recipient_person_id=people[name].id)
    repo.create(title="x", recipient_group_id=groups["Grandkids"].id)
    session.commit()
    top, _ = recipient_chips(session, repo)
    assert keys(top)[:3] == ["Elsa", "Grandkids", "Pop"]


def test_unused_chips_rank_closest_relation_first(session, people, groups):
    top, rest = recipient_chips(session, GiftRepo(session, people["Abel"].id))
    assert keys(top) == ["Jules", "Astrid", "Elsa", "Emma", "Shay", "Cal"]
    assert keys(rest)[:2] == ["Evan", "Gina"] and keys(rest)[-3:] == [
        "Girls", "Grandkids", "Nils & Stacy"]


def test_chip_ranking_ignores_hidden_gifts(session, people):
    # Jules gives Abel many gifts; that must not affect (or reveal via) Abel's chips.
    jules = GiftRepo(session, people["Jules"].id)
    for _ in range(5):
        jules.create(title="x", recipient_person_id=people["Abel"].id)
    session.commit()
    assert GiftRepo(session, people["Abel"].id).recent_recipients() == []


def test_source_suggestions_come_only_from_visible_gifts(session, people):
    jules = GiftRepo(session, people["Jules"].id)
    jules.create(title="x", recipient_person_id=people["Abel"].id, source="Secret Shop")
    jules.create(title="x", recipient_person_id=people["Emma"].id, source="Etsy")
    session.commit()
    assert known_sources(session, GiftRepo(session, people["Abel"].id)) == ["Etsy"]


def test_ad_hoc_groups_are_not_chips(session, people):
    find_or_create_group(session, {people["Emma"].id, people["Elsa"].id})
    top, rest = recipient_chips(session, GiftRepo(session, people["Abel"].id))
    assert "Elsa & Emma" not in keys(top) + keys(rest)


# --- groups ------------------------------------------------------------------

def test_existing_group_is_reused(session, people, groups):
    ids = {people["Cal"].id, people["Gina"].id}
    assert find_or_create_group(session, ids) is groups["Girls"]


def test_new_selection_makes_an_ad_hoc_group(session, people):
    group = find_or_create_group(session, {people["Emma"].id, people["Elsa"].id})
    assert (group.name, group.ad_hoc) == ("Elsa & Emma", True)


def test_naming_an_ad_hoc_group_saves_it(session, people):
    ids = {people["Emma"].id, people["Elsa"].id}
    ad_hoc = find_or_create_group(session, ids)
    named = find_or_create_group(session, ids, name="The Sisters")
    assert named is ad_hoc and (named.name, named.ad_hoc) == ("The Sisters", False)


def test_group_names_stay_unique(session, people):
    group = find_or_create_group(session, {people["Pop"].id, people["Nell"].id}, name="Grandkids")
    assert group.name == "Grandkids (2)"


def test_group_needs_two_people(session, people):
    with pytest.raises(FormError):
        find_or_create_group(session, {people["Pop"].id})


def test_group_budget_hints(session, people, groups):
    hints = group_budget_hints(session, people["Abel"].id)
    assert hints[people["Nils"].id][0]["group"] == "Nils & Stacy"
    assert hints[people["Nils"].id][0]["amount"] == "30"
    assert people["Emma"].id not in hints


# --- occasions and sticky defaults ---------------------------------------------

def test_ensure_occasions_covers_this_and_next_year(session):
    ensure_occasions(session, date(2027, 3, 1))
    names = {o.name for o in session.scalars(select(Occasion))}
    assert {"Christmas 2027", "Christmas 2028", "Easter 2027", "Just Because 2028"} <= names


@pytest.mark.parametrize("today, year", [(date(2026, 10, 14), 2026), (date(2026, 12, 25), 2026),
                                         (date(2026, 12, 26), 2027)])
def test_default_occasion_is_next_christmas(session, people, today, year):
    ensure_occasions(session, today)
    occasion = default_occasion(session, GiftRepo(session, people["Abel"].id), today)
    assert occasion.name == f"Christmas {year}"


def test_default_occasion_sticks_to_last_gift(session, people):
    birthday = session.scalar(select(Occasion).where(Occasion.year == 2026, Occasion.type_id == 2))
    repo = GiftRepo(session, people["Abel"].id)
    repo.create(title="x", recipient_person_id=people["Emma"].id, occasion_id=birthday.id)
    session.commit()
    assert default_occasion(session, repo, date(2026, 10, 14)) is birthday


# --- HTTP ----------------------------------------------------------------------

USERS = {"abel@example.com": "Abel", "jules@example.com": "Jules"}


@pytest.fixture
def clients(tmp_path):
    def make(email):
        return TestClient(create_app(Settings(users=USERS, dev_user=email,
                                              data_dir=str(tmp_path))))
    return make("abel@example.com"), make("jules@example.com")


@pytest.fixture
def ids(clients):
    with clients[0].app.state.sessionmaker() as session:
        return {
            **{p.name: p.id for p in session.scalars(select(Person))},
            **{f"group:{g.name}": g.id for g in session.scalars(select(Group))},
            **{f"occ:{o.name}": o.id for o in session.scalars(select(Occasion))},
        }


def jpeg() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 30, 30)).save(out, format="JPEG")
    return out.getvalue()


def upload(client) -> str:
    resp = client.post("/photos", files={"file": ("p.jpg", jpeg(), "image/jpeg")})
    assert resp.status_code == 201
    return resp.json()["id"]


def gifts_in_db(client):
    with client.app.state.sessionmaker() as session:
        stmt = select(Gift).order_by(Gift.id).options(
            selectinload(Gift.photos),
            selectinload(Gift.recipient_group).selectinload(Group.members))
        return list(session.scalars(stmt))


def test_every_page_has_the_one_tap_camera_button(clients):
    abel, _ = clients
    html = abel.get("/").text
    assert 'for="fab-camera"' in html
    assert re.search(r'id="fab-camera"[^>]*accept="image/\*"[^>]*capture="environment"', html)
    assert '<dialog id="add-dialog"' in html


def test_fast_path_photo_recipient_price_save(clients, ids):
    abel, _ = clients
    photo = upload(abel)
    resp = abel.post("/gifts", data={"photo_id": photo, "recipients": f"person:{ids['Emma']}",
                                      "price": "24.99", "occasion_id": ids["occ:Christmas 2026"]},
                      follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/?saved=")

    [gift] = gifts_in_db(abel)
    assert (gift.recipient_person_id, gift.cost_cents, gift.title) == (ids["Emma"], 2499, None)
    assert [p.id for p in gift.photos] == [photo]
    assert gift.status == "bought" and gift.created_by_id == ids["Abel"]

    home = abel.get(resp.headers["location"]).text
    assert "Saved gift for Emma" in home and f"/photos/{photo}/thumb" in home


def test_occasion_and_source_stick(clients, ids):
    abel, _ = clients
    abel.post("/gifts", data={"recipients": f"person:{ids['Pop']}", "source": "Made-up Market",
                               "occasion_id": ids["occ:Birthday 2026"]})
    form = abel.get("/gifts/new").text
    assert 'value="Made-up Market"' in form
    assert re.search(rf'value="{ids["occ:Birthday 2026"]}"[^>]*selected', form)


def test_several_people_become_a_group_gift(clients, ids):
    abel, _ = clients
    abel.post("/gifts", data={"recipients": [f"person:{ids['Emma']}", f"person:{ids['Elsa']}"],
                               "group_name": "The Sisters", "price": "40"})
    [gift] = gifts_in_db(abel)
    assert gift.recipient_group.name == "The Sisters"
    assert {m.name for m in gift.recipient_group.members} == {"Emma", "Elsa"}
    assert not gift.recipient_group.ad_hoc


def test_selecting_an_existing_groups_members_reuses_it(clients, ids):
    abel, _ = clients
    abel.post("/gifts", data={"recipients": [f"person:{ids['Cal']}", f"person:{ids['Gina']}"]})
    [gift] = gifts_in_db(abel)
    assert gift.recipient_group_id == ids["group:Girls"]


def test_group_chip_saves_as_that_group(clients, ids):
    abel, _ = clients
    abel.post("/gifts", data={"recipients": f"group:{ids['group:Nils & Stacy']}"})
    assert gifts_in_db(abel)[0].recipient_group_id == ids["group:Nils & Stacy"]


@pytest.mark.parametrize("data, message", [
    ({}, "Pick who it&#39;s for"),
    ({"recipients": "person:99999"}, "Unknown recipient"),
    ({"recipients": "bogus"}, "Unknown recipient"),
    ({"recipients": "PERSON", "price": "abc"}, "Unknown recipient"),
    ({"recipients": "person:{Emma}", "price": "abc"}, "Cost should be a number"),
    ({"recipients": "person:{Emma}", "status": "lost"}, "Unknown status"),
    ({"recipients": "person:{Emma}", "occasion_id": "99999"}, "Unknown occasion"),
])
def test_invalid_form_is_redisplayed_with_values(clients, ids, data, message):
    abel, _ = clients
    data = {k: v.format(**ids) if isinstance(v, str) else v for k, v in data.items()}
    resp = abel.post("/gifts", data={**data, "title": "Keep me"})
    assert resp.status_code == 422
    assert message in resp.text
    assert 'value="Keep me"' in resp.text
    assert gifts_in_db(abel) == []


def test_cannot_attach_someone_elses_unattached_photo(clients, ids):
    abel, jules = clients
    photo = upload(jules)
    resp = abel.post("/gifts", data={"recipients": f"person:{ids['Emma']}", "photo_id": photo})
    assert resp.status_code == 422 and gifts_in_db(abel) == []


def test_share_target_starts_a_gift_with_the_photo(clients):
    abel, _ = clients
    resp = abel.post("/share", files={"photo": ("shared.jpg", jpeg(), "image/jpeg")},
                      headers={"sec-fetch-site": "none"}, follow_redirects=False)
    assert resp.status_code == 303
    photo = resp.headers["location"].removeprefix("/gifts/new?photo=")
    form = abel.get(resp.headers["location"]).text
    assert f'name="photo_id" value="{photo}"' in form
    assert f'src="/photos/{photo}/thumb"' in form


def test_share_target_with_no_image_opens_a_blank_form(clients):
    abel, _ = clients
    resp = abel.post("/share", files={"photo": ("x.txt", b"not an image", "text/plain")},
                      follow_redirects=False)
    assert resp.headers["location"] == "/gifts/new"


def test_new_gift_page_ignores_photos_the_viewer_cannot_see(clients):
    abel, jules = clients
    photo = upload(jules)
    assert f'value="{photo}"' not in abel.get(f"/gifts/new?photo={photo}").text


# --- PWA -------------------------------------------------------------------------

def test_manifest_declares_share_target_and_icons(clients):
    abel, _ = clients
    manifest = abel.get("/manifest.webmanifest").json()
    assert manifest["share_target"]["action"] == "/share"
    assert manifest["share_target"]["enctype"] == "multipart/form-data"
    assert manifest["share_target"]["params"]["files"][0]["name"] == "photo"
    assert {i["sizes"] for i in manifest["icons"]} == {"192x192", "512x512"}
    for icon in manifest["icons"]:
        assert abel.get(icon["src"]).status_code == 200


def test_manifest_link_sends_credentials(clients):
    # Browsers fetch manifests without cookies unless told to; Access would block it.
    abel, _ = clients
    assert 'rel="manifest" href="/manifest.webmanifest" crossorigin="use-credentials"' in abel.get("/").text


def test_service_worker_and_offline_page(clients):
    abel, _ = clients
    sw = abel.get("/sw.js")
    assert sw.headers["content-type"].startswith("text/javascript")
    assert "fetch" in sw.text and "/offline" in sw.text
    assert abel.get("/offline").status_code == 200


# --- CSRF --------------------------------------------------------------------------

@pytest.mark.parametrize("site, allowed", [
    ("same-origin", True), ("none", True), (None, True), ("cross-site", False), ("same-site", False),
])
def test_cross_site_posts_are_blocked(clients, ids, site, allowed):
    abel, _ = clients
    headers = {"sec-fetch-site": site} if site else {}
    resp = abel.post("/gifts", data={"recipients": f"person:{ids['Emma']}"}, headers=headers,
                      follow_redirects=False)
    assert (resp.status_code == 303) == allowed
    assert (resp.status_code == 403) == (not allowed)


def test_cross_site_get_is_still_fine(clients):
    abel, _ = clients
    assert abel.get("/", headers={"sec-fetch-site": "cross-site"}).status_code == 200


# --- orphans -------------------------------------------------------------------------

def test_abandoned_uploads_are_cleaned_up(clients, ids):
    abel, _ = clients
    kept, abandoned = upload(abel), upload(abel)
    abel.post("/gifts", data={"recipients": f"person:{ids['Emma']}", "photo_id": kept})
    store = abel.app.state.photo_store

    with abel.app.state.sessionmaker() as session:
        later = datetime.now(timezone.utc) + timedelta(days=2)
        assert delete_orphan_photos(session, store, older_than=later) == 1
        assert {p.id for p in session.scalars(select(Photo))} == {kept}
    assert not store.path(abandoned, "full").exists()
    assert store.path(kept, "full").exists()


def test_recent_uploads_are_not_cleaned_up(clients):
    abel, _ = clients
    upload(abel)
    with abel.app.state.sessionmaker() as session:
        cutoff = datetime.now(timezone.utc) - timedelta(days=1)
        assert delete_orphan_photos(session, abel.app.state.photo_store, older_than=cutoff) == 0


# --- visibility still holds on the new pages ------------------------------------------

def test_home_page_never_lists_the_viewers_gifts(clients, ids):
    abel, jules = clients
    jules.post("/gifts", data={"recipients": f"person:{ids['Abel']}", "title": "Hidden Thing"})
    jules.post("/gifts", data={"recipients": f"person:{ids['Emma']}", "title": "Shown Thing"})
    home = abel.get("/").text
    assert "Shown Thing" in home and "Hidden Thing" not in home
    # Saved-toast lookups go through the repo too.
    hidden_id = next(g.id for g in gifts_in_db(abel) if g.title == "Hidden Thing")
    assert "Hidden Thing" not in abel.get(f"/?saved={hidden_id}").text


def test_group_hints_are_embedded_for_the_form(clients, ids):
    abel, _ = clients
    html = abel.get("/").text
    hints = json.loads(html_lib.unescape(re.search(r"data-hints='([^']*)'", html).group(1)))
    assert hints[str(ids["Nils"])][0]["group"] == "Nils & Stacy"


# --- cache busting ---------------------------------------------------------------

def test_page_links_scripts_and_styles_by_content_hash(clients):
    # A new page must never run with a cached old script (seen on a phone after a deploy).
    import hashlib
    from app.templating import STATIC
    abel, _ = clients
    html = abel.get("/").text
    for name in ("app.css", "add.js"):
        digest = hashlib.sha256((STATIC / name).read_bytes()).hexdigest()[:12]
        url = f"/static/{name}?v={digest}"
        assert f'"{url}"' in html
        assert abel.get(url).status_code == 200


# --- a new person typed into the form --------------------------------------------

def _person(client, name):
    with client.app.state.sessionmaker() as s:
        return s.scalar(select(Person).where(func.lower(Person.name) == name.lower()))


def test_new_person_is_created_with_the_gift(clients):
    abel, _ = clients
    resp = abel.post("/gifts", data={"recipients": "new", "new_person": "  Dani  ", "price": "20"},
                      follow_redirects=False)
    assert resp.status_code == 303
    dani = _person(abel, "Dani")
    assert dani is not None and dani.name == "Dani" and dani.relation_id is None and dani.active
    assert "Saved gift for Dani" in abel.get(resp.headers["location"]).text
    assert "needs a relation" in abel.get("/settings/people").text


def test_new_person_can_join_other_recipients(clients, ids):
    abel, _ = clients
    resp = abel.post("/gifts", data={"recipients": ["new", f"person:{ids['Emma']}"],
                                      "new_person": "Dani"})
    assert "Saved gift for Dani, Emma" in resp.text


def test_typed_name_matching_someone_picks_them(clients, ids):
    abel, _ = clients
    abel.post("/gifts", data={"recipients": "new", "new_person": "emma", "title": "Kite"})
    with abel.app.state.sessionmaker() as s:
        assert s.scalar(select(func.count()).select_from(Person).where(
            func.lower(Person.name) == "emma")) == 1


def test_typed_name_reactivates_an_inactive_person(clients, ids):
    abel, _ = clients
    with abel.app.state.sessionmaker() as s:
        s.get(Person, ids["Pop"]).active = False
        s.commit()
    abel.post("/gifts", data={"recipients": "new", "new_person": "Pop"})
    assert _person(abel, "Pop").active


def test_cannot_type_yourself_in(clients):
    abel, _ = clients
    resp = abel.post("/gifts", data={"recipients": "new", "new_person": "abel"})
    assert resp.status_code == 422 and "That&#39;s you" in resp.text


def test_new_person_is_not_created_if_the_gift_fails(clients):
    abel, _ = clients
    resp = abel.post("/gifts", data={"recipients": "new", "new_person": "Dani", "price": "abc"})
    assert resp.status_code == 422
    assert _person(abel, "Dani") is None
    assert 'value="Dani"' in resp.text  # kept for another try


def test_new_chip_without_a_name_is_an_error(clients):
    abel, _ = clients
    resp = abel.post("/gifts", data={"recipients": "new", "new_person": " "})
    assert resp.status_code == 422 and "Type the new person" in resp.text
