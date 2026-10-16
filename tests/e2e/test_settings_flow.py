"""Settings pages in a browser: the small bits of JS, and a full add-budget round trip."""

import re

from tests.e2e.conftest import expect
from tests.e2e.test_add_flow import assert_no_sideways_scroll


def section(page, heading):
    return page.locator(".settings-section", has=page.get_by_role("heading", name=heading, exact=True))


def test_basis_only_shows_for_group_budgets(page):
    page.goto("/settings/budgets")
    add = section(page, "Christmas").locator("[data-rule-form]")
    basis = add.locator("[data-basis]")
    expect(basis).to_be_hidden()
    add.locator("[data-target]").select_option(label="Grandkids")
    expect(basis).to_be_visible()
    add.locator("[data-target]").select_option(label="Grandchild")
    expect(basis).to_be_hidden()


def test_add_a_budget_round_trip(page):
    page.goto("/settings/budgets")
    add = section(page, "Easter").locator("[data-rule-form]")
    add.locator("[data-target]").select_option(label="Grandchild")
    add.locator("input[name=amount]").fill("15")
    add.get_by_role("button", name="Add").click()
    expect(page.locator(".toast")).to_have_text("Added.")
    expect(section(page, "Easter").locator(".rule-row:not(.add-row)", has_text="Grandchild")
           .locator("input[name=amount]")).to_have_value("15")


def test_delete_asks_first(page):
    page.goto("/settings/budgets")
    row = section(page, "Christmas").locator(".rule-row:not(.add-row)", has_text="Nils & Stacy")
    page.once("dialog", lambda d: d.dismiss())
    row.get_by_role("button", name="Delete").click()
    expect(row).to_have_count(1)
    page.once("dialog", lambda d: d.accept())
    row.get_by_role("button", name="Delete").click()
    expect(page.locator(".toast")).to_have_text("Deleted.")
    expect(row).to_have_count(0)


def test_month_and_day_only_for_a_fixed_date(page):
    page.goto("/settings/occasions")
    add = page.locator(".occasion-row.add-row")
    expect(add.locator("[data-fixed-date]")).to_be_hidden()
    add.locator("[data-date-rule]").select_option("fixed")
    expect(add.locator("[data-fixed-date]")).to_be_visible()


def test_settings_fit_a_phone(page):
    for tab in ("budgets", "people", "groups", "occasions"):
        page.goto(f"/settings/{tab}")
        assert_no_sideways_scroll(page)


# --- save as you go ----------------------------------------------------------------------

def person_row(page, name):
    return page.locator(".person-row:not(.add-row)").filter(
        has=page.locator(f'input[name=name][value="{name}"]'))


def test_people_save_as_you_go(page):
    page.goto("/settings/people")
    pop = person_row(page, "Pop")
    expect(pop.get_by_role("button", name="Save")).to_have_count(0)
    pop.locator("input[name=notes]").fill("Likes fishing")
    pop.locator("input[name=notes]").press("Tab")
    expect(pop.locator("[data-row-status]")).to_have_text("Saved ✓")
    page.reload()
    expect(person_row(page, "Pop").locator("input[name=notes]")).to_have_value("Likes fishing")


def test_birthday_waits_for_both_halves(page):
    page.goto("/settings/people")
    em = person_row(page, "Emma")
    em.locator("select[name=birthday_month]").select_option(label="Nov")
    expect(em.locator("[data-row-status]")).to_have_text("Add the day…")
    em.locator("input[name=birthday_day]").fill("30")
    em.locator("input[name=birthday_day]").press("Enter")  # saves in place, no page load
    expect(em.locator("[data-row-status]")).to_have_text("Saved ✓")
    expect(page).to_have_url(re.compile(r"/settings/people$"))
    page.reload()
    expect(person_row(page, "Emma").locator("input[name=birthday_day]")).to_have_value("30")


def test_an_error_shows_on_its_row_and_nothing_saves(page):
    page.goto("/settings/people")
    name = person_row(page, "Emma").locator("input[name=name]")
    name.fill("Elsa")
    name.press("Tab")
    status = page.locator(".person-row [data-row-status].error")
    expect(status).to_have_text("There's already someone called Elsa")
    page.reload()
    expect(person_row(page, "Emma")).to_have_count(1)


def test_budget_and_group_save_as_you_go(page):
    page.goto("/settings/budgets")
    row = section(page, "Christmas").locator(".rule-row:not(.add-row)", has_text="Child")
    row.locator("input[name=amount]").fill("250")
    row.locator("input[name=amount]").press("Tab")
    expect(row.locator("[data-row-status]")).to_have_text("Saved ✓")

    page.goto("/settings/groups")
    girls = page.locator(".group-card").filter(has=page.locator('input[name=name][value="Girls"]'))
    girls.locator(".chip", has_text="Elsa").click()
    expect(girls.locator("[data-row-status]")).to_have_text("Saved ✓")
    page.reload()
    girls = page.locator(".group-card").filter(has=page.locator('input[name=name][value="Girls"]'))
    expect(girls.locator(".chip", has_text="Elsa").locator("input")).to_be_checked()
