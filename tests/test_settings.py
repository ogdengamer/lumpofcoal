"""Settings: budgets, people, relations, groups, occasion types. Made-up data."""

from html import unescape

import pytest
from sqlalchemy import select

from app import manage
from app.models import BudgetRule, Group, OccasionType, Person, Relation
from tests.test_gallery import USERS, add, clients, ids  # noqa: F401  (fixtures)


def db(client):
    return client.app.state.sessionmaker()


def type_id(client, name):
    with db(client) as s:
        return s.scalar(select(OccasionType.id).where(OccasionType.name == name))


def relation_id(client, name):
    with db(client) as s:
        return s.scalar(select(Relation.id).where(Relation.name == name))


def rules(client):
    """{(occasion type, target name): (amount cents, basis)} for every rule."""
    with db(client) as s:
        out = {}
        for r in s.scalars(select(BudgetRule)):
            target = (r.relation or r.person or r.group).name
            out[(r.occasion_type.name, target)] = (r.amount_cents, r.basis)
        return out


def post(client, url, **data):
    return client.post(url, data=data, follow_redirects=False)


def add_rule(client, occasion, target, amount, basis="per_member"):
    return post(client, "/settings/budgets", occasion_type_id=type_id(client, occasion),
                target=target, amount=amount, basis=basis)


def rule_id(client, occasion, target_name):
    with db(client) as s:
        for r in s.scalars(select(BudgetRule)):
            if r.occasion_type.name == occasion and (r.relation or r.person or r.group).name == target_name:
                return r.id


# --- pages -------------------------------------------------------------------------

@pytest.mark.parametrize("tab", ["budgets", "people", "groups", "occasions", "users", "backups"])
def test_tabs_render(clients, tab):
    abel, _ = clients
    resp = abel.get(f"/settings/{tab}")
    assert resp.status_code == 200 and 'aria-current="page"' in unescape(resp.text)


def test_settings_link_in_nav_and_home_redirects(clients):
    abel, _ = clients
    assert 'href="/settings"' in abel.get("/").text
    assert post(abel, "/settings").status_code in (303, 405)
    assert abel.get("/settings", follow_redirects=False).headers["location"] == "/settings/budgets"


# --- budgets ------------------------------------------------------------------------

def test_add_edit_delete_a_budget(clients, ids):
    abel, _ = clients
    resp = add_rule(abel, "Christmas", f"relation:{relation_id(abel, 'Grandchild')}", "75")
    assert resp.status_code == 303 and resp.headers["location"] == "/settings/budgets?done=added"
    assert rules(abel)[("Christmas", "Grandchild")] == (7500, "per_member")

    rid = rule_id(abel, "Christmas", "Grandchild")
    post(abel, f"/settings/budgets/{rid}", amount="$80.50")
    assert rules(abel)[("Christmas", "Grandchild")] == (8050, "per_member")

    post(abel, f"/settings/budgets/{rid}/delete")
    assert ("Christmas", "Grandchild") not in rules(abel)


def test_group_budget_basis(clients, ids):
    abel, _ = clients
    add_rule(abel, "Christmas", f"group:{ids['group:Grandkids']}", "25", basis="per_member")
    assert rules(abel)[("Christmas", "Grandkids")] == (2500, "per_member")
    post(abel, f"/settings/budgets/{rule_id(abel, 'Christmas', 'Grandkids')}",
         amount="60", basis="group_total")
    assert rules(abel)[("Christmas", "Grandkids")] == (6000, "group_total")


def test_person_and_relation_budgets_are_always_per_person(clients, ids):
    abel, _ = clients
    add_rule(abel, "Easter", f"person:{ids['Emma']}", "20", basis="group_total")
    assert rules(abel)[("Easter", "Emma")] == (2000, "per_member")


@pytest.mark.parametrize("amount, message", [("", "Enter a budget amount"),
                                             ("abc", "Cost should be a number")])
def test_budget_amount_is_validated(clients, ids, amount, message):
    abel, _ = clients
    resp = add_rule(abel, "Easter", f"person:{ids['Emma']}", amount)
    assert resp.status_code == 422 and message in unescape(resp.text)
    assert ("Easter", "Emma") not in rules(abel)


def test_duplicate_budget_is_refused(clients, ids):
    abel, _ = clients
    resp = add_rule(abel, "Christmas", f"relation:{relation_id(abel, 'Child')}", "10")
    assert resp.status_code == 422
    assert "There's already a Christmas budget for Child" in unescape(resp.text)
    assert rules(abel)[("Christmas", "Child")] == (20000, "per_member")


def test_birthday_budgets_cannot_be_for_a_group(clients, ids):
    abel, _ = clients
    resp = add_rule(abel, "Birthday", f"group:{ids['group:Girls']}", "40", basis="group_total")
    assert resp.status_code == 422 and "Birthday budgets are per person" in unescape(resp.text)
    assert ("Birthday", "Girls") not in rules(abel)
    # ...and the form doesn't offer groups for Birthday.
    page = abel.get("/settings/budgets").text
    birthday = page[page.index("<h2>Birthday</h2>"):page.index("<h2>Easter</h2>")]
    assert "group:" not in birthday


def test_a_person_can_be_in_only_one_budgeted_group_per_occasion(clients, ids):
    abel, _ = clients
    assert add_rule(abel, "Christmas", f"group:{ids['group:Grandkids']}", "60",
                    basis="group_total").status_code == 303
    # Girls (Cal, Gina) overlaps Grandkids (Evan, Cal, Gina).
    resp = add_rule(abel, "Christmas", f"group:{ids['group:Girls']}", "40", basis="group_total")
    assert resp.status_code == 422
    assert "Cal and Gina are already in the Grandkids Christmas budget" in unescape(resp.text)
    # A different occasion is fine.
    assert add_rule(abel, "Easter", f"group:{ids['group:Girls']}", "40",
                    basis="group_total").status_code == 303


def test_adding_a_member_to_a_budgeted_group_is_checked_too(clients, ids):
    abel, _ = clients  # Nils & Stacy has a seeded Christmas budget.
    add_rule(abel, "Christmas", f"group:{ids['group:Girls']}", "40", basis="group_total")
    resp = post(abel, f"/settings/groups/{ids['group:Girls']}", name="Girls",
                members=[ids["Cal"], ids["Gina"], ids["Nils"]])
    assert resp.status_code == 422
    assert "Nils is already in the Nils & Stacy Christmas budget" in unescape(resp.text)
    with db(abel) as s:
        assert {m.name for m in s.get(Group, ids["group:Girls"]).members} == {"Cal", "Gina"}


# --- budgets aimed at the viewer are hidden from them ---------------------------------------

def test_budgets_for_you_are_hidden_from_you(clients, ids):
    abel, jules = clients
    assert add_rule(abel, "Birthday", f"person:{ids['Jules']}", "150").status_code == 303
    assert "Jules" in abel.get("/settings/budgets").text.split("<h2>Birthday</h2>")[1].split("<h2>")[0]

    page = jules.get("/settings/budgets").text
    birthday = page.split("<h2>Birthday</h2>")[1].split("<h2>")[0]
    assert "150" not in birthday and "budget for Jules" not in birthday
    rid = rule_id(abel, "Birthday", "Jules")
    assert post(jules, f"/settings/budgets/{rid}", amount="1").status_code == 404
    assert post(jules, f"/settings/budgets/{rid}/delete").status_code == 404
    assert rules(abel)[("Birthday", "Jules")] == (15000, "per_member")


def test_budgets_for_a_group_you_are_in_are_hidden(clients, ids):
    abel, jules = clients
    with db(abel) as s:
        s.add(Group(name="Parents", members=[s.get(Person, ids["Abel"]), s.get(Person, ids["Jules"])]))
        s.commit()
        parents = s.scalar(select(Group.id).where(Group.name == "Parents"))
        s.add(BudgetRule(occasion_type_id=type_id(abel, "Easter"), group_id=parents,
                         amount_cents=9900, basis="group_total"))
        s.commit()
    for client in (abel, jules):
        assert "99" not in client.get("/settings/budgets").text.split("<h2>Easter</h2>")[1]
    # ...and from the add form's group-budget hint.
    assert "Parents" not in abel.get("/gifts/new").text.split("data-hints=")[1].split(">")[0]


def test_cannot_budget_for_yourself_or_your_groups(clients, ids):
    abel, _ = clients
    resp = add_rule(abel, "Easter", f"person:{ids['Abel']}", "10")
    assert resp.status_code == 422 and "You can't set a budget for yourself" in unescape(resp.text)
    assert f'value="person:{ids["Abel"]}"' not in abel.get("/settings/budgets").text


def test_relation_budgets_are_visible_to_both(clients, ids):
    abel, jules = clients
    for client in (abel, jules):
        christmas = client.get("/settings/budgets").text.split("<h2>Christmas</h2>")[1]
        assert 'value="200"' in christmas.split("<h2>")[0]


# --- people and relations -----------------------------------------------------------------

def person(client, name):
    with db(client) as s:
        p = s.scalar(select(Person).where(Person.name == name))
        s.expunge_all()
        return p


def test_add_person_with_birthday(clients):
    abel, _ = clients
    resp = post(abel, "/settings/people", name="Dani", relation_id=relation_id(abel, "Friend"),
                birthday_month="2", birthday_day="29", notes="Loves tea", active="1")
    assert resp.headers["location"] == "/settings/people?done=added"
    dani = person(abel, "Dani")
    assert (dani.birthday_month, dani.birthday_day, dani.notes, dani.active) == (2, 29, "Loves tea", True)


@pytest.mark.parametrize("month, day, message", [
    ("2", "30", "February doesn't have a day 30"),
    ("4", "", "A birthday needs both a month and a day"),
    ("", "12", "A birthday needs both a month and a day"),
])
def test_birthday_is_validated(clients, month, day, message):
    abel, _ = clients
    resp = post(abel, "/settings/people", name="Dani", birthday_month=month, birthday_day=day,
                active="1")
    assert resp.status_code == 422 and message in unescape(resp.text)
    assert person(abel, "Dani") is None


def test_edit_and_deactivate_person(clients, ids):
    abel, _ = clients
    post(abel, f"/settings/people/{ids['Pop']}", name="Grandpa", relation_id=relation_id(abel, "Parent"),
         birthday_month="6", birthday_day="1")  # no "active": unticked
    pop = person(abel, "Grandpa")
    assert (pop.birthday_month, pop.birthday_day, pop.active) == (6, 1, False)
    assert f'value="person:{ids["Pop"]}"' not in abel.get("/gifts/new").text  # off the chips


def test_duplicate_person_name_refused(clients, ids):
    abel, _ = clients
    resp = post(abel, "/settings/people", name="Emma", active="1")
    assert resp.status_code == 422 and "There's already someone called Emma" in unescape(resp.text)


def test_app_users_cannot_be_made_inactive(clients, ids):
    abel, _ = clients
    resp = post(abel, f"/settings/people/{ids['Jules']}", name="Jules")
    assert resp.status_code == 422 and "uses the app" in unescape(resp.text)
    assert person(abel, "Jules").active


def test_relations_add_rename_delete(clients):
    abel, _ = clients
    post(abel, "/settings/relations", name="Cousin")
    cousin = relation_id(abel, "Cousin")
    post(abel, f"/settings/relations/{cousin}", name="Cousins")
    assert relation_id(abel, "Cousins") == cousin
    post(abel, f"/settings/relations/{cousin}/delete")
    assert relation_id(abel, "Cousins") is None


def test_relation_in_use_cannot_be_deleted(clients):
    abel, _ = clients
    resp = post(abel, f"/settings/relations/{relation_id(abel, 'Child')}/delete")
    assert resp.status_code == 422 and "can't be deleted" in unescape(resp.text)
    assert relation_id(abel, "Child") is not None


# --- groups ---------------------------------------------------------------------------------

def test_add_rename_and_delete_group(clients, ids):
    abel, _ = clients
    post(abel, "/settings/groups", name="Twins", members=[ids["Emma"], ids["Elsa"]])
    with db(abel) as s:
        twins = s.scalar(select(Group).where(Group.name == "Twins"))
        assert {m.name for m in twins.members} == {"Emma", "Elsa"} and not twins.ad_hoc
        twins_id = twins.id
    post(abel, f"/settings/groups/{twins_id}", name="The Twins", members=[ids["Emma"], ids["Elsa"]])
    post(abel, f"/settings/groups/{twins_id}/delete")
    with db(abel) as s:
        assert s.get(Group, twins_id) is None


def test_group_needs_two_people(clients, ids):
    abel, _ = clients
    resp = post(abel, "/settings/groups", name="Solo", members=[ids["Emma"]])
    assert resp.status_code == 422 and "at least two people" in unescape(resp.text)


def test_group_with_gifts_cannot_be_deleted(clients, ids):
    abel, _ = clients
    add(abel, recipients=f"group:{ids['group:Girls']}", title="Kite")
    resp = post(abel, f"/settings/groups/{ids['group:Girls']}/delete")
    assert resp.status_code == 422 and "Gifts are recorded for Girls" in unescape(resp.text)


def test_group_including_you_cannot_be_deleted_whatever_its_gifts(clients, ids):
    # "It has gifts" would be a spoiler for a group you're in, so the answer never varies.
    abel, jules = clients
    with db(abel) as s:
        s.add(Group(name="Parents", members=[s.get(Person, ids["Abel"]), s.get(Person, ids["Jules"])]))
        s.commit()
        parents = s.scalar(select(Group.id).where(Group.name == "Parents"))
    resp = post(abel, f"/settings/groups/{parents}/delete")
    assert resp.status_code == 422 and "Groups that include you can't be deleted" in unescape(resp.text)


# --- occasion types ----------------------------------------------------------------------------

def test_add_occasion_creates_this_and_next_year(clients):
    abel, _ = clients
    post(abel, "/settings/occasions", name="Anniversary", date_rule="fixed", month="6", day="14")
    page = abel.get("/gifts/new").text
    assert "Anniversary 20" in page
    with db(abel) as s:
        t = s.scalar(select(OccasionType).where(OccasionType.name == "Anniversary"))
        assert (t.date_rule, t.month, t.day) == ("fixed", 6, 14)


def test_fixed_date_needs_month_and_day(clients):
    abel, _ = clients
    resp = post(abel, "/settings/occasions", name="Anniversary", date_rule="fixed")
    assert resp.status_code == 422 and "A fixed date needs" in unescape(resp.text)


def test_birthday_occasion_keeps_its_date_rule(clients):
    abel, _ = clients
    bday = type_id(abel, "Birthday")
    resp = post(abel, f"/settings/occasions/{bday}", name="Birthday", date_rule="none")
    assert resp.status_code == 422
    assert post(abel, f"/settings/occasions/{bday}", name="Bday",
                date_rule="birthday").status_code == 303
    resp = post(abel, "/settings/occasions", name="Name day", date_rule="birthday")
    assert resp.status_code == 422


def relation_order(client):
    with client.app.state.sessionmaker() as session:
        return [r.name for r in manage.relations(session)]


def test_relations_have_their_own_tab(clients):
    abel, _ = clients
    relations = abel.get("/settings/relations").text
    assert "Add relation" in relations and 'aria-current="page">Relations<' in relations
    people = abel.get("/settings/people").text
    assert "Add relation" not in people and 'href="/settings/relations"' in people
    assert post(abel, "/settings/relations", name="Cousin").headers["location"] == \
        "/settings/relations?done=added"


def test_new_relation_from_a_persons_row(clients, ids):
    abel, _ = clients
    fields = {"name": "Dani", "active": "1", "relation_id": "new"}
    resp = abel.post("/settings/people", data=fields | {"new_relation": " "})
    assert resp.status_code == 422 and "Type the new relation" in resp.text
    post(abel, "/settings/people", **fields, new_relation="Daughter-in-law")
    dil = relation_id(abel, "Daughter-in-law")
    assert relation_order(abel)[-1] == "Daughter-in-law"  # new ones go last
    # Naming one that exists picks it rather than complaining.
    resp = abel.post(f"/settings/people/{ids['Emma']}", headers={"X-Autosave": "1"},
                     data={"name": "Emma", "active": "1", "relation_id": "new",
                           "new_relation": "Daughter-in-law"})
    assert resp.json() == {"ok": True}
    page = abel.get("/settings/people").text
    assert page.count(f'<option value="{dil}" selected>') == 2


def test_people_page_tags_missing_details(clients, ids):
    abel, _ = clients
    post(abel, "/settings/people", name="Dani", active="1", birthday_month="3", birthday_day="4")
    page = abel.get("/settings/people").text
    row = page[page.index(f'id="person-{ids["Emma"]}"'):][:3000].split("</form>")[0]
    assert "needs a birthday" in row and "needs a relation" not in row
    dani = page[page.index('value="Dani"'):].split("</form>")[0]
    assert "needs a relation" in dani and "needs a birthday" not in dani
    me = page[page.index(f'id="person-{ids["Abel"]}"'):].split("</form>")[0]
    assert "needs a" not in me  # never nagged about yourself


def test_relations_can_be_reordered(clients):
    abel, _ = clients
    assert relation_order(abel)[:3] == ["Partner", "Child", "Grandchild"]
    post(abel, "/settings/relations", name="Cousin")
    assert relation_order(abel)[-1] == "Cousin"  # new ones go last
    cousin = relation_id(abel, "Cousin")
    resp = abel.post(f"/settings/relations/{cousin}/up", follow_redirects=False)
    assert resp.headers["location"] == f"/settings/relations#relation-{cousin}"
    assert relation_order(abel)[-2:] == ["Cousin", "Friend"]
    first = relation_id(abel, "Partner")
    abel.post(f"/settings/relations/{first}/up")  # already first: nothing happens
    abel.post(f"/settings/relations/{relation_id(abel, 'Child')}/up")
    assert relation_order(abel)[:2] == ["Child", "Partner"]
    page = abel.get("/settings/relations").text
    assert page.index('id="relation-' + str(relation_id(abel, "Child"))) < page.index(
        'id="relation-' + str(first))
    assert abel.post("/settings/relations/999/up").status_code == 404


def test_relation_order_is_used_everywhere(clients, ids):
    abel, _ = clients
    for _ in range(9):  # Friend to the top
        abel.post(f"/settings/relations/{relation_id(abel, 'Friend')}/up")
    assert relation_order(abel)[0] == "Friend"
    gallery = abel.get("/").text
    filters = gallery[gallery.index('name="relation"'):]
    assert filters.index(">Friend<") < filters.index(">Partner<")
    with abel.app.state.sessionmaker() as session:
        from app.gifts import recipient_chips
        from app.visibility import GiftRepo
        top, rest = recipient_chips(session, GiftRepo(session, ids["Abel"]))
        names = [c.label for c in top + rest]
    assert names.index("Nils") < names.index("Jules")  # a Friend before the Partner


def test_upgrade_keeps_the_existing_order(tmp_path):
    """Before positions, relations were in the order they were added."""
    from alembic import command
    from sqlalchemy import text

    from app.db import alembic_config, make_engine
    engine = make_engine(str(tmp_path))
    cfg = alembic_config(engine)
    command.upgrade(cfg, "0006")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO relations (id, name) VALUES (1, 'Spouse'), (2, 'Kid'), "
                          "(5, 'Cousin')"))
    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT name FROM relations ORDER BY position")).scalars().all() \
            == ["Spouse", "Kid", "Cousin"]
