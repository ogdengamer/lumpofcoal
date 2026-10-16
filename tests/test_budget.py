"""Budget dashboard maths and privacy. Made-up gifts.

Seeded budgets: Child Christmas $200 and Birthday $100 each; Grandchild Birthday $50;
Nils & Stacy Christmas $30 for the group; Nils Christmas $15.
"""

import re
from datetime import date

import pytest
from sqlalchemy import select

from app import budget
from app.gifts import ensure_occasions, find_or_create_group
from app.models import BudgetRule, Group, Occasion, OccasionType
from app.visibility import GiftRepo
from tests.test_gallery import add, clients, ids  # noqa: F401  (fixtures)

TODAY = date(2026, 10, 15)


def occasion(session, type_name, year=2026):
    return session.scalar(select(Occasion).join(OccasionType).where(
        OccasionType.name == type_name, Occasion.year == year))


@pytest.fixture
def christmas(session):
    return occasion(session, "Christmas")


def board(session, people, occ, viewer="Abel"):
    return budget.build(session, GiftRepo(session, people[viewer].id), occ, TODAY)


def row(b, name):
    return next(r for r in b.rows + b.unbudgeted if r.name == name)


def names(rows):
    return [r.name for r in rows]


def give(session, people, viewer="Jules", **fields):
    gift = GiftRepo(session, people[viewer].id).create(**fields)
    session.commit()
    return gift


# --- dates --------------------------------------------------------------------------

@pytest.mark.parametrize("year, expected", [(2024, date(2024, 3, 31)), (2026, date(2026, 4, 5)),
                                            (2027, date(2027, 3, 28)), (2038, date(2038, 4, 25))])
def test_easter(year, expected):
    assert budget.easter(year) == expected


def test_default_is_the_next_dated_occasion(session):
    ensure_occasions(session, date(2026, 12, 26))  # the app does this at startup
    assert budget.next_occasion(session, date(2026, 10, 15)).name == "Christmas 2026"
    assert budget.next_occasion(session, date(2026, 12, 25)).name == "Christmas 2026"
    assert budget.next_occasion(session, date(2026, 12, 26)).name == "Easter 2027"


def test_split_adds_back_up():
    assert budget.split(1000, 3) == [334, 333, 333]
    assert sum(budget.split(1001, 7)) == 1001


# --- rows ----------------------------------------------------------------------------

def test_every_budget_has_a_row_even_without_gifts(session, people, christmas):
    b = board(session, people, christmas)
    assert names(b.rows) == ["Astrid", "Elsa", "Emma", "Shay", "Nils & Stacy"]
    assert all(r.no_gifts for r in b.rows) and b.unbudgeted == []
    assert row(b, "Nils & Stacy").budget_cents == 3000
    assert b.budget_cents == 4 * 20000 + 3000


def test_spent_planned_left_and_colours(session, people, christmas):
    emma, elsa = people["Emma"].id, people["Elsa"].id
    give(session, people, recipient_person_id=emma, occasion_id=christmas.id, cost_cents=18000)
    give(session, people, recipient_person_id=emma, occasion_id=christmas.id, cost_cents=4000,
         status="idea")
    give(session, people, recipient_person_id=elsa, occasion_id=christmas.id, cost_cents=20500,
         status="given")
    give(session, people, recipient_person_id=people["Shay"].id, occasion_id=christmas.id,
         cost_cents=17999, status="wrapped")
    b = board(session, people, christmas)
    em, el, sh = row(b, "Emma"), row(b, "Elsa"), row(b, "Shay")
    assert (em.spent_cents, em.planned_cents, em.left_cents, em.gift_count) == (18000, 4000, 2000, 2)
    assert em.status == "amber"   # exactly 90%: within 10%
    assert sh.status == "green"   # just under 90%
    assert (el.status, el.left_cents) == ("red", -500)
    assert row(b, "Astrid").no_gifts and not em.no_gifts


def test_ideas_do_not_change_the_colour(session, people, christmas):
    give(session, people, recipient_person_id=people["Emma"].id, occasion_id=christmas.id,
         cost_cents=50000, status="idea")
    assert row(board(session, people, christmas), "Emma").status == "green"


def test_gift_to_one_member_counts_toward_their_group_budget(session, people, christmas):
    # Nils has his own $15 rule, but the group budget wins.
    give(session, people, recipient_person_id=people["Nils"].id, occasion_id=christmas.id,
         cost_cents=1500)
    b = board(session, people, christmas)
    assert row(b, "Nils & Stacy").spent_cents == 1500
    assert "Nils" not in names(b.rows + b.unbudgeted)


def test_shared_gift_splits_across_people(session, people, christmas):
    pair = find_or_create_group(session, {people["Emma"].id, people["Elsa"].id})
    give(session, people, recipient_group_id=pair.id, occasion_id=christmas.id, cost_cents=5001)
    b = board(session, people, christmas)
    assert sorted([row(b, "Emma").spent_cents, row(b, "Elsa").spent_cents]) == [2500, 2501]


def test_unbudgeted_people_are_listed_separately(session, people, groups, christmas):
    give(session, people, recipient_group_id=groups["Grandkids"].id, occasion_id=christmas.id,
         cost_cents=3000)
    b = board(session, people, christmas)
    assert names(b.unbudgeted) == ["Cal", "Evan", "Gina"]
    assert b.unbudgeted_spent_cents == 3000 and b.spent_cents == 0


def test_per_member_group_budget_is_times_members(session, people, groups, christmas):
    session.add(BudgetRule(occasion_type_id=christmas.type_id, group_id=groups["Grandkids"].id,
                           amount_cents=2500, basis="per_member"))
    session.commit()
    assert row(board(session, people, christmas), "Grandkids").budget_cents == 7500


def test_inactive_people_without_gifts_are_left_off(session, people, christmas):
    people["Shay"].active = False
    session.commit()
    assert "Shay" not in names(board(session, people, christmas).rows)


def test_birthday_rows_sorted_by_birthday(session, people):
    for name, (m, d) in {"Emma": (11, 30), "Elsa": (10, 20), "Astrid": (3, 1)}.items():
        people[name].birthday_month, people[name].birthday_day = m, d
    session.commit()
    b = board(session, people, occasion(session, "Birthday"))
    assert names(b.rows)[:3] == ["Elsa", "Emma", "Astrid"]
    assert row(b, "Elsa").birthday == date(2026, 10, 20)


# --- privacy -----------------------------------------------------------------------------

def test_your_gifts_and_row_are_never_counted(session, people, christmas):
    give(session, people, recipient_person_id=people["Abel"].id, occasion_id=christmas.id,
         cost_cents=99900)
    b = board(session, people, christmas, viewer="Abel")
    assert "Abel" not in names(b.rows + b.unbudgeted)
    assert b.spent_cents == 0 and b.unbudgeted_spent_cents == 0 and b.gift_count == 0
    # Jules, who gave it, does see it.
    assert row(board(session, people, christmas, viewer="Jules"), "Abel").spent_cents == 99900


def test_budget_for_a_group_you_are_in_is_hidden(session, people, christmas):
    parents = Group(name="Parents", members=[people["Abel"], people["Jules"]])
    session.add(parents)
    session.flush()
    session.add(BudgetRule(occasion_type_id=christmas.type_id, group_id=parents.id,
                           amount_cents=10000, basis="group_total"))
    give(session, people, viewer="Abel", recipient_person_id=people["Jules"].id,
         occasion_id=christmas.id, cost_cents=2000)
    b = board(session, people, christmas, viewer="Abel")
    assert "Parents" not in names(b.rows)
    assert row(b, "Jules").spent_cents == 2000 and row(b, "Jules").budget_cents is None


# --- upcoming birthdays ----------------------------------------------------------------------

def test_upcoming_birthdays_within_60_days_across_new_year(session, people):
    for name, (m, d) in {"Emma": (12, 1), "Elsa": (1, 10), "Astrid": (3, 1),
                         "Abel": (12, 5)}.items():
        people[name].birthday_month, people[name].birthday_day = m, d
    session.commit()
    ensure_occasions(session, date(2026, 11, 20))
    jan10 = occasion(session, "Birthday", 2027)
    give(session, people, recipient_person_id=people["Elsa"].id, occasion_id=jan10.id,
         cost_cents=4000)
    soon = budget.upcoming_birthdays(session, GiftRepo(session, people["Abel"].id),
                                     date(2026, 11, 20))
    assert [(u.person.name, u.when) for u in soon] == [("Emma", date(2026, 12, 1)),
                                                       ("Elsa", date(2027, 1, 10))]
    assert (soon[1].spent_cents, soon[1].budget_cents) == (4000, 10000)


# --- the page ------------------------------------------------------------------------------

def test_dashboard_page(clients, ids):
    abel, jules = clients
    add(jules, recipients=f"person:{ids['Abel']}", title="Secret Telescope", price="123",
        occasion_id=ids["occ:Christmas 2026"])
    add(abel, recipients=f"person:{ids['Emma']}", title="Kite", price="20",
        occasion_id=ids["occ:Christmas 2026"])
    add(abel, recipients=f"person:{ids['Elsa']}", title="Loose sock", price="5")
    page = abel.get(f"/budget?occasion={ids['occ:Christmas 2026']}").text
    assert 'href="/budget"' in page
    assert "Emma" in page and "$20" in page and "no gifts yet" in page
    assert re.search(rf'href="/\?recipient={ids["Emma"]}&(amp;)?occasion={ids["occ:Christmas 2026"]}"', page)
    assert "Gifts with no occasion" in page and "Loose sock" in page
    assert "Telescope" not in page and "123" not in page


def test_dashboard_defaults_to_next_occasion(clients):
    abel, _ = clients
    assert "<option value" in abel.get("/budget").text
    assert abel.get("/budget").status_code == 200
