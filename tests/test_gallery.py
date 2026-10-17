"""Gallery filters, saved filters, and the view/edit/delete routes. Made-up gifts."""

import io
import re
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.gallery import GalleryFilters
from app.main import create_app
from app.models import Gift, Group, Occasion, OccasionType, Person, Relation, SavedFilter
from app.visibility import GiftRepo

# --- filter clauses, against the repo ----------------------------------------------


@pytest.fixture
def stock(session, people, groups):
    """A spread of gifts from Jules's point of view of the data."""
    christmas = session.scalar(select(Occasion).join(OccasionType)
                               .where(OccasionType.name == "Christmas", Occasion.year == 2026))
    birthday = session.scalar(select(Occasion).join(OccasionType)
                              .where(OccasionType.name == "Birthday", Occasion.year == 2026))
    jules = GiftRepo(session, people["Jules"].id)
    make = lambda title, **kw: jules.create(title=title, **kw)
    gifts = {
        "kite": make("Kite", recipient_person_id=people["Evan"].id, occasion_id=christmas.id,
                     source="Toy Barn", cost_cents=1500),
        "puzzle": make("Puzzle", recipient_group_id=groups["Grandkids"].id,
                       occasion_id=christmas.id, status="wrapped", cost_cents=2000),
        "mug": make("Mug", recipient_person_id=people["Nils"].id, occasion_id=christmas.id,
                    status="given", notes="the blue one", cost_cents=1200),
        "candle": make("Candle", recipient_group_id=groups["Nils & Stacy"].id,
                       occasion_id=christmas.id),
        "scarf": make("Scarf", recipient_person_id=people["Emma"].id, occasion_id=birthday.id),
        "untitled": make(None, recipient_person_id=people["Elsa"].id),
        "for_abel": make("Telescope", recipient_person_id=people["Abel"].id,
                          occasion_id=christmas.id, source="Toy Barn"),
    }
    session.commit()
    return {"christmas": christmas.id, "birthday": birthday.id, **gifts}


def titles(session, people, viewer="Abel", **params):
    filters = GalleryFilters.from_params({k: str(v) for k, v in params.items()})
    return sorted(g.title or "-" for g in
                  GiftRepo(session, people[viewer].id).list(*filters.clauses()))


def test_no_filters_shows_all_visible(session, people, stock):
    assert titles(session, people) == ["-", "Candle", "Kite", "Mug", "Puzzle", "Scarf"]


def test_recipient_includes_their_group_gifts(session, people, stock):
    assert titles(session, people, recipient=people["Evan"].id) == ["Kite", "Puzzle"]
    assert titles(session, people, recipient=people["Nils"].id) == ["Candle", "Mug"]


def test_relation_filter(session, people, stock):
    grandchild = session.scalar(select(Relation).where(Relation.name == "Grandchild"))
    assert titles(session, people, relation=grandchild.id) == ["Kite", "Puzzle"]


def test_group_includes_members_individually(session, people, groups, stock):
    assert titles(session, people, group=groups["Nils & Stacy"].id) == ["Candle", "Mug"]


def test_occasion_status_source_filters(session, people, stock):
    assert titles(session, people, occasion=stock["birthday"]) == ["Scarf"]
    assert titles(session, people, status="wrapped") == ["Puzzle"]
    assert "Mug" not in titles(session, people, status="not_given")
    assert titles(session, people, source="Toy Barn") == ["Kite"]


def test_needs_details_and_search(session, people, stock):
    assert titles(session, people, needs="details") == ["-"]
    assert titles(session, people, q="blue") == ["Mug"]
    assert titles(session, people, q="toy barn") == ["Kite"]


def test_filters_combine(session, people, stock):
    assert titles(session, people, occasion=stock["christmas"], status="not_given",
                  recipient=people["Evan"].id) == ["Kite", "Puzzle"]


@pytest.mark.parametrize("params", [
    {"recipient": "Abel"}, {"q": "Telescope"}, {"source": "Toy Barn"},
    {"relation": "Partner"}, {"status": "bought"},
])
def test_no_filter_reaches_a_hidden_gift(session, people, stock, params):
    spouse = session.scalar(select(Relation).where(Relation.name == "Partner")).id
    resolved = {k: {"Abel": people["Abel"].id, "Partner": spouse}.get(v, v)
                for k, v in params.items()}
    assert "Telescope" not in titles(session, people, **resolved)


def test_bad_params_are_ignored():
    filters = GalleryFilters.from_params({"recipient": "abc", "status": "lost", "needs": "x"})
    assert not filters.active


def test_query_string_is_canonical():
    a = GalleryFilters.from_params({"status": "wrapped", "occasion": "3", "q": ""})
    b = GalleryFilters.from_params({"occasion": "3", "status": "wrapped"})
    assert a.query_string() == b.query_string() == "occasion=3&status=wrapped"


# --- HTTP ---------------------------------------------------------------------------

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


def add(client, follow=False, **data):
    resp = client.post("/gifts", data=data, follow_redirects=False)
    assert resp.status_code == 303, resp.text
    return int(parse_qs(urlparse(resp.headers["location"]).query)["saved"][0])


def gift(client, gift_id):
    with client.app.state.sessionmaker() as session:
        stmt = select(Gift).where(Gift.id == gift_id).options(
            selectinload(Gift.photos), selectinload(Gift.recipient_group).selectinload(Group.members))
        return session.scalar(stmt)


def upload(client) -> str:
    out = io.BytesIO()
    Image.new("RGB", (40, 30), (10, 120, 60)).save(out, format="JPEG")
    return client.post("/photos", files={"file": ("p.jpg", out.getvalue(), "image/jpeg")}).json()["id"]


def test_gallery_lists_cards_with_totals(clients, ids):
    abel, _ = clients
    add(abel, recipients=f"person:{ids['Emma']}", title="Kite", price="15")
    add(abel, recipients=f"person:{ids['Elsa']}", price="5.50")
    html = abel.get("/").text
    assert "2 gifts · $20.50" in html
    assert "Kite" in html and "Needs details" in html
    # Both gifts lack an occasion, and everyone but Abel lacks a birthday.
    assert "18 things need attention" in html  # 2 gifts + 16 people


def test_gallery_filters_via_query(clients, ids):
    abel, _ = clients
    add(abel, recipients=f"person:{ids['Emma']}", title="Kite", status="wrapped")
    add(abel, recipients=f"person:{ids['Elsa']}", title="Book")
    html = abel.get("/?status=wrapped").text
    assert "Kite" in html and "Book" not in html and "1 gift" in html


def test_gallery_hides_viewers_gifts_and_totals(clients, ids):
    abel, jules = clients
    add(jules, recipients=f"person:{ids['Abel']}", title="Hidden Thing", price="99")
    add(jules, recipients=f"person:{ids['Emma']}", title="Shown Thing", price="1")
    for url in ["/", "/?q=Hidden", f"/?recipient={ids['Abel']}", "/?needs=details"]:
        html = abel.get(url).text
        assert "Hidden Thing" not in html and "$99" not in html and "$100" not in html


def test_recipient_filter_does_not_offer_the_viewer(clients):
    abel, _ = clients
    html = abel.get("/").text
    select_html = re.search(r'<select name="recipient">.*?</select>', html, re.S).group(0)
    assert ">Abel<" not in select_html and ">Jules<" in select_html


def test_starter_quick_filters(clients):
    abel, _ = clients
    html = abel.get("/").text
    assert 'href="/?status=not_given"' in html and "Not given yet" in html


def test_save_and_remove_a_quick_filter(clients, ids):
    abel, jules = clients
    resp = abel.post("/filters", data={"name": "Grandkid stuff",
                                        "query": f"status=wrapped&group={ids['group:Grandkids']}"},
                      follow_redirects=False)
    assert resp.status_code == 303
    canonical = f"group={ids['group:Grandkids']}&status=wrapped"
    assert resp.headers["location"] == f"/?{canonical}"
    escaped = canonical.replace("&", "&amp;")  # as it appears inside an HTML attribute
    assert f'href="/?{escaped}"' in jules.get("/").text  # shared with the other user

    with abel.app.state.sessionmaker() as session:
        sf = session.scalar(select(SavedFilter).where(SavedFilter.name == "Grandkid stuff"))
    abel.post(f"/filters/{sf.id}/delete")
    assert "Grandkid stuff" not in abel.get("/").text


@pytest.mark.parametrize("data", [{"name": "", "query": "status=wrapped"},
                                  {"name": "Nothing", "query": "bogus=1"},
                                  {"name": "Not given yet", "query": "status=idea"}])
def test_bad_saved_filters_are_rejected(clients, data):
    abel, _ = clients
    assert abel.post("/filters", data=data).status_code == 422


# --- detail / edit / delete ------------------------------------------------------------

def test_detail_page_shows_full_photo_and_values(clients, ids):
    abel, _ = clients
    photo = upload(abel)
    gid = add(abel, recipients=f"person:{ids['Emma']}", title="Kite", price="15",
              photo_id=photo, notes="red one")
    html = abel.get(f"/gifts/{gid}").text
    assert f'src="/photos/{photo}/full"' in html
    assert 'value="Kite"' in html and 'value="15"' in html and "red one" in html
    assert re.search(rf'value="person:{ids["Emma"]}"\s+data-kind="person"[^>]*checked', html)


def test_edit_updates_fields(clients, ids):
    abel, _ = clients
    gid = add(abel, recipients=f"person:{ids['Emma']}")
    resp = abel.post(f"/gifts/{gid}", data={
        "recipients": f"group:{ids['group:Girls']}", "title": "Drawing kit", "price": "22",
        "status": "wrapped", "occasion_id": ids["occ:Christmas 2026"], "source": "Market",
        "notes": "n"}, follow_redirects=False)
    assert resp.headers["location"] == f"/?saved={gid}&edited=1"
    g = gift(abel, gid)
    assert (g.title, g.cost_cents, g.status, g.source, g.notes) == ("Drawing kit", 2200, "wrapped", "Market", "n")
    assert (g.recipient_person_id, g.recipient_group_id) == (None, ids["group:Girls"])


def test_edit_group_gift_to_one_person_clears_group(clients, ids):
    abel, _ = clients
    gid = add(abel, recipients=f"group:{ids['group:Girls']}")
    abel.post(f"/gifts/{gid}", data={"recipients": f"person:{ids['Cal']}"})
    g = gift(abel, gid)
    assert (g.recipient_person_id, g.recipient_group_id) == (ids["Cal"], None)


def test_ad_hoc_group_gift_edits_as_its_members(clients, ids):
    abel, _ = clients
    gid = add(abel, recipients=[f"person:{ids['Emma']}", f"person:{ids['Elsa']}"])
    html = abel.get(f"/gifts/{gid}").text
    for name in ("Emma", "Elsa"):
        assert re.search(rf'value="person:{ids[name]}"\s+data-kind="person"[^>]*checked', html)


def test_add_and_remove_photos(clients, ids):
    abel, _ = clients
    first, second = upload(abel), upload(abel)
    gid = add(abel, recipients=f"person:{ids['Emma']}", photo_id=first)
    abel.post(f"/gifts/{gid}", data={"recipients": f"person:{ids['Emma']}", "photo_id": second})
    assert [p.id for p in gift(abel, gid).photos] == [first, second]
    abel.post(f"/gifts/{gid}", data={"recipients": f"person:{ids['Emma']}",
                                      "remove_photo": first})
    assert [p.id for p in gift(abel, gid).photos] == [second]


def test_invalid_edit_is_redisplayed_and_not_saved(clients, ids):
    abel, _ = clients
    gid = add(abel, recipients=f"person:{ids['Emma']}", title="Kite")
    resp = abel.post(f"/gifts/{gid}", data={"recipients": f"person:{ids['Emma']}",
                                             "title": "Changed", "price": "abc"})
    assert resp.status_code == 422 and "Cost should be a number" in resp.text
    assert 'value="Changed"' in resp.text
    assert gift(abel, gid).title == "Kite"


def test_back_link_returns_to_filtered_gallery(clients, ids):
    abel, _ = clients
    gid = add(abel, recipients=f"person:{ids['Emma']}", status="wrapped")
    list_html = abel.get("/?status=wrapped").text
    assert f'href="/gifts/{gid}?back=status%3Dwrapped"' in list_html
    detail = abel.get(f"/gifts/{gid}?back=status%3Dwrapped").text
    assert 'href="/?status=wrapped"' in detail
    resp = abel.post(f"/gifts/{gid}", data={"recipients": f"person:{ids['Emma']}",
                                             "back": "status=wrapped"}, follow_redirects=False)
    assert resp.headers["location"] == f"/?status=wrapped&saved={gid}&edited=1"


@pytest.mark.parametrize("back", ["//evil.example", "https://evil.example", "/admin"])
def test_back_cannot_redirect_elsewhere(clients, ids, back):
    abel, _ = clients
    gid = add(abel, recipients=f"person:{ids['Emma']}")
    resp = abel.post(f"/gifts/{gid}", data={"recipients": f"person:{ids['Emma']}", "back": back},
                      follow_redirects=False)
    assert resp.headers["location"] == f"/?saved={gid}&edited=1"


def test_saving_an_edit_shows_the_banner_on_the_list(clients, ids):
    abel, _ = clients
    gid = add(abel, recipients=f"person:{ids['Emma']}", status="wrapped")
    html = abel.post(f"/gifts/{gid}", data={"recipients": f"person:{ids['Emma']}", "title": "Kite",
                                             "status": "wrapped", "back": "status=wrapped"}).text
    assert "Saved Kite for Emma." in html and "Add another" not in html
    assert 'selected>Wrapped<' in html  # still filtered as before


def test_filters_list_people_first_then_the_gift(clients):
    abel, _ = clients
    html = abel.get("/").text
    order = [html.index(f"<label>{name}") for name in
             ("For", "Group", "Relation", "Occasion", "Status", "Source")]
    assert order == sorted(order)


def test_delete(clients, ids):
    abel, _ = clients
    gid = add(abel, recipients=f"person:{ids['Emma']}", title="Kite")
    resp = abel.post(f"/gifts/{gid}/delete", follow_redirects=False)
    assert resp.headers["location"] == "/?deleted=1"
    assert gift(abel, gid) is None
    assert abel.get(f"/gifts/{gid}").status_code == 404


def test_delete_returns_to_the_list_it_came_from(clients, ids):
    abel, _ = clients
    gid = add(abel, recipients=f"person:{ids['Emma']}", title="Kite", status="wrapped")
    detail = abel.get(f"/gifts/{gid}?back=status%3Dwrapped").text
    assert 'name="back" value="status=wrapped"' in detail
    resp = abel.post(f"/gifts/{gid}/delete", data={"back": "status=wrapped"},
                     follow_redirects=False)
    assert resp.headers["location"] == "/?status=wrapped&deleted=1"
    assert "Gift deleted." in abel.get(resp.headers["location"]).text

    gid = add(abel, recipients=f"person:{ids['Emma']}")
    resp = abel.post(f"/gifts/{gid}/delete", data={"back": "attention"}, follow_redirects=False)
    assert resp.headers["location"] == "/attention?deleted=1"
    assert "Gift deleted." in abel.get(resp.headers["location"]).text
    gid = add(abel, recipients=f"person:{ids['Emma']}")
    resp = abel.post(f"/gifts/{gid}/delete",
                     data={"back": "https://evil.example"}, follow_redirects=False)
    assert resp.headers["location"] == "/?deleted=1"


def test_hidden_gift_cannot_be_viewed_edited_or_deleted(clients, ids):
    abel, jules = clients
    gid = add(jules, recipients=f"person:{ids['Abel']}", title="Hidden Thing")
    assert abel.get(f"/gifts/{gid}").status_code == 404
    assert abel.post(f"/gifts/{gid}", data={"recipients": f"person:{ids['Emma']}",
                                             "title": "Hijacked"}).status_code == 404
    assert abel.post(f"/gifts/{gid}/delete").status_code == 404
    g = gift(jules, gid)
    assert (g.title, g.recipient_person_id) == ("Hidden Thing", ids["Abel"])


def test_readdressing_a_gift_to_yourself_leaves_the_page(clients, ids):
    abel, _ = clients
    gid = add(abel, recipients=f"person:{ids['Emma']}")
    resp = abel.post(f"/gifts/{gid}", data={"recipients": f"person:{ids['Abel']}"},
                      follow_redirects=False)
    assert resp.headers["location"] == "/"


def test_cannot_attach_a_photo_from_a_hidden_gift(clients, ids):
    abel, jules = clients
    photo = upload(jules)
    add(jules, recipients=f"person:{ids['Abel']}", photo_id=photo)
    gid = add(abel, recipients=f"person:{ids['Emma']}")
    resp = abel.post(f"/gifts/{gid}", data={"recipients": f"person:{ids['Emma']}",
                                             "photo_id": photo})
    assert resp.status_code == 422 and gift(abel, gid).photos == []


# --- needs attention ------------------------------------------------------------------

def test_attention_lists_gifts_and_people(clients, ids):
    from app.models import BudgetRule
    abel, _ = clients
    christmas = ids["occ:Christmas 2026"]
    add(abel, recipients=f"person:{ids['Emma']}", title="Kite", price="5", occasion_id=christmas)  # fine
    add(abel, recipients=f"person:{ids['Elsa']}", title="Paints", occasion_id=christmas)  # no cost
    add(abel, recipients=f"person:{ids['Elsa']}", title="Someday", status="idea")  # vague idea: fine
    add(abel, recipients=f"person:{ids['Elsa']}", status="idea")  # idea with no title
    abel.post("/settings/people", data={"name": "Dani", "active": "1"})  # no relation
    abel.post("/settings/people", data={"name": "Rosa", "active": "1", "relation_id": "new",
                                        "new_relation": "Cousin", "birthday_month": "3",
                                        "birthday_day": "4"})
    page = abel.get("/attention").text.split("<dialog")[0]  # not the add form's chips
    assert "Paints" in page and "Needs cost" in page
    assert "Kite" not in page and "Someday" not in page
    assert page.count("Needs title") == 1
    assert "Dani" in page and f'href="/settings/people#person-' in page
    # Nobody in the made-up family has a birthday yet, budget or not.
    for name in ("Astrid", "Emma", "Nell"):
        assert name in page
    assert "Needs birthday" in page
    assert "Rosa" not in page  # has both


def test_attention_never_shows_your_own_gifts(clients, ids):
    abel, jules = clients
    add(jules, recipients=f"person:{ids['Abel']}")  # untitled, for Abel
    page = abel.get("/attention").text
    assert "Untitled gift" not in page
    assert "Untitled gift" in jules.get("/attention").text


def test_fixing_from_attention_returns_there(clients, ids):
    abel, _ = clients
    gid = add(abel, recipients=f"person:{ids['Emma']}")
    assert 'href="/attention"' in abel.get(f"/gifts/{gid}?back=attention").text
    resp = abel.post(f"/gifts/{gid}", data={"recipients": f"person:{ids['Emma']}", "title": "Kite",
                                             "back": "attention"}, follow_redirects=False)
    assert resp.headers["location"] == f"/attention?saved={gid}"
