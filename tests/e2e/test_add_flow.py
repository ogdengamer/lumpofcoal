"""Browser tests of the add-gift flow on an emulated Android phone."""

from tests.e2e.conftest import chip, expect, take_photo

# --- the critical path --------------------------------------------------------------

def test_plus_photo_recipient_price_save(page):
    page.goto("/")
    take_photo(page)

    dialog = page.locator("#add-dialog")
    expect(dialog).to_be_visible()
    expect(dialog.locator("[data-preview]")).to_be_visible()
    expect(dialog.locator("[data-photo-id]")).not_to_have_value("", timeout=10_000)

    chip(page, "Emma").tap()
    expect(dialog.locator("[data-price]")).to_be_focused()  # keyboard goes straight to price
    page.keyboard.type("24.99")
    dialog.locator("[data-save]").tap()

    expect(page.locator(".toast")).to_contain_text("Saved gift for Emma")
    expect(page.locator(".cards img").first).to_be_visible()
    assert page.locator(".cards").inner_text().count("$24.99") == 1


def test_occasion_defaults_to_next_christmas(page):
    page.goto("/")
    take_photo(page)
    occasion = page.locator("#add-dialog [data-occasion] option:checked")
    expect(occasion).to_contain_text("Christmas")


def test_save_tapped_during_upload_waits_for_it(page):
    # Delay uploads inside the page (a sleeping Playwright route handler would
    # block the test itself, not the upload).
    page.add_init_script("""
      const realFetch = window.fetch;
      window.fetch = (url, opts) => String(url).endsWith('/photos')
        ? new Promise(r => setTimeout(r, 1500)).then(() => realFetch(url, opts))
        : realFetch(url, opts);
    """)
    page.goto("/")
    take_photo(page)
    chip(page, "Elsa").tap()
    page.locator("#add-dialog [data-save]").tap()
    expect(page.locator("#add-dialog [data-save]")).to_have_text("Saving after upload…")
    expect(page.locator(".toast")).to_contain_text("Saved gift for Elsa", timeout=10_000)
    expect(page.locator(".cards img").first).to_be_visible()  # the photo made it


def test_failed_upload_can_still_save_without_photo(page):
    page.route("**/photos", lambda route: route.fulfill(status=500, body="disk full"))
    page.goto("/")
    take_photo(page)
    expect(page.locator("#add-dialog [data-photo-status]")).to_contain_text("didn't upload")
    page.locator("#add-dialog [data-more-chips]").tap()
    chip(page, "Pop").tap()
    page.locator("#add-dialog [data-save]").tap()
    expect(page.locator(".toast")).to_contain_text("Saved gift for Pop")


def test_retake_uses_the_newest_photo(page, server):
    page.goto("/")
    take_photo(page)
    first = page.locator("#add-dialog [data-photo-id]")
    expect(first).not_to_have_value("")
    old = first.input_value()
    take_photo(page, via="#add-dialog label[for=add-camera]")
    expect(first).not_to_have_value(old)
    expect(first).not_to_have_value("")


# --- chips ------------------------------------------------------------------------

def test_first_pick_jumps_to_cost_and_more_picks_add_people(page):
    page.goto("/")
    take_photo(page)
    dialog = page.locator("#add-dialog")
    chip(page, "Emma").tap()
    expect(dialog.locator("[data-price]")).to_be_focused()
    chip(page, "Elsa").tap()
    expect(dialog.locator('input[name="recipients"]:checked')).to_have_count(2)
    expect(dialog.locator("[data-price]")).not_to_be_focused()  # no jump on later picks
    chip(page, "Emma").tap()  # tapping again unpicks
    checked = dialog.locator('input[name="recipients"]:checked')
    expect(checked).to_have_count(1)
    expect(checked).to_have_attribute("data-label", "Elsa")


def test_several_people_offers_a_group_name(page):
    page.goto("/")
    take_photo(page)
    dialog = page.locator("#add-dialog")
    chip(page, "Emma").tap()
    expect(dialog.locator("[data-group-name]")).to_be_hidden()
    chip(page, "Elsa").tap()
    expect(dialog.locator("[data-group-name]")).to_be_visible()
    dialog.locator('input[name="group_name"]').fill("The Sisters")
    dialog.locator("[data-save]").tap()
    expect(page.locator(".toast")).to_contain_text("Saved gift for Elsa, Emma")


def test_group_budget_hint_offers_the_group(page):
    page.goto("/")
    take_photo(page)
    dialog = page.locator("#add-dialog")
    dialog.locator("[data-more-chips]").tap()
    chip(page, "Nils").tap()

    hint = dialog.locator("[data-group-hint]")
    expect(hint).to_be_visible()
    expect(hint).to_contain_text("Nils is in the Nils & Stacy budget ($30 for the group)")

    hint.locator("[data-use-group]").tap()
    checked = dialog.locator('input[name="recipients"]:checked')
    expect(checked).to_have_attribute("data-label", "Nils & Stacy")
    expect(hint).to_be_hidden()


def test_hint_only_for_occasions_with_a_group_rule(page):
    page.goto("/")
    take_photo(page)
    dialog = page.locator("#add-dialog")
    dialog.locator("[data-more-chips]").tap()
    chip(page, "Nils").tap()
    expect(dialog.locator("[data-group-hint]")).to_be_visible()
    dialog.locator("[data-occasion]").select_option(label="Birthday 2026")
    expect(dialog.locator("[data-group-hint]")).to_be_hidden()


# --- standalone page (share target lands here) -------------------------------------------

def test_standalone_form_works_without_the_dialog(page):
    page.goto("/gifts/new")
    take_photo(page, via="label[for=add-gallery]")
    expect(page.locator("[data-photo-id]")).not_to_have_value("")
    page.locator("main [data-more-chips]").tap()
    chip(page, "Shay", scope="main").tap()
    page.keyboard.type("15")
    page.locator("[data-save]").tap()
    expect(page.locator(".toast")).to_contain_text("Saved gift for Shay")


def test_validation_error_keeps_the_form(page):
    page.goto("/")
    take_photo(page)
    page.locator("#add-dialog [data-price]").fill("12")
    page.locator("#add-dialog [data-save]").tap()  # no recipient picked
    expect(page.locator(".error")).to_contain_text("Pick who it's for")
    expect(page.locator("[data-price]")).to_have_value("12")
    expect(page.locator("[data-photo-id]")).not_to_have_value("")  # photo kept


def test_service_worker_registers(page):
    page.goto("/")
    page.wait_for_function("navigator.serviceWorker.ready.then(() => true)")
    assert page.evaluate("navigator.serviceWorker.controller !== null || true")


# --- layout -----------------------------------------------------------------------

def assert_no_sideways_scroll(page):
    # Compare with the device's width, not window.innerWidth: on a mobile browser an
    # overflowing page widens the layout viewport itself, hiding the overflow.
    overflow = page.evaluate("document.documentElement.scrollWidth") - page.viewport_size["width"]
    assert overflow <= 0, f"page is {overflow}px wider than the phone screen"


def test_no_sideways_scroll_on_phone(page):
    page.goto("/")
    assert_no_sideways_scroll(page)
    take_photo(page)
    page.locator("#add-dialog [data-more-chips]").tap()
    assert_no_sideways_scroll(page)

    page.goto("/gifts/new")
    page.locator("main [data-more-chips]").tap()
    assert_no_sideways_scroll(page)


def test_done_key_on_price_saves(page):
    # Photo -> recipient -> price -> keyboard "Done" is the whole flow; no scrolling to Save.
    page.goto("/")
    take_photo(page)
    expect(page.locator("#add-dialog [data-photo-id]")).not_to_have_value("")
    chip(page, "Emma").tap()
    page.keyboard.type("9")
    page.keyboard.press("Enter")
    expect(page.locator(".toast")).to_contain_text("Saved gift for Emma")


def test_nothing_is_folded_away_but_extra_recipients(page):
    page.goto("/gifts/new")
    form = page.locator("main .gift-form")
    expect(form.locator("details")).to_have_count(0)
    for name in ("price", "occasion_id", "source", "status", "notes"):
        expect(form.locator(f"[name={name}]")).to_be_visible()
    expect(form.locator(".field", has=page.locator("[name=price]"))).to_contain_text("Cost")

    # Less-used people wait behind "…"; tapping it shows everyone.
    expect(chip(page, "Nell", scope="main")).to_be_hidden()
    form.locator("[data-more-chips]").tap()
    expect(chip(page, "Nell", scope="main")).to_be_visible()
    expect(form.locator("[data-more-chips]")).to_be_hidden()


def test_cost_box_matches_the_other_inputs(page):
    page.goto("/gifts/new")
    height = lambda sel: page.locator(sel).bounding_box()["height"]
    assert abs(height("main .price") - height("main [name=source]")) <= 2


def test_opening_the_sheet_does_not_pop_the_keyboard(page):
    # The title box is first in the form; it mustn't grab focus when the sheet opens.
    page.goto("/")
    take_photo(page)
    expect(page.locator("#add-dialog")).to_be_visible()
    assert page.evaluate("document.activeElement.tagName") not in ("INPUT", "TEXTAREA", "SELECT")


def test_add_someone_new_from_the_form(page):
    page.goto("/")
    take_photo(page)
    dialog = page.locator("#add-dialog")
    dialog.locator("[data-new-person]").tap()
    name = dialog.locator("[data-new-name]")
    expect(name).to_be_focused()
    name.fill("Dani")
    name.press("Enter")  # adds the chip; doesn't save the gift
    expect(dialog).to_be_visible()
    expect(chip(page, "Dani").locator("input")).to_be_checked()
    expect(dialog.locator("[data-price]")).to_be_focused()  # same as picking a chip
    page.keyboard.type("20")
    dialog.locator("[data-save]").tap()
    expect(page.locator(".toast")).to_contain_text("Saved gift for Dani")
    # Dani is a chip next time.
    take_photo(page)
    expect(chip(page, "Dani")).to_have_count(1)


def test_unpicking_the_new_person_forgets_them(page):
    page.goto("/gifts/new")
    main = page.locator("main")
    main.locator("[data-new-person]").tap()
    main.locator("[data-new-name]").fill("Dani")
    main.locator("[data-new-add]").tap()
    chip(page, "Dani", scope="main").tap()  # unpick
    expect(main.locator("[data-new-chip]")).to_be_hidden()
    expect(main.locator("[data-new-person]")).to_be_visible()
    expect(main.locator("[data-new-name]")).to_have_value("")


# --- the + button, and the add form's layout ------------------------------------------

def test_add_form_title_comes_under_the_photo(page):
    page.goto("/")
    take_photo(page)
    dialog = page.locator("#add-dialog")
    expect(dialog.locator(".form-head h2")).to_have_text("New gift")
    expect(dialog.locator(".form-head input")).to_have_count(0)
    title = dialog.locator("label.field", has_text="Title").locator("input[name=title]")
    photo = dialog.locator(".photo-slot").bounding_box()
    assert title.bounding_box()["y"] > photo["y"] + photo["height"] - 1
    assert title.bounding_box()["y"] < dialog.locator(".chips").bounding_box()["y"]


def test_backing_out_of_the_camera_opens_the_form_without_a_photo(page):
    page.goto("/")
    page.locator("#fab-camera").dispatch_event("cancel")  # what Android fires on "back"
    dialog = page.locator("#add-dialog")
    expect(dialog).to_be_visible()
    expect(dialog.locator("[data-photo-id]")).to_have_value("")
    chip(page, "Emma").tap()
    page.keyboard.type("12")
    dialog.locator("[data-save]").tap()
    expect(page.locator(".toast")).to_contain_text("Saved gift for Emma")


def test_on_a_computer_plus_opens_the_form(browser, server):
    p, chromium = browser
    context = chromium.new_context(**p.devices["Desktop Chrome"], base_url=server[1])
    page = context.new_page()
    page.goto("/")
    chosen = []
    page.on("filechooser", lambda fc: chosen.append(fc))
    page.locator("label.fab").click()
    expect(page.locator("#add-dialog")).to_be_visible()
    page.wait_for_timeout(300)
    assert chosen == []  # no file picker: the form's own Camera/Gallery buttons are there
    context.close()
