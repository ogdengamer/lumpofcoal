import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models import Base, BudgetRule, Gift, Occasion, OccasionType, Person, Relation
from app.seed import OCCASION_TYPES, RELATIONS, seed
from tests.family import seed_family


def test_migrations_match_models(engine):
    """Fails if a model changed without a matching Alembic migration."""
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


# --- seed ----------------------------------------------------------------

def test_first_run_defaults_are_generic(tmp_path):
    from app.db import make_engine, make_sessionmaker, migrate
    engine = make_engine(str(tmp_path / "fresh"))
    migrate(engine)
    with make_sessionmaker(engine)() as fresh:
        assert seed(fresh) is True
        assert [r.name for r in fresh.scalars(select(Relation).order_by(Relation.id))] == RELATIONS
        assert RELATIONS[0] == "Partner"
        assert {t.name for t in fresh.scalars(select(OccasionType))} == set(OCCASION_TYPES)
        for model in (Person, BudgetRule, Occasion):
            assert fresh.scalar(select(func.count()).select_from(model)) == 0



def test_seed_people_relations_and_groups(people, groups):
    assert len(people) == 17
    assert people["Nell"].relation.name == "Parent"
    assert people["Hazel"].relation.name == "Niece/Nephew"
    assert people["Gina"].relation.name == "Grandchild"
    assert {p.name for p in groups["Grandkids"].members} == {"Cal", "Evan", "Gina"}
    assert {p.name for p in groups["Girls"].members} == {"Cal", "Gina"}
    assert {p.name for p in groups["Nils & Stacy"].members} == {"Nils", "Stacy"}


def test_seed_occasions_and_rules(session):
    occasions = {o.name for o in session.scalars(select(Occasion))}
    assert occasions == {"Christmas 2026", "Birthday 2026"}
    assert session.scalar(select(OccasionType).where(OccasionType.name == "Christmas")).day == 25

    rules = {
        (r.occasion_type.name,
         (r.relation or r.person or r.group).name, r.amount_cents, r.basis)
        for r in session.scalars(select(BudgetRule))
    }
    assert rules == {
        ("Christmas", "Child", 20000, "per_member"),
        ("Birthday", "Child", 10000, "per_member"),
        ("Birthday", "Grandchild", 5000, "per_member"),
        ("Christmas", "Nils & Stacy", 3000, "group_total"),
        ("Christmas", "Nils", 1500, "per_member"),
    }


def test_seed_only_runs_on_an_empty_database(session):
    session.delete(session.scalar(select(Person).where(Person.name == "Kira")))
    session.commit()
    assert seed(session) is False and seed_family(session) is False
    assert session.scalar(select(func.count()).select_from(Person)) == 16


def test_users_see_themselves_as_self(people):
    abel, jules = people["Abel"], people["Jules"]
    assert abel.relation_label(viewer_id=abel.id) == "Self"
    assert abel.relation_label(viewer_id=jules.id) == "Partner"
    assert people["Emma"].relation_label(viewer_id=abel.id) == "Child"


# --- constraints ---------------------------------------------------------

@pytest.mark.parametrize("fields", [
    pytest.param({"recipient_person_id": 1, "recipient_group_id": 1}, id="two-recipients"),
    pytest.param({}, id="no-recipient"),
    pytest.param({"recipient_person_id": 1, "status": "lost"}, id="bad-status"),
    pytest.param({"recipient_person_id": 1, "cost_cents": -1}, id="negative-cost"),
])
def test_invalid_gifts_are_rejected(session, people, fields):
    session.add(Gift(created_by_id=people["Abel"].id, **fields))
    with pytest.raises(IntegrityError):
        session.commit()


def test_duplicate_rule_for_same_target_is_rejected(session):
    child = session.scalar(select(Relation).where(Relation.name == "Child"))
    christmas = session.scalar(select(OccasionType).where(OccasionType.name == "Christmas"))
    session.add(BudgetRule(occasion_type=christmas, relation=child, amount_cents=1))
    with pytest.raises(IntegrityError):
        session.commit()


def test_rule_needs_exactly_one_target(session, people):
    christmas = session.scalar(select(OccasionType).where(OccasionType.name == "Christmas"))
    child = session.scalar(select(Relation).where(Relation.name == "Child"))
    session.add(BudgetRule(occasion_type=christmas, relation=child, person=people["Emma"],
                           amount_cents=1))
    with pytest.raises(IntegrityError):
        session.commit()


def test_foreign_keys_are_enforced(session):
    session.add(Gift(created_by_id=9999, recipient_person_id=9999))
    with pytest.raises(IntegrityError):
        session.commit()
