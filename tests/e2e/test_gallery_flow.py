"""Browser tests of the gallery, filters and the gift page. Made-up gifts."""

import re
from urllib.parse import urlencode

import pytest

from tests.e2e.conftest import chip, expect, take_photo
from tests.e2e.test_add_flow import assert_no_sideways_scroll


@pytest.fixture
def ids(server):
    from sqlalchemy import select

    from app.models import Group, Person
    app, _ = server
    with app.state.sessionmaker() as session:
        return {**{p.name: p.id for p in session.scalars(select(Person))},
                **{g.name: g.id for g in session.scalars(select(Group))}}


def add(page, **data) -> int:
    # urlencode ourselves: Playwright's form= can't repeat a field (several recipients).
    resp = page.request.post("/gifts", data=urlencode(data, doseq=True), max_redirects=0,
                             headers={"content-type": "application/x-www-form-urlencoded"})
    return int(re.search(r"saved=(\d+)", resp.headers["location"]).group(1))


def card(page, text):
    return page.locator(".card", has_text=text)


def test_tap_card_edit_title_and_come_back(page, ids):
    add(page, recipients=f"person:{ids['Emma']}", price="15")
    page.goto("/")
    card(page, "Needs details").tap()

    # The title is typed at the top, above the photo, where the heading would be.
    title = page.locator("#edit-form .form-head input[name=title]")
    expect(title).to_have_attribute("placeholder", "Untitled gift")
    assert title.bounding_box()["y"] < page.locator("#edit-form .photo-slot").bounding_box()["y"]
    expect(page.locator('#edit-form input[name="title"]')).to_have_count(1)
    title.fill("Paint set")
    expect(page.locator(".fab")).to_have_count(0)  # editing, not adding
    page.locator("#edit-form [data-save]").tap()

    # Saving goes back to the list, at the top, with a banner.
    expect(page).to_have_url(re.compile(r"/\?saved=\d+&edited=1$"))
    expect(page.locator(".toast")).to_contain_text("Saved Paint set for Emma.")
    expect(card(page, "Paint set")).to_contain_text("Emma · $15")


def test_filter_dropdown_applies_immediately(page, ids):
    add(page, recipients=f"person:{ids['Emma']}", title="Kite", status="wrapped")
    add(page, recipients=f"person:{ids['Elsa']}", title="Book")
    page.goto("/")
    page.locator(".filters summary").tap()
    page.locator('.filter-form select[name="status"]').select_option("wrapped")
    expect(page).to_have_url(re.compile(r"status=wrapped"))
    expect(page.locator(".cards .card")).to_have_count(1)
    expect(card(page, "Kite")).to_be_visible()


def test_save_a_quick_filter(page, ids):
    add(page, recipients=f"person:{ids['Evan']}", title="Kite")
    page.goto(f"/?group={ids['Grandkids']}")
    page.locator('.save-filter input[name="name"]').fill("Grandkids")
    page.locator(".save-filter button").tap()
    chip_link = page.locator(".qf", has_text="Grandkids")
    expect(chip_link).to_have_class(re.compile(r"\bon\b"))
    page.goto("/")
    chip_link.tap()
    expect(card(page, "Kite")).to_be_visible()


def test_needs_attention_empties_as_gifts_are_fixed(page, ids):
    add(page, recipients=f"person:{ids['Pop']}")  # no title, occasion or cost
    page.goto("/")
    banner = page.locator(".attention-banner")
    expect(banner).to_contain_text("17 things need attention")  # + 16 birthdays
    banner.tap()
    issue = page.locator(".issue", has_text="Untitled gift")
    expect(issue).to_contain_text("Needs title, occasion, cost")
    issue.tap()

    expect(page.locator("a.back")).to_have_text("← Needs attention")
    page.locator('#edit-form input[name="title"]').fill("Socks")
    page.locator("#edit-form [data-occasion]").select_option(label="Christmas 2026")
    page.locator("#edit-form [data-price]").fill("8")
    page.locator("#edit-form [data-save]").tap()

    expect(page).to_have_url(re.compile(r"/attention\?saved=\d+$"))  # back to the list
    expect(page.locator(".toast")).to_have_text("Saved Socks.")
    expect(page.locator(".issue", has_text="Socks")).to_have_count(0)
    expect(page.locator(".issue", has_text="Untitled")).to_have_count(0)
    page.goto("/")
    expect(page.locator(".attention-banner")).to_contain_text("16 things need attention")


def test_delete_asks_first(page, ids):
    gift_id = add(page, recipients=f"person:{ids['Emma']}", title="Kite")
    page.goto(f"/gifts/{gift_id}")

    page.once("dialog", lambda d: d.dismiss())
    page.locator(".delete-form button").tap()
    expect(page).to_have_url(re.compile(rf"/gifts/{gift_id}$"))

    page.once("dialog", lambda d: d.accept())
    page.locator(".delete-form button").tap()
    expect(page.locator(".toast")).to_have_text("Gift deleted.")
    expect(card(page, "Kite")).to_have_count(0)


def test_add_a_second_photo_on_the_gift_page(page, ids):
    gift_id = add(page, recipients=f"person:{ids['Emma']}", title="Kite")
    page.goto(f"/gifts/{gift_id}")
    take_photo(page, via="label[for=edit-camera]")
    expect(page.locator("#edit-form [data-photo-id]")).not_to_have_value("")
    page.locator("#edit-form [data-save]").tap()
    expect(page.locator(".toast")).to_contain_text("Saved Kite")  # back on the list
    page.goto(f"/gifts/{gift_id}")
    expect(page.locator(".existing-photos img")).to_have_count(1)

    take_photo(page, via="label[for=edit-camera]")
    expect(page.locator("#edit-form [data-photo-id]")).not_to_have_value("")
    page.locator("#edit-form [data-save]").tap()
    expect(page.locator(".toast")).to_contain_text("Saved Kite")
    page.goto(f"/gifts/{gift_id}")
    expect(page.locator(".existing-photos img")).to_have_count(2)


def test_gift_page_has_no_plus_button(page, ids):
    # It's for editing: no + floating over Save, and no add form on the page.
    gift_id = add(page, recipients=f"person:{ids['Emma']}", title="Kite")
    page.goto(f"/gifts/{gift_id}")
    expect(page.locator(".fab")).to_have_count(0)
    expect(page.locator("#add-dialog")).to_have_count(0)
    take_photo(page, via="label[for=edit-camera]")
    expect(page.locator("#edit-form [data-photo-id]")).not_to_have_value("")


def test_ad_hoc_group_gift_opens_with_both_people_picked(page, ids):
    gift_id = add(page, recipients=[f"person:{ids['Emma']}", f"person:{ids['Elsa']}"])
    page.goto(f"/gifts/{gift_id}")
    expect(page.locator('#edit-form input[name="recipients"]:checked')).to_have_count(2)


def test_no_sideways_scroll_on_gallery_and_gift_page(page, ids):
    for i in range(4):
        page.request.post("/filters", form={"name": f"A long quick filter name {i}",
                                            "query": f"status={['idea','bought','wrapped','given'][i]}"})
    gift_id = add(page, recipients=f"person:{ids['Emma']}", title="A" * 80)
    width = page.viewport_size["width"]

    page.goto("/")
    page.locator(".filters summary").tap()
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    page.goto(f"/gifts/{gift_id}")
    assert page.evaluate("document.documentElement.scrollWidth") <= width


def test_quick_filter_x_removes_only_that_filter_after_asking(page, ids):
    page.goto(f"/?group={ids['Grandkids']}")
    page.locator('.save-filter input[name="name"]').fill("Grandkids")
    page.locator(".save-filter button").tap()
    pill = page.locator(".qf-saved", has_text="Grandkids")
    expect(pill).to_have_count(1)
    page.goto("/")

    page.once("dialog", lambda d: d.dismiss())
    pill.locator(".qf-x").tap()
    expect(pill).to_have_count(1)

    page.once("dialog", lambda d: d.accept())
    pill.locator(".qf-x").tap()
    expect(pill).to_have_count(0)
    expect(page.locator(".qf-saved", has_text="Not given yet")).to_have_count(1)


def test_source_chips_suggest_recent_sources(page, ids):
    for source in ["Target", "Etsy", "Costco", "Craft fair", "Home"]:
        add(page, recipients=f"person:{ids['Emma']}", title="x", source=source)
    page.goto("/gifts/new")
    box = page.locator("main [data-source]")
    shown = page.locator("main .source-chip:visible")

    # Pre-filled from the last gift, with the 4 most recent one tap away.
    expect(box).to_have_value("Home")
    expect(shown).to_have_text(["Home", "Craft fair", "Costco", "Etsy"])
    expect(page.locator("main .source-chip.on")).to_have_text("Home")

    # Typing filters (any part of the name, any case), reaching older ones too.
    box.fill("t")
    expect(shown).to_have_text(["Craft fair", "Costco", "Etsy", "Target"])
    box.fill("tar")
    expect(shown).to_have_text(["Target"])
    box.fill("Somewhere new")
    expect(page.locator("main [data-source-chips]")).to_be_hidden()

    # Tapping a chip fills the box and shows the recent ones again.
    box.fill("cost")
    shown.first.tap()
    expect(box).to_have_value("Costco")
    expect(shown).to_have_count(4)
    expect(page.locator("main datalist")).to_have_count(0)


def test_filters_on_a_phone_people_left_gift_right(page):
    page.goto("/")
    page.locator(".filters summary").tap()
    box = lambda name: page.locator(f".filter-grid select[name={name}]").bounding_box()
    left = [box(n) for n in ("recipient", "group", "relation")]
    right = [box(n) for n in ("occasion", "status", "source")]
    assert len({round(b["x"]) for b in left}) == 1 and len({round(b["x"]) for b in right}) == 1
    assert left[0]["x"] < right[0]["x"]
    assert [b["y"] for b in left] == sorted(b["y"] for b in left)
    assert [round(b["y"]) for b in left] == [round(b["y"]) for b in right]


def test_two_columns_even_on_a_zoomed_in_phone(page, ids):
    # Larger display zoom on an S24+ leaves ~300 CSS px; that still gets two columns.
    for title in ("Kite", "Socks", "Book"):
        add(page, recipients=f"person:{ids['Emma']}", title=title)
    page.set_viewport_size({"width": 300, "height": 700})
    page.goto("/")
    first, second = (page.locator(".cards .card").nth(i).bounding_box() for i in (0, 1))
    assert round(first["y"]) == round(second["y"]) and second["x"] > first["x"]
    assert_no_sideways_scroll(page)


def test_ordered_shows_the_link_field_and_on_the_way(page, ids):
    gid = add(page, recipients=f"person:{ids['Emma']}", title="Kite", price="20")
    page.goto(f"/gifts/{gid}")
    form = page.locator("main form[action='/gifts/%d']" % gid)
    link = form.locator("[data-order-link]")
    expect(link).to_be_hidden()
    form.locator("[data-status]").select_option("ordered")
    expect(link).to_be_visible()
    expect(link.locator("input")).to_be_focused()
    page.keyboard.type("shop.example/orders/7")
    form.locator("[data-save]").tap()  # no https:// typed: the browser mustn't block it

    expect(page.locator(".qf", has_text="On the way (1)")).to_be_visible()
    expect(card(page, "Kite").locator(".status")).to_have_text("Ordered")
    page.locator(".qf", has_text="On the way").tap()
    expect(page).to_have_url(re.compile(r"status=ordered"))
    card(page, "Kite").tap()
    opener = page.get_by_role("link", name="Open ↗")
    expect(opener).to_have_attribute("href", "https://shop.example/orders/7")
    assert_no_sideways_scroll(page)


def test_for_chips_stay_folded_with_a_less_used_recipient(page, ids):
    for name in ("Jules", "Astrid", "Elsa", "Emma", "Shay", "Cal"):  # the top 6, used twice
        add(page, recipients=f"person:{ids[name]}")
        add(page, recipients=f"person:{ids[name]}")
    gift_id = add(page, recipients=f"person:{ids['Sage']}", title="Yo-yo")
    page.goto(f"/gifts/{gift_id}")
    expect(page.locator("#edit-form .rest-chip.picked")).to_have_count(1)  # Sage isn't top 6
    expect(chip(page, "Sage", "#edit-form")).to_be_visible()
    expect(chip(page, "Pop", "#edit-form")).to_be_hidden()  # the rest stay behind "…"
    page.locator("#edit-form [data-more-chips]").tap()
    expect(chip(page, "Pop", "#edit-form")).to_be_visible()

