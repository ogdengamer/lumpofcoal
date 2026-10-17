"""Ordered online: the status, the order link, and the On the way quick filter."""

import re

import pytest
from alembic import command
from sqlalchemy import text

from app.db import alembic_config, make_engine
from app.routes.gifts import parse_order_url
from app.gifts import FormError
from tests.test_budget import board, christmas, give, row  # noqa: F401  (fixtures)
from tests.test_gallery import add, clients, gift, ids  # noqa: F401  (fixtures)


def link_field_shown(page, fid="edit"):
    match = re.search(r'data-order-link (hidden)?>\s*<label for="' + fid + '-order-url"', page)
    return match.group(1) is None


@pytest.mark.parametrize("pasted, stored", [
    ("", None),
    ("https://shop.example/orders/123", "https://shop.example/orders/123"),
    ("shop.example/track?id=9", "https://shop.example/track?id=9"),
    ("http://tracking.example/x", "http://tracking.example/x"),
])
def test_order_links(pasted, stored):
    assert parse_order_url(pasted) == stored


@pytest.mark.parametrize("pasted", ["javascript:alert(1)", "not a link", "ftp://files.example/x",
                                    "https://" + "a" * 500 + ".example"])
def test_bad_order_links_are_refused(pasted):
    with pytest.raises(FormError):
        parse_order_url(pasted)


def test_ordered_gift_with_its_link(clients, ids):
    abel, _ = clients
    gid = add(abel, recipients=f"person:{ids['Emma']}", title="Kite", status="ordered",
              order_url="shop.example/orders/42")
    assert gift(abel, gid).order_url == "https://shop.example/orders/42"
    page = abel.get(f"/gifts/{gid}").text
    assert 'href="https://shop.example/orders/42" target="_blank" rel="noopener noreferrer"' in page
    assert link_field_shown(page)
    assert '<option value="ordered" selected>Ordered</option>' in page
    resp = abel.post(f"/gifts/{gid}", data={"recipients": f"person:{ids['Emma']}",
                                            "status": "ordered", "order_url": "nope nope"})
    assert resp.status_code == 422 and "doesn&#39;t look like a web address" in resp.text


def test_labels_and_the_link_field_stays_out_of_the_way(clients, ids):
    abel, _ = clients
    gid = add(abel, recipients=f"person:{ids['Emma']}", title="Scarf")  # the default: Have it
    page = abel.get(f"/gifts/{gid}").text
    assert '<option value="bought" selected>Have it</option>' in page
    assert not link_field_shown(page)
    assert not link_field_shown(abel.get("/").text, "add")  # the add dialog, too
    assert "Open ↗" not in page


def test_on_the_way_quick_filter(clients, ids):
    abel, jules = clients
    assert "On the way" not in abel.get("/").text
    add(abel, recipients=f"person:{ids['Emma']}", title="Kite", status="ordered")
    add(jules, recipients=f"person:{ids['Abel']}", title="Telescope", status="ordered")
    gallery = abel.get("/").text
    assert 'href="/?status=ordered"' in gallery and "On the way (1)" in gallery  # not his own
    filtered = abel.get("/?status=ordered").text
    assert "Kite" in filtered and "Telescope" not in filtered
    assert "On the way (2)" in jules.get("/").text


def test_ordered_counts_as_spent(session, people, christmas):
    give(session, people, recipient_person_id=people["Emma"].id, occasion_id=christmas.id,
         cost_cents=5000, status="ordered")
    assert row(board(session, people, christmas), "Emma").spent_cents == 5000


def test_upgrade_keeps_gifts_and_allows_ordered(tmp_path):
    engine = make_engine(str(tmp_path))
    cfg = alembic_config(engine)
    command.upgrade(cfg, "0007")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO people (id, name, active) VALUES (1, 'Pat', 1)"))
        conn.execute(text("INSERT INTO gifts (title, recipient_person_id, status, created_by_id, "
                          "created_at, updated_at) VALUES ('Mug', 1, 'bought', 1, "
                          "'2026-10-01', '2026-10-01')"))
    command.upgrade(cfg, "head")
    with engine.begin() as conn:
        assert conn.execute(text("SELECT title, status, order_url FROM gifts")).one() == \
            ("Mug", "bought", None)
        conn.execute(text("UPDATE gifts SET status = 'ordered'"))
        with pytest.raises(Exception, match="CHECK constraint"):
            conn.execute(text("UPDATE gifts SET status = 'lost'"))
